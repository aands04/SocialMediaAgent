import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.auth.service import hash_password
from app.config import get_settings
from app.db import Base, TenantSession, get_db
from app.games.live_test import serialize
from app.games.provider import FussballDeProvider
from app.jobs.generation import claim_next, process_generation_job
from app.main import app
from app.models import (
    AuditLog,
    Club,
    ClubBrandingConfiguration,
    ClubStatus,
    Game,
    GenerationJob,
    GenerationJobStatus,
    GenerationJobType,
    InstagramConnection,
    InstagramPage,
    JobStatus,
    LiveGameState,
    LogoAsset,
    MatchEvent,
    MediaAsset,
    PlanProfile,
    Post,
    PostChannelContent,
    PostStatus,
    ProviderSnapshot,
    PublicationJob,
    PublicationMediaItem,
    PublicationRuleSlot,
    Role,
    SharedOpponentLogo,
    SocialChannelConnection,
    StorageObject,
    StoryRule,
    Team,
    TeamChannelAssignment,
    User,
)
from app.posts.club_carousel import coordinate_club_matchday_feed, matchday_bundle_jobs
from app.tenancy.state import system_scope, tenant_scope
from app.web import berlin_datetime


@pytest.fixture
def browser(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'dashboard.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    raw_factory = sessionmaker(engine, class_=TenantSession, expire_on_commit=False)
    with raw_factory() as db, system_scope("Dashboard-Testmandant anlegen"):
        profile = PlanProfile(name="Dashboard-Test", description="Testprofil", version=1)
        db.add(profile)
        db.flush()
        club = Club(
            name="Dashboard Testverein",
            short_name="Dashboard",
            slug="dashboard-testverein",
            status=ClubStatus.ACTIVE,
            timezone="Europe/Berlin",
            plan_profile_id=profile.id,
        )
        db.add(club)
        db.flush()
        admin = User(
                email="admin@test.invalid",
                password_hash=hash_password("Very-Secure-Test-Password"),
                role=Role.ADMIN,
                all_teams=True,
                club_id=club.id,
            )
        db.add(admin)
        db.commit()

    @contextmanager
    def factory():
        with tenant_scope(club.id, admin.id), raw_factory() as db:
            yield db

    async def override():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override
    with TestClient(app) as client:
        page = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)', page.text).group(1)
        response = client.post(
            "/login",
            data={
                "email": "admin@test.invalid",
                "password": "Very-Secure-Test-Password",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        yield client, factory
    app.dependency_overrides.clear()


def csrf(client):
    response = client.get("/")
    return (
        re.search(r'name="csrf_token" value="([^"]+)', response.text).group(1)
        if 'name="csrf_token"' in response.text
        else client.cookies
    )


def session_csrf(client):
    response = client.get("/teams")
    return re.search(r'name="csrf_token" value="([^"]+)', response.text).group(1)


def create_automation_team(factory, *, suffix: str, rules: dict | None = None):
    with factory() as db:
        page = InstagramPage(
            internal_name=f"automation-page-{suffix}",
            display_name=f"Automatik Seite {suffix}",
            username=f"automation_{suffix}",
            club="Dashboard Testverein",
            active=True,
        )
        db.add(page)
        db.flush()
        team = Team(
            internal_name=f"automation-team-{suffix}",
            display_name=f"Automatik Mannschaft {suffix}",
            short_name=f"AM{suffix[:4]}",
            slug=f"automation-team-{suffix}",
            club="Dashboard Testverein",
            fussball_url=f"https://example.invalid/automation-{suffix}",
            instagram_page_id=page.id,
            media_subdir=f"automation-{suffix}/players",
            timezone="Europe/Berlin",
            rules=rules or {},
        )
        db.add(team)
        db.commit()
        return team.id


def test_publication_time_can_be_changed_from_post_detail(browser):
    client, factory = browser
    with factory() as db:
        page = InstagramPage(
            internal_name="reschedule-page",
            display_name="Zeitplan-Seite",
            username="reschedule_page",
            club="Dashboard Testverein",
            active=True,
        )
        db.add(page)
        db.flush()
        team = Team(
            internal_name="reschedule-team",
            display_name="Zeitplan Mannschaft",
            short_name="ZM",
            slug="reschedule-team",
            club="Dashboard Testverein",
            fussball_url="https://example.invalid/reschedule-team",
            instagram_page_id=page.id,
            media_subdir="reschedule-team/players",
            timezone="Europe/Berlin",
        )
        db.add(team)
        db.flush()
        post = Post(
            team_id=team.id,
            instagram_page_id=page.id,
            post_type="announcement",
            status=PostStatus.APPROVED,
            text="Freigegebener Zeitplan",
            approved_version=1,
        )
        db.add(post)
        db.flush()
        job = PublicationJob(
            post_id=post.id,
            team_id=team.id,
            instagram_page_id=page.id,
            kind="feed",
            media_path="/tmp/reschedule-feed.png",
            scheduled_at=datetime.now(timezone.utc) + timedelta(days=1),
            status=JobStatus.SCHEDULED,
            approval_status="approved",
            approved_post_version=post.version,
            idempotency_key="dashboard-reschedule-feed",
        )
        db.add(job)
        db.commit()
        post_id = post.id
        job_id = job.id
        job_version = job.version

    detail = client.get(f"/posts/{post_id}")
    assert detail.status_code == 200
    assert "Zeitpunkt ändern" in detail.text
    assert "Europe/Berlin" in detail.text

    local_time = (datetime.now(ZoneInfo("Europe/Berlin")) + timedelta(days=3)).replace(
        second=0, microsecond=0
    )
    payload = {
        "csrf_token": "ungueltig",
        "scheduled_at": local_time.strftime("%Y-%m-%dT%H:%M"),
        "job_version": str(job_version),
    }
    denied = client.post(
        f"/posts/{post_id}/publications/{job_id}/schedule",
        data=payload,
        follow_redirects=False,
    )
    assert denied.status_code == 403

    payload["csrf_token"] = session_csrf(client)
    changed = client.post(
        f"/posts/{post_id}/publications/{job_id}/schedule",
        data=payload,
        follow_redirects=False,
    )
    assert changed.status_code == 303
    assert changed.headers["location"].startswith(f"/posts/{post_id}")

    with factory() as db:
        saved_post = db.get(Post, post_id)
        saved_job = db.get(PublicationJob, job_id)
        saved_time = saved_job.scheduled_at
        if saved_time.tzinfo is None:
            saved_time = saved_time.replace(tzinfo=timezone.utc)
        assert saved_time == local_time.astimezone(timezone.utc)
        assert saved_job.absolute_time is True
        assert saved_job.stale_time is False
        assert saved_job.status == JobStatus.UNAPPROVED
        assert saved_job.approval_status == "reapproval_required"
        assert saved_post.status == PostStatus.REAPPROVAL
        assert db.scalar(
            select(AuditLog).where(
                AuditLog.action == "publication.schedule_changed",
                AuditLog.entity_id == job_id,
            )
        )


def test_branding_assistant_loads_and_saves_tenant_structured_values(browser):
    client, factory = browser
    with factory() as db:
        club = db.scalar(select(Club))
        page = InstagramPage(
            internal_name="branding-page",
            display_name="Branding-Seite",
            username="branding_page",
            club=club.name,
            active=True,
        )
        db.add(page)
        db.flush()
        team = Team(
            internal_name="branding-team",
            display_name="Beispielstadt Erste",
            short_name="Erste",
            slug="branding-team",
            club=club.name,
            fussball_url="https://example.invalid/branding-team",
            instagram_page_id=page.id,
            media_subdir="branding-team",
        )
        db.add(team)
        db.flush()
        db.add(
            Game(
                team_id=team.id,
                provider="fixture",
                external_id="branding-home",
                home_team=team.display_name,
                away_team="Gastverein",
                kickoff=datetime.now(timezone.utc) + timedelta(days=2),
                venue="Sportpark Beispielstadt",
                source_url="https://example.invalid/game",
            )
        )
        db.commit()
        club_id = club.id
        club_version = club.version
        team_id = team.id

    page = client.get("/branding")
    assert page.status_code == 200
    assert "Beispielstadt Erste" in page.text
    assert "Sportpark Beispielstadt" in page.text
    assert "DejaVu Sans" in page.text
    assert "Liberation Serif" in page.text
    assert "system prompt" not in page.text.casefold()
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/branding",
        data={
            "csrf_token": token,
            "version": "0",
            "club_version": str(club_version),
            "action": "save",
            "primary_color": "#123456",
            "secondary_color": "#FFFFFF",
            "accent_colors": ["#ABCDEF", "#abcdef", "#FEDCBA"],
            "graphic_style": "modern",
            "image_effects": ["emotional", "modern"],
            "background_style": "gradient",
            "text_alignment": "center",
            "logo_placement": "top-left",
            "safe_margins": "normal",
            "player_position": "center-right",
            "image_text_amount": "normal",
            "player_background_ratio": "65",
            "dynamics": "balanced",
            "individualization": "club",
            "primary_font_choice": "standard:dejavu-sans",
            "secondary_font_choice": "standard:liberation-serif",
            "address_style": "ihr",
            "tone": "emotional",
            "text_length": "medium",
            "emoji_usage": "sparse",
            "hashtags": ["#Beispiel", "beispiel", "#Heimspiel"],
            "mentions": ["@Test.Konto", "test.konto"],
            "team_names_json": (
                '[{"team_id":"%s","display_name":"Erste Mannschaft",'
                '"short_name":"I","active":true}]' % team_id
            ),
            "home_label": "Heimspiel",
            "away_label": "Auswärtsspiel",
            "home_venue": "Sportpark Beispielstadt",
            "home_venue_short": "Sportpark",
            "cta_type": "support",
            "sponsors_json": "[]",
            "max_hashtags": "10",
            "feed_max_text_amount": "normal",
            "story_safe_top": "12",
            "story_safe_bottom": "15",
            "legacy_image_json": "{}",
            "legacy_text_json": "{}",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with factory() as db:
        config = db.get(ClubBrandingConfiguration, club_id)
        assert config.image_settings["primary_color"] == "#123456"
        assert config.image_settings["accent_colors"] == ["#ABCDEF", "#FEDCBA"]
        assert config.image_settings["image_effects"] == ["emotional", "modern"]
        assert config.image_settings["primary_standard_font"] == "dejavu-sans"
        assert config.image_settings["secondary_standard_font"] == "liberation-serif"
        assert config.primary_font_id is None
        assert config.secondary_font_id is None
        assert config.text_settings["hashtags"] == ["#Beispiel", "#Heimspiel"]
        assert config.text_settings["mentions"] == ["@test.konto"]
        assert config.text_settings["team_names"][0]["team_id"] == team_id


def test_branding_assistant_rejects_unknown_team_and_missing_csrf(browser):
    client, factory = browser
    page = client.get("/branding")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    with factory() as db:
        club = db.scalar(select(Club))
        version = club.version
    common = {
        "version": "0",
        "club_version": str(version),
        "action": "save",
        "primary_color": "#123456",
        "secondary_color": "#FFFFFF",
        "graphic_style": "modern",
        "background_style": "gradient",
        "text_alignment": "left",
        "logo_placement": "top-left",
        "safe_margins": "normal",
        "player_position": "center-right",
        "image_text_amount": "normal",
        "player_background_ratio": "60",
        "dynamics": "balanced",
        "individualization": "club",
        "address_style": "ihr",
        "tone": "emotional",
        "text_length": "medium",
        "emoji_usage": "sparse",
        "team_names_json": '[{"team_id":"fremd","display_name":"Fremd","short_name":"F","active":true}]',
        "sponsors_json": "[]",
        "cta_type": "none",
        "max_hashtags": "10",
        "legacy_image_json": "{}",
        "legacy_text_json": "{}",
    }
    assert client.post("/branding", data=common).status_code == 422
    common["csrf_token"] = token
    response = client.post("/branding", data=common)
    assert response.status_code == 422
    assert "gehört nicht zu diesem Verein" in response.text

    common["team_names_json"] = "[]"
    common["club_logo_id"] = "manipuliertes-logo"
    response = client.post("/branding", data=common)
    assert response.status_code == 422
    assert "Vereinslogo gehört nicht zum Verein" in response.text

    common["club_logo_id"] = ""
    common["sponsors_json"] = (
        '[{"name":"Beispielsponsor","media_asset_id":"fremdes-medium",'
        '"instagram_mention":"","placement":"footer","team_ids":[]}]'
    )
    response = client.post("/branding", data=common)
    assert response.status_code == 422
    assert "Sponsorenmedium gehört nicht zum Verein" in response.text


def test_club_dashboard_shows_usage_and_next_seven_days_in_plain_language(browser):
    client, factory = browser
    scheduled_at = datetime.now(timezone.utc) + timedelta(days=1)
    with factory() as db:
        page = InstagramPage(
            internal_name="dashboard-overview",
            display_name="Dashboard Übersicht",
            username="dashboard_overview",
            club="Dashboard Testverein",
            active=True,
        )
        db.add(page)
        db.flush()
        team = Team(
            internal_name="dashboard-overview",
            display_name="Erste Mannschaft",
            short_name="I",
            slug="dashboard-overview",
            club="Dashboard Testverein",
            fussball_url="https://www.fussball.de/dashboard-overview",
            instagram_page_id=page.id,
            media_subdir="dashboard-overview",
        )
        db.add(team)
        db.flush()
        db.add(
            Team(
                internal_name="dashboard-overview-archived",
                display_name="Archivierte Mannschaft",
                short_name="A",
                slug="dashboard-overview-archived",
                club="Dashboard Testverein",
                active=False,
                fussball_url="https://www.fussball.de/dashboard-overview-archived",
                instagram_page_id=page.id,
                media_subdir="dashboard-overview-archived",
            )
        )
        post = Post(
            team_id=team.id,
            instagram_page_id=page.id,
            post_type="announcement",
            status=PostStatus.APPROVED,
        )
        db.add(post)
        db.flush()
        db.add_all(
            [
                PublicationJob(
                    post_id=post.id,
                    team_id=team.id,
                    instagram_page_id=page.id,
                    kind="feed",
                    media_path="generated/feed.png",
                    scheduled_at=scheduled_at,
                    approval_status="approved",
                    status=JobStatus.SCHEDULED,
                    idempotency_key="dashboard-overview-feed",
                ),
                PublicationJob(
                    post_id=post.id,
                    team_id=team.id,
                    instagram_page_id=page.id,
                    kind="story",
                    media_path="generated/story.png",
                    scheduled_at=scheduled_at,
                    approval_status="unapproved",
                    status=JobStatus.UNAPPROVED,
                    idempotency_key="dashboard-overview-story",
                ),
                StorageObject(
                    provider="local",
                    bucket="dashboard",
                    object_key="clubs/dashboard/generated/feed",
                    category="generated/feed",
                    size_bytes=1_610_612_736,
                    checksum="a" * 64,
                    mime_type="image/png",
                ),
            ]
        )
        db.commit()
        post_id = post.id

    response = client.get("/")
    assert response.status_code == 200
    assert "Geplante Beiträge" in response.text
    assert "Veröffentlichungen" in response.text
    assert "<strong>2</strong><span>Mannschaften</span>" in response.text
    assert re.search(
        r"<strong>1 / \d+</strong>\s*<span>Aktive Mannschaften</span>",
        response.text,
    )
    assert "KI-Textgenerierungen" in response.text
    assert "KI-Bilder" in response.text
    assert "1,50 / 1,00 GB" in response.text
    assert "0 / 20" in response.text
    assert "Geplante Veröffentlichungen" in response.text
    assert "Spielankündigung" in response.text
    assert "Freigegeben" in response.text
    assert "Nicht freigegeben" in response.text
    assert "Geplant" in response.text
    assert f'href="/posts/{post_id}"' in response.text
    assert "Aktuelle Beiträge" not in response.text


def test_suspended_club_keeps_reads_but_blocks_dashboard_mutations(browser):
    client, factory = browser
    with factory() as db:
        club = db.query(Club).one()
        club.status = ClubStatus.SUSPENDED
        db.commit()

    page = client.get("/teams")
    assert page.status_code == 200
    token = re.search(r'name="csrf_token" value="([^"]+)', page.text).group(1)
    blocked = client.post(
        "/teams",
        data={
            "csrf_token": token,
            "internal_name": "gesperrt",
            "display_name": "Gesperrt",
            "short_name": "G",
            "slug": "gesperrt",
            "club": "Testverein",
            "fussball_url": "https://www.fussball.de/gesperrt",
            "instagram_page_id": "unzulässig",
            "media_subdir": "gesperrt",
        },
        follow_redirects=False,
    )
    assert blocked.status_code == 403
    assert "derzeit gesperrt" in blocked.json()["detail"]


def test_setup_pending_club_can_complete_dashboard_setup(browser):
    client, factory = browser
    with factory() as db:
        club = db.query(Club).one()
        club.status = ClubStatus.SETUP_PENDING
        page = InstagramPage(
            internal_name="setup-page",
            display_name="Setup-Seite",
            username="setup-page",
            club="Dashboard Testverein",
            active=True,
            publishing_enabled=False,
        )
        db.add(page)
        db.commit()
        page_id = page.id

    form = client.get("/teams")
    token = re.search(r'name="csrf_token" value="([^"]+)', form.text).group(1)
    response = client.post(
        "/teams",
        data={
            "csrf_token": token,
            "internal_name": "setup-team",
            "display_name": "Setup-Mannschaft",
            "short_name": "Setup",
            "slug": "setup-team",
            "club": "Dashboard Testverein",
            "fussball_url": "https://www.fussball.de/setup-team",
            "instagram_page_id": page_id,
            "media_subdir": "setup-team",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_instagram_meta_test_dashboard_is_explicit_and_blocks_mock_connect(
    browser, monkeypatch
):
    client, factory = browser
    import app.admin_routes as admin_routes
    import app.channels.routes as channel_routes

    monkeypatch.setattr(admin_routes.settings, "environment", "meta-test")
    monkeypatch.setattr(admin_routes.settings, "publisher_mode", "instagram")
    monkeypatch.setitem(
        admin_routes.templates.env.globals, "environment", "meta-test"
    )
    monkeypatch.setattr(channel_routes.settings, "facebook_channel_enabled", True)
    monkeypatch.setattr(channel_routes.settings, "whatsapp_channel_enabled", True)
    with factory() as db:
        page = InstagramPage(
            internal_name="meta-dashboard",
            display_name="SV Ehlen Instagram",
            username="svehlen1901",
            club="SV Ehlen",
            active=True,
            publishing_enabled=False,
            connection_status="connected",
        )
        db.add(page)
        db.flush()
        db.add(
            InstagramConnection(
                instagram_page_id=page.id,
                instagram_user_id="ig-dashboard",
                confirmed_username="svehlen1901",
                account_type="BUSINESS",
                scopes=[
                    "instagram_business_basic",
                    "instagram_business_content_publish",
                ],
                status="connected",
                test_account=True,
                api_version="v23.0",
                token_expires_at=datetime.now(timezone.utc) + timedelta(days=20),
            )
        )
        db.commit()
        page_id = page.id
        version = page.version
    response = client.get("/channels")
    assert response.status_code == 200
    assert "Social-Media-Kanäle" in response.text
    assert "Automatische Veröffentlichungen sind derzeit pausiert" in response.text
    assert "Technische Details" in response.text
    assert "Facebook verbinden" in response.text
    assert "WhatsApp einrichten" in response.text
    assert "Der PlatformAdmin muss" not in response.text
    facebook_setup = client.get("/channels/facebook/setup")
    assert facebook_setup.status_code == 200
    assert "Mit Meta verbinden" in facebook_setup.text
    assert "durch den PlatformAdmin" not in facebook_setup.text
    whatsapp_setup = client.get("/channels/whatsapp/setup")
    assert whatsapp_setup.status_code == 200
    assert "durch den PlatformAdmin" not in whatsapp_setup.text
    token = session_csrf(client)
    blocked = client.post(
        f"/instagram/{page_id}/state",
        data={
            "csrf_token": token,
            "version": version,
            "action": "mock-connect",
        },
    )
    assert blocked.status_code == 422


def test_channel_specific_text_requires_reapproval_and_stays_tenant_bound(browser):
    client, factory = browser
    with factory() as db:
        page = InstagramPage(
            internal_name="channel-text-page",
            display_name="Kanaltext Instagram",
            username="channel_text",
            club="Dashboard Testverein",
            active=True,
            connection_status="connected",
        )
        db.add(page)
        db.flush()
        team = Team(
            internal_name="channel-text-team",
            display_name="Kanaltext Mannschaft",
            short_name="KM",
            slug="channel-text-team",
            club="Dashboard Testverein",
            fussball_url="https://example.invalid/channel-text-team",
            instagram_page_id=page.id,
            media_subdir="channel-text-team/players",
        )
        db.add(team)
        db.flush()
        post = Post(
            team_id=team.id,
            instagram_page_id=page.id,
            post_type="announcement",
            status=PostStatus.APPROVED,
            text="Kurzer Instagram-Text",
            approved_version=1,
        )
        connection = SocialChannelConnection(
            channel_type="facebook",
            internal_name="Facebook",
            display_name="Vereinsseite",
            external_account_id="page-42",
            status="connected",
            active=True,
            publishing_enabled=True,
        )
        db.add_all([post, connection])
        db.flush()
        db.add(
            TeamChannelAssignment(
                team_id=team.id,
                channel_connection_id=connection.id,
                enabled=True,
                announcement_enabled=True,
            )
        )
        db.add(
            PublicationJob(
                post_id=post.id,
                team_id=team.id,
                instagram_page_id=None,
                channel_type="facebook",
                channel_connection_id=connection.id,
                content_type="announcement",
                target="page-42",
                kind="feed",
                media_path="generated/facebook.png",
                text_snapshot=post.text,
                scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
                approval_status="approved",
                status=JobStatus.SCHEDULED,
                approved_post_version=post.version,
                idempotency_key="channel-text-facebook",
            )
        )
        db.commit()
        post_id = post.id
        connection_id = connection.id
        post_version = post.version

    detail = client.get(f"/posts/{post_id}")
    assert detail.status_code == 200
    assert "Vorschau je zusätzlichem Kanal" in detail.text
    assert "Kurzer Instagram-Text" in detail.text

    changed = client.post(
        f"/posts/{post_id}/channels/{connection_id}/text",
        data={
            "csrf_token": session_csrf(client),
            "version": post_version,
            "text": "Ausführlicher Facebook-Text für die Vereinsseite.",
        },
        follow_redirects=False,
    )
    assert changed.status_code == 303
    with factory() as db:
        variant = db.scalar(
            select(PostChannelContent).where(
                PostChannelContent.post_id == post_id,
                PostChannelContent.channel_connection_id == connection_id,
            )
        )
        post = db.get(Post, post_id)
        job = db.scalar(
            select(PublicationJob).where(
                PublicationJob.channel_connection_id == connection_id
            )
        )
        assert variant.text.startswith("Ausführlicher Facebook-Text")
        assert post.status == PostStatus.REAPPROVAL
        assert post.approved_version is None
        assert job.status == JobStatus.UNAPPROVED
        assert job.text_snapshot == variant.text


def logo_png(color=(20, 90, 200, 255)):
    buffer = BytesIO()
    image = Image.new("RGBA", (160, 160), color)
    ImageDraw.Draw(image).rectangle((35, 35, 125, 125), fill=(255, 255, 255, 255))
    image.save(buffer, "PNG")
    return buffer.getvalue()


def player_image(image_format="JPEG", color=(20, 90, 200)):
    buffer = BytesIO()
    image = Image.new("RGB", (900, 1200), color)
    ImageDraw.Draw(image).rectangle((250, 120, 650, 1100), fill=(240, 240, 240))
    image.save(buffer, image_format)
    return buffer.getvalue()


def manual_post_image(size=(1080, 1350)):
    buffer = BytesIO()
    image = Image.new("RGB", size, (16, 48, 112))
    ImageDraw.Draw(image).rectangle((120, 160, 960, 1120), fill=(235, 210, 40))
    image.save(buffer, "PNG")
    return buffer.getvalue()


def test_publication_plan_shows_recent_and_adjustable_upcoming_windows(browser):
    client, factory = browser
    current_time = datetime.now(timezone.utc)
    with factory() as db:
        page = InstagramPage(
            internal_name="publication-plan",
            display_name="SV Ehlen Instagram",
            username="svehlen1901",
            club="SV Ehlen",
            active=True,
        )
        db.add(page)
        db.flush()
        team = Team(
            internal_name="publication-plan-team",
            display_name="SV Ehlen I",
            short_name="SVE",
            slug="publication-plan-team",
            club="SV Ehlen",
            fussball_url="https://www.fussball.de/publication-plan",
            instagram_page_id=page.id,
            media_subdir="publication-plan",
        )
        db.add(team)
        db.flush()
        game = Game(
            team_id=team.id,
            provider="fixture",
            external_id="publication-plan-game",
            home_team="SV Ehlen I",
            away_team="TSV Kalender",
            kickoff=current_time + timedelta(days=3),
            competition="Kreisliga A",
            status="scheduled",
            source_url="fixture://publication-plan",
        )
        db.add(game)
        db.flush()

        recent_post = Post(
            game_id=game.id,
            team_id=team.id,
            instagram_page_id=page.id,
            post_type="announcement",
            status=PostStatus.PUBLISHED,
            text="Jüngster veröffentlichter Spielbeitrag",
        )
        old_post = Post(
            game_id=game.id,
            team_id=team.id,
            instagram_page_id=page.id,
            post_type="result",
            status=PostStatus.PUBLISHED,
            text="Historischer Beitrag außerhalb des Rückblicks",
        )
        story_post = Post(
            team_id=team.id,
            instagram_page_id=page.id,
            post_type="manual",
            manual_submission_id="publication-plan-story",
            status=PostStatus.APPROVED,
            text="Geplante Story im Standardzeitraum",
        )
        attention_post = Post(
            team_id=team.id,
            instagram_page_id=page.id,
            post_type="manual",
            manual_submission_id="publication-plan-attention",
            status=PostStatus.PENDING,
            text="Feed benötigt noch Freigabe",
        )
        overdue_post = Post(
            team_id=team.id,
            instagram_page_id=page.id,
            post_type="manual",
            manual_submission_id="publication-plan-overdue",
            status=PostStatus.APPROVED,
            text="Überfälliger manueller Beitrag",
        )
        carousel_post = Post(
            team_id=team.id,
            instagram_page_id=page.id,
            post_type="manual",
            manual_submission_id="publication-plan-carousel",
            status=PostStatus.APPROVED,
            text="Karussell im erweiterten Zeitraum",
        )
        cancelled_post = Post(
            team_id=team.id,
            instagram_page_id=page.id,
            post_type="manual",
            manual_submission_id="publication-plan-cancelled",
            status=PostStatus.CANCELLED,
            text="Abgebrochener Zukunftsbeitrag",
        )
        db.add_all(
            [
                recent_post,
                old_post,
                story_post,
                attention_post,
                overdue_post,
                carousel_post,
                cancelled_post,
            ]
        )
        db.flush()

        recent_time = current_time - timedelta(hours=12)
        old_time = current_time - timedelta(days=3)
        story_time = current_time + timedelta(days=2)
        attention_time = current_time + timedelta(days=4)
        overdue_time = current_time - timedelta(minutes=30)
        carousel_time = current_time + timedelta(days=8)
        cancelled_time = current_time + timedelta(days=1)
        jobs = [
            PublicationJob(
                post_id=recent_post.id,
                game_id=game.id,
                team_id=team.id,
                instagram_page_id=page.id,
                kind="feed",
                media_path="/tmp/recent-feed.png",
                scheduled_at=recent_time - timedelta(hours=1),
                approval_status="approved",
                status=JobStatus.PUBLISHED,
                published_at=recent_time,
                permalink="https://www.instagram.com/p/recent",
                idempotency_key="publication-plan-recent",
            ),
            PublicationJob(
                post_id=old_post.id,
                game_id=game.id,
                team_id=team.id,
                instagram_page_id=page.id,
                kind="feed",
                media_path="/tmp/old-feed.png",
                scheduled_at=old_time,
                approval_status="approved",
                status=JobStatus.PUBLISHED,
                published_at=old_time,
                idempotency_key="publication-plan-old",
            ),
            PublicationJob(
                post_id=story_post.id,
                team_id=team.id,
                instagram_page_id=page.id,
                kind="story",
                media_path="/tmp/planned-story.png",
                scheduled_at=story_time,
                approval_status="approved",
                status=JobStatus.SCHEDULED,
                idempotency_key="publication-plan-story",
            ),
            PublicationJob(
                post_id=attention_post.id,
                team_id=team.id,
                instagram_page_id=page.id,
                kind="feed",
                media_path="/tmp/attention-feed.png",
                scheduled_at=attention_time,
                approval_status="unapproved",
                status=JobStatus.UNAPPROVED,
                idempotency_key="publication-plan-attention",
            ),
            PublicationJob(
                post_id=overdue_post.id,
                team_id=team.id,
                instagram_page_id=page.id,
                kind="feed",
                media_path="/tmp/overdue-feed.png",
                scheduled_at=overdue_time,
                approval_status="approved",
                status=JobStatus.SCHEDULED,
                idempotency_key="publication-plan-overdue",
            ),
            PublicationJob(
                post_id=carousel_post.id,
                team_id=team.id,
                instagram_page_id=page.id,
                kind="carousel",
                media_path="/tmp/carousel-cover.png",
                scheduled_at=carousel_time,
                approval_status="approved",
                status=JobStatus.SCHEDULED,
                idempotency_key="publication-plan-carousel",
            ),
            PublicationJob(
                post_id=cancelled_post.id,
                team_id=team.id,
                instagram_page_id=page.id,
                kind="feed",
                media_path="/tmp/cancelled-feed.png",
                scheduled_at=cancelled_time,
                approval_status="rejected",
                status=JobStatus.CANCELLED,
                idempotency_key="publication-plan-cancelled",
            ),
        ]
        db.add_all(jobs)
        db.flush()
        carousel_job = jobs[5]
        for position in range(1, 4):
            db.add(
                PublicationMediaItem(
                    publication_job_id=carousel_job.id,
                    position=position,
                    media_path=f"/tmp/carousel-{position}.png",
                    checksum=str(position) * 64,
                    file_size=1024,
                    width=1080,
                    height=1350,
                )
            )
        db.commit()
        attention_post_id = attention_post.id
        story_post_id = story_post.id
        story_post_version = story_post.version

    default_page = client.get("/posts")
    assert default_page.status_code == 200
    assert "Zentraler Veröffentlichungsplan" in default_page.text
    assert "In den letzten 2 Tagen veröffentlicht" in default_page.text
    assert "Geplant für die nächsten 7 Tage" in default_page.text
    assert "Überfällige Veröffentlichungen" in default_page.text
    assert "Überfälliger manueller Beitrag" in default_page.text
    assert "Überfällig – noch nicht veröffentlicht" in default_page.text
    assert "Jüngster veröffentlichter Spielbeitrag" in default_page.text
    assert "Geplante Story im Standardzeitraum" in default_page.text
    assert "Feed benötigt noch Freigabe" in default_page.text
    assert "Historischer Beitrag außerhalb des Rückblicks" not in default_page.text
    assert "Abgebrochener Zukunftsbeitrag" not in default_page.text
    assert berlin_datetime(old_time) not in default_page.text
    assert berlin_datetime(carousel_time) not in default_page.text
    assert "Freigabe: unapproved" in default_page.text

    extended_page = client.get("/posts?days=14")
    assert extended_page.status_code == 200
    assert "Geplant für die nächsten 14 Tage" in extended_page.text
    assert berlin_datetime(carousel_time) in extended_page.text
    assert "Karussell" in extended_page.text
    assert "3 Bilder" in extended_page.text
    assert extended_page.text.count("Karussellbild") == 3
    assert extended_page.text.count('width="62" height="92"') == 3
    assert 'class="publication-preview"' in extended_page.text
    assert 'width="92" height="116"' in extended_page.text

    story_page = client.get("/posts?days=14&format=story")
    assert story_page.status_code == 200
    assert story_page.text.count('data-publication-kind="story"') == 1
    assert 'data-publication-kind="feed"' not in story_page.text
    assert 'data-publication-kind="carousel"' not in story_page.text
    assert '<option value="story" selected>' in story_page.text

    feed_page = client.get("/posts?days=14&format=feed")
    assert feed_page.status_code == 200
    assert feed_page.text.count('data-publication-kind="feed"') == 3
    assert feed_page.text.count('data-publication-kind="carousel"') == 1
    assert 'data-publication-kind="story"' not in feed_page.text
    assert '<option value="feed" selected>' in feed_page.text

    assert client.get("/posts?days=0").status_code == 422
    assert client.get("/posts?days=91").status_code == 422
    assert client.get("/posts?format=video").status_code == 422

    token = session_csrf(client)
    rejected = client.post(
        f"/posts/{attention_post_id}/reject",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert rejected.status_code == 303
    deleted = client.post(
        f"/posts/{story_post_id}/delete",
        data={
            "csrf_token": token,
            "version": story_post_version,
            "confirmation": "BEITRAG LÖSCHEN",
        },
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    with factory() as db:
        assert db.get(Post, story_post_id) is None
        rejected_post = db.get(Post, attention_post_id)
        assert rejected_post.status == PostStatus.REJECTED
        rejection_audit = db.query(AuditLog).filter_by(action="post.rejected").one()
        assert rejection_audit.details["reason"] is None


def test_manual_post_can_be_uploaded_and_scheduled_from_dashboard(
    browser, tmp_path, monkeypatch
):
    client, factory = browser
    import app.admin_routes as admin_routes

    monkeypatch.setattr(admin_routes.settings, "generated_root", tmp_path / "generated")
    monkeypatch.setattr(admin_routes.settings, "media_root", tmp_path / "media")
    monkeypatch.setattr(admin_routes.settings, "upload_root", tmp_path / "uploads")
    with factory() as db:
        page = InstagramPage(
            internal_name="manual-dashboard-page",
            display_name="Manuelle Beiträge",
            username="manualdashboard",
            club="SV Ehlen",
            active=True,
            connection_status="connected",
        )
        db.add(page)
        db.flush()
        team = Team(
            internal_name="manual-dashboard-team",
            display_name="SV Ehlen I",
            short_name="SVE",
            slug="manual-dashboard-team",
            club="SV Ehlen",
            fussball_url="https://www.fussball.de/manual-dashboard",
            instagram_page_id=page.id,
            media_subdir="erste_mannschaft/spieler",
            timezone="Europe/Berlin",
        )
        db.add(team)
        db.commit()
        team_id = team.id

    form = client.get("/posts/manual/new")
    assert form.status_code == 200
    assert "Beitrag manuell erstellen" in form.text
    assert "manual-crop-canvas" in form.text
    assert "Zoom" in form.text
    assert "Instagram-Konten im Bild markieren" in form.text
    assert 'name="user_tags"' in form.text
    csrf_token = re.search(r'name="csrf_token" value="([^"]+)', form.text).group(1)
    submission_id = re.search(
        r'name="submission_id" value="([^"]+)', form.text
    ).group(1)
    local_publish_at = (
        datetime.now(ZoneInfo("Europe/Berlin")) + timedelta(hours=2)
    ).strftime("%Y-%m-%dT%H:%M")
    data = {
        "csrf_token": csrf_token,
        "submission_id": submission_id,
        "team_id": team_id,
        "kind": "feed",
        "text": "Heute gibt es Neuigkeiten direkt aus dem Verein.",
        "scheduled_at": local_publish_at,
        "crop_specs": '[{"x":0.2,"y":0,"width":0.6,"height":1}]',
        "user_tags": '[[{"username":"@testverein.kassel","x":0.3,"y":0.4}]]',
    }
    blocked = client.post(
        "/posts/manual/new",
        data={**data, "csrf_token": "wrong"},
        files={"images": ("beitrag.png", manual_post_image((1600, 1200)), "image/png")},
    )
    assert blocked.status_code == 403

    response = client.post(
        "/posts/manual/new",
        data=data,
        files={"images": ("beitrag.png", manual_post_image((1600, 1200)), "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with factory() as db:
        post = db.query(Post).filter_by(manual_submission_id=submission_id).one()
        job = db.query(PublicationJob).filter_by(post_id=post.id).one()
        assert post.game_id is None
        assert post.post_type == "manual"
        assert post.text == data["text"]
        assert post.design_snapshot["source"] == "manual_upload"
        uploaded = post.design_snapshot["manual_upload"]["images"][0]
        assert uploaded["source_width"] == 1600
        assert uploaded["source_height"] == 1200
        assert uploaded["crop"] == {
            "x": 0.2,
            "y": 0.0,
            "width": 0.6,
            "height": 1.0,
        }
        assert uploaded["user_tags"] == [
            {"username": "testverein.kassel", "x": 0.3, "y": 0.4}
        ]
        assert Path(uploaded["original_path"]).read_bytes() == manual_post_image(
            (1600, 1200)
        )
        assert job.game_id is None
        assert job.kind == "feed"
        assert job.status == JobStatus.UNAPPROVED
        assert Path(job.media_path).is_file()
        assert db.query(AuditLog).filter_by(action="manual_post.created").count() == 1
        post_id = post.id

    detail = client.get(f"/posts/{post_id}")
    assert detail.status_code == 200
    assert "Manuell erstellter Beitrag" in detail.text
    assert "@testverein.kassel" in detail.text
    assert "30 % von links / 40 % von oben" in detail.text
    assert "Grafiken neu erzeugen" not in detail.text
    rerender = client.post(
        f"/posts/{post_id}/rerender",
        data={"csrf_token": csrf_token, "version": 1},
    )
    assert rerender.status_code == 422

    carousel_form = client.get("/posts/manual/new")
    carousel_csrf = re.search(
        r'name="csrf_token" value="([^"]+)', carousel_form.text
    ).group(1)
    carousel_submission = re.search(
        r'name="submission_id" value="([^"]+)', carousel_form.text
    ).group(1)
    carousel = client.post(
        "/posts/manual/new",
        data={
            "csrf_token": carousel_csrf,
            "submission_id": carousel_submission,
            "team_id": team_id,
            "kind": "carousel",
            "text": "Ein gemeinsamer Text für alle drei Bilder.",
            "scheduled_at": local_publish_at,
        },
        files=[
            ("images", ("drittes.png", manual_post_image(), "image/png")),
            ("images", ("erstes.png", manual_post_image(), "image/png")),
            ("images", ("zweites.png", manual_post_image(), "image/png")),
        ],
        follow_redirects=False,
    )
    assert carousel.status_code == 303
    with factory() as db:
        carousel_post = db.query(Post).filter_by(
            manual_submission_id=carousel_submission
        ).one()
        carousel_job = db.query(PublicationJob).filter_by(
            post_id=carousel_post.id
        ).one()
        media = db.query(PublicationMediaItem).filter_by(
            publication_job_id=carousel_job.id
        ).order_by(PublicationMediaItem.position).all()
        assert carousel_job.kind == "carousel"
        assert carousel_job.text_snapshot == carousel_post.text
        assert [item.position for item in media] == [1, 2, 3]
        assert [
            image["original_filename"]
            for image in carousel_post.design_snapshot["manual_upload"]["images"]
        ] == ["drittes.png", "erstes.png", "zweites.png"]
        carousel_post_id = carousel_post.id
    carousel_detail = client.get(f"/posts/{carousel_post_id}")
    assert carousel_detail.status_code == 200
    assert "KARUSSELL 1/3" in carousel_detail.text
    assert "KARUSSELL 3/3" in carousel_detail.text


def player_image_archive(entries: dict[str, bytes]):
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_player_images_can_be_uploaded_from_dashboard(browser, tmp_path, monkeypatch):
    client, factory = browser
    media_root = tmp_path / "external-media"
    upload_root = tmp_path / "uploads"
    (media_root / "erste_mannschaft" / "spieler").mkdir(parents=True)
    import app.admin_routes as admin_routes

    monkeypatch.setattr(admin_routes.settings, "media_root", media_root)
    monkeypatch.setattr(admin_routes.settings, "upload_root", upload_root)
    with factory() as db:
        page = InstagramPage(
            internal_name="player-upload",
            display_name="Player Upload",
            username="player-upload",
            club="SV Ehlen",
            active=True,
            connection_status="connected",
        )
        db.add(page)
        db.flush()
        team = Team(
            internal_name="upload-team",
            display_name="SV Ehlen",
            short_name="SVE",
            slug="upload-team",
            club="SV Ehlen",
            fussball_url="https://www.fussball.de/team",
            instagram_page_id=page.id,
            media_subdir="erste_mannschaft/spieler",
        )
        db.add(team)
        db.commit()
        team_id = team.id

    token = session_csrf(client)
    assert client.post(
        f"/media/{team_id}/upload",
        data={"csrf_token": "wrong"},
        files={"files": ("spieler.jpg", player_image(), "image/jpeg")},
    ).status_code == 403
    response = client.post(
        f"/media/{team_id}/upload",
        data={"csrf_token": token},
        files=[
            ("files", ("Max_Mustermann.jpg", player_image(), "image/jpeg")),
            ("files", ("Erika-Musterfrau.png", player_image("PNG"), "image/png")),
        ],
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "2%20Spielerbilder%20hochgeladen" in response.headers["location"]

    with factory() as db:
        assets = db.query(MediaAsset).filter_by(team_id=team_id).order_by(MediaAsset.filename).all()
        assert len(assets) == 2
        assert {asset.storage_kind for asset in assets} == {"upload"}
        assert {asset.player_name for asset in assets} == {
            "Erika Musterfrau",
            "Max Mustermann",
        }
        for asset in assets:
            assert not Path(asset.relative_path).is_absolute()
            assert (upload_root / asset.relative_path).is_file()
        asset_id = assets[0].id
        import app.posts.service as post_service

        monkeypatch.setattr(post_service, "get_settings", lambda: admin_routes.settings)
        assert Path(post_service._media_path(assets[0])).is_relative_to(upload_root)
        assert db.query(AuditLog).filter_by(action="media.player_images_uploaded").count() == 1

    preview = client.get(f"/media/{asset_id}/preview")
    assert preview.status_code == 200
    assert preview.headers["content-type"] in {"image/jpeg", "image/png"}
    page = client.get(f"/media?team_id={team_id}")
    assert page.status_code == 200
    assert "Spielerbilder hochladen" in page.text
    assert "Dashboard-Upload" in page.text
    assert f'/media/{asset_id}/preview' in page.text

    duplicate = client.post(
        f"/media/{team_id}/upload",
        data={"csrf_token": token},
        files={"files": ("Kopie.jpg", player_image(), "image/jpeg")},
        follow_redirects=False,
    )
    assert duplicate.status_code == 303
    with factory() as db:
        assert db.query(MediaAsset).filter_by(team_id=team_id).count() == 2

    scan = client.post(
        f"/media/{team_id}/scan",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert scan.status_code == 303
    with factory() as db:
        assert all(
            asset.available
            for asset in db.query(MediaAsset).filter_by(
                team_id=team_id, storage_kind="upload"
            )
        )


def test_player_images_can_be_uploaded_as_safe_zip_archive(browser, tmp_path, monkeypatch):
    client, factory = browser
    upload_root = tmp_path / "uploads"
    import app.admin_routes as admin_routes

    monkeypatch.setattr(admin_routes.settings, "upload_root", upload_root)
    with factory() as db:
        page = InstagramPage(
            internal_name="player-zip-upload",
            display_name="Player ZIP Upload",
            username="player-zip-upload",
            club="SV Ehlen",
            active=True,
        )
        db.add(page)
        db.flush()
        team = Team(
            internal_name="zip-upload-team",
            display_name="SV Ehlen",
            short_name="SVE",
            slug="zip-upload-team",
            club="SV Ehlen",
            fussball_url="https://www.fussball.de/team",
            instagram_page_id=page.id,
            media_subdir="erste",
        )
        db.add(team)
        db.commit()
        team_id = team.id

    archive = player_image_archive(
        {
            f"mannschaft/SVE_{number:02d}.png": player_image(
                "PNG", color=(number * 10, 90, 200)
            )
            for number in range(25)
        }
    )
    response = client.post(
        f"/media/{team_id}/upload",
        data={"csrf_token": session_csrf(client)},
        files={"archive": ("spielerbilder.zip", archive, "application/zip")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "25%20Spielerbilder%20hochgeladen" in response.headers["location"]
    with factory() as db:
        assets = db.query(MediaAsset).filter_by(team_id=team_id).all()
        assert len(assets) == 25
        assert {asset.storage_kind for asset in assets} == {"upload"}
        assert all((upload_root / asset.relative_path).is_file() for asset in assets)

    unsafe_archive = player_image_archive({"../escape.jpg": player_image()})
    rejected = client.post(
        f"/media/{team_id}/upload",
        data={"csrf_token": session_csrf(client)},
        files={"archive": ("unsicher.zip", unsafe_archive, "application/zip")},
    )
    assert rejected.status_code == 422
    assert "unsicherer Pfad" in rejected.json()["detail"]
    assert not (tmp_path / "escape.jpg").exists()
    with factory() as db:
        assert db.query(MediaAsset).filter_by(team_id=team_id).count() == 25

    page = client.get(f"/media?team_id={team_id}")
    assert "Alternativ ZIP-Archiv auswählen" in page.text


def test_nginx_allows_large_requests_only_for_player_image_uploads():
    config = (
        Path(__file__).parents[1] / "deploy" / "nginx" / "default.conf"
    ).read_text(encoding="utf-8")
    assert "client_max_body_size 20m;" in config
    assert "location ~ ^/media/[A-Za-z0-9_-]+/upload$" in config
    assert "client_max_body_size 512m;" in config


def test_nginx_proxies_refresh_web_container_address_via_docker_dns():
    nginx_root = Path(__file__).parents[1] / "deploy" / "nginx"
    for filename, expected_proxy_count in (
        ("default.conf", 3),
        ("meta-public.conf", 5),
    ):
        config = (nginx_root / filename).read_text(encoding="utf-8")
        assert "resolver 127.0.0.11 ipv6=off valid=10s;" in config
        assert "resolver_timeout 5s;" in config
        assert "set $web_backend web:8000;" in config
        assert config.count("proxy_pass http://$web_backend;") == expected_proxy_count
        assert "proxy_pass http://web:8000;" not in config


def test_nginx_proxy_healthcheck_uses_ipv4_loopback():
    compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    assert "http://127.0.0.1/health" in compose
    assert "http://localhost/health" not in compose
    assert "start_period: 20s" in compose


def test_player_image_upload_rejects_fake_images(browser, tmp_path, monkeypatch):
    client, factory = browser
    import app.admin_routes as admin_routes

    monkeypatch.setattr(admin_routes.settings, "upload_root", tmp_path / "uploads")
    with factory() as db:
        page = InstagramPage(
            internal_name="invalid-player-upload",
            display_name="Invalid Player Upload",
            username="invalid-player-upload",
            club="SV Ehlen",
            active=True,
        )
        db.add(page)
        db.flush()
        team = Team(
            internal_name="invalid-upload-team",
            display_name="SV Ehlen",
            short_name="SVE",
            slug="invalid-upload-team",
            club="SV Ehlen",
            fussball_url="https://www.fussball.de/team",
            instagram_page_id=page.id,
            media_subdir="erste",
        )
        db.add(team)
        db.commit()
        team_id = team.id
    response = client.post(
        f"/media/{team_id}/upload",
        data={"csrf_token": session_csrf(client)},
        files={"files": ("kein-bild.jpg", b"not-an-image", "image/jpeg")},
    )
    assert response.status_code == 422
    assert "keine technisch lesbare Bilddatei" in response.json()["detail"]
    with factory() as db:
        assert db.query(MediaAsset).filter_by(team_id=team_id).count() == 0


def test_team_and_per_game_opponent_logo_workflow(browser, tmp_path, monkeypatch):
    client, factory = browser
    upload_root = tmp_path / "uploads"
    monkeypatch.setattr(
        __import__("app.admin_routes", fromlist=["settings"]).settings,
        "upload_root",
        upload_root,
    )
    with factory() as db:
        admin = db.query(User).filter_by(email="admin@test.invalid").one()
        page = InstagramPage(
            internal_name="logos",
            display_name="Logos",
            username="logos",
            club="SV Ehlen",
            active=True,
            connection_status="connected",
        )
        db.add(page)
        db.flush()
        team = Team(
            internal_name="logo-team",
            display_name="SV Ehlen",
            short_name="SVE",
            slug="logo-team",
            club="SV Ehlen",
            fussball_url="https://www.fussball.de/team",
            instagram_page_id=page.id,
            media_subdir="erste",
        )
        db.add(team)
        db.flush()
        game = Game(
            team_id=team.id,
            external_id="logo-dashboard-1",
            home_team=team.display_name,
            away_team="TSV Immenhausen II",
            kickoff=datetime.now(timezone.utc) + timedelta(days=5),
            competition="Kreisliga A",
            venue="Ehlen",
            pitch="Rasenplatz",
            source_url="fixture://logo-dashboard",
        )
        second = Game(
            team_id=team.id,
            external_id="logo-dashboard-2",
            home_team="TSV Immenhausen II",
            away_team=team.display_name,
            kickoff=datetime.now(timezone.utc) + timedelta(days=12),
            competition="Kreisliga A",
            venue="Immenhausen",
            pitch="Rasenplatz",
            source_url="fixture://logo-dashboard",
        )
        db.add_all([game, second])
        db.commit()
        team_id, game_id, second_id, admin_id = team.id, game.id, second.id, admin.id
    token = session_csrf(client)
    assert client.post(
        f"/teams/{team_id}/logo",
        data={"csrf_token": "wrong"},
        files={"file": ("sve.png", logo_png(), "image/png")},
    ).status_code == 403
    uploaded = client.post(
        f"/teams/{team_id}/logo",
        data={"csrf_token": token},
        files={"file": ("sve.png", logo_png(), "image/png")},
        follow_redirects=False,
    )
    assert uploaded.status_code == 303
    teams_page = client.get("/teams").text
    assert "verifiziert" in teams_page
    assert 'class="logo-thumb" width="88" height="88"' in teams_page
    assert "/static/style.css?v=20260806-matchday-bundles" in teams_page
    management = client.get(f"/games/{game_id}/opponent-logo")
    assert management.status_code == 200
    assert "neutraler Text-Fallback" in management.text
    opponent_upload = client.post(
        f"/games/{game_id}/opponent-logo",
        data={"csrf_token": token, "action": "upload"},
        files={"file": ("tsv.png", logo_png((0, 150, 60, 255)), "image/png")},
        follow_redirects=False,
    )
    assert opponent_upload.status_code == 303
    opponent_page = client.get(f"/games/{game_id}/opponent-logo").text
    assert 'class="logo-preview" width="240" height="240"' in opponent_page
    second_version = client.post(
        f"/games/{game_id}/opponent-logo",
        data={"csrf_token": token, "action": "upload"},
        files={"file": ("tsv-v2.png", logo_png((180, 80, 10, 255)), "image/png")},
        follow_redirects=False,
    )
    assert second_version.status_code == 303
    with factory() as db:
        first = db.get(Game, game_id)
        second = db.get(Game, second_id)
        logo = db.get(LogoAsset, first.opponent_logo_id)
        assert logo and logo.uploaded_by == admin_id
        assert second.opponent_logo_id is None
        logo_id = logo.id
    suggestion = client.get(f"/games/{second_id}/opponent-logo")
    assert suggestion.text.count("Vorschlag ausdrücklich bestätigen") == 2
    assert "Passende Logos aus der systemweiten Bibliothek" in suggestion.text
    with factory() as db:
        assert db.get(Game, second_id).opponent_logo_id is None
        shared_logo = db.query(SharedOpponentLogo).order_by(
            SharedOpponentLogo.catalog_version.desc()
        ).first()
        shared_logo_id = shared_logo.id
        original_filename = shared_logo.original_filename
    shared_preview = client.get(
        f"/shared-opponent-logos/{shared_logo_id}/preview?game_id={second_id}"
    )
    assert shared_preview.status_code == 200
    assert original_filename not in shared_preview.headers["content-disposition"]
    assigned = client.post(
        f"/games/{second_id}/opponent-logo",
        data={"csrf_token": token, "action": "select", "logo_id": logo_id},
        follow_redirects=False,
    )
    assert assigned.status_code == 303
    with factory() as db:
        second = db.get(Game, second_id)
        assert second.opponent_logo_id == logo_id
        team = db.get(Team, team_id)
        post = Post(
            game_id=second.id,
            team_id=team.id,
            instagram_page_id=team.instagram_page_id,
            post_type="announcement",
            status=__import__("app.models", fromlist=["PostStatus"]).PostStatus.PENDING,
            text="Legacy",
            feed_path=str(tmp_path / "legacy-feed.png"),
            design_snapshot={"logos": {}},
        )
        db.add(post)
        db.flush()
        db.add(
            PublicationJob(
                post_id=post.id,
                game_id=second.id,
                team_id=team.id,
                instagram_page_id=team.instagram_page_id,
                kind="feed",
                media_path=post.feed_path,
                scheduled_at=second.kickoff,
                idempotency_key=f"{post.id}:feed:v1",
            )
        )
        db.commit()
        post_id, post_version = post.id, post.version
    legacy_page = client.get(f"/posts/{post_id}").text
    assert "Lokale Logo-Neuzusammensetzung ist für diesen älteren Beitrag nicht möglich" in legacy_page
    blocked = client.post(
        f"/posts/{post_id}/recompose-logos",
        data={"csrf_token": token, "version": post_version},
        follow_redirects=False,
    )
    assert blocked.status_code == 409
    assert "Grafiken neu erzeugen" in blocked.json()["detail"]


def test_games_dashboard_groups_and_consciously_splits_or_connects_matchday(browser):
    client, factory = browser
    with factory() as db:
        page = InstagramPage(
            internal_name="bundle-dashboard",
            display_name="Bündel-Seite",
            username="bundle_dashboard",
            club="Bündelverein",
            active=True,
        )
        db.add(page)
        db.flush()
        teams = []
        games = []
        for number, hour in ((1, 11), (2, 13)):
            team = Team(
                internal_name=f"bundle-{number}",
                display_name=f"Bündelverein {number}",
                short_name=f"BV {number}",
                slug=f"bundle-{number}",
                club="Bündelverein",
                fussball_url=f"https://example.invalid/bundle-{number}",
                instagram_page_id=page.id,
                media_subdir=f"bundle-{number}",
                rules={
                    "announcement_enabled": True,
                    "result_enabled": True,
                    "club_matchday_feed_mode": "announcements_and_results",
                },
            )
            db.add(team)
            db.flush()
            game = Game(
                team_id=team.id,
                provider="mock",
                external_id=f"bundle-dashboard-{number}",
                home_team=team.display_name,
                away_team=f"Gegner {number}",
                kickoff=datetime(2026, 8, 16, hour, tzinfo=timezone.utc),
                competition="Kreisliga",
                venue="Sportplatz",
                pitch="Rasenplatz",
                source_url=f"fixture://bundle-dashboard-{number}",
            )
            db.add(game)
            teams.append(team)
            games.append(game)
        db.commit()
        game_ids = [item.id for item in games]

    page = client.get("/games")
    assert page.status_code == 200
    assert page.text.count("Gemeinsame Ankündigung erzeugen") == 1
    assert "durch Vereinsregel gebündelt" in page.text
    grouped_result = re.search(
        r'<button name="post_type" value="result"([^>]*)>Gemeinsames Ergebnis erzeugen</button>',
        page.text,
    )
    assert grouped_result and "disabled" in grouped_result.group(1)

    with factory() as db:
        for game_id in game_ids:
            game = db.get(Game, game_id)
            game.home_score = 1
            game.away_score = 0
            game.result_confirmed = True
        db.commit()
    confirmed_page = client.get("/games").text
    grouped_result = re.search(
        r'<button name="post_type" value="result"([^>]*)>Gemeinsames Ergebnis erzeugen</button>',
        confirmed_page,
    )
    assert grouped_result and "disabled" not in grouped_result.group(1)

    token = session_csrf(client)
    separated = client.post(
        "/games/bundles/separate",
        data={"csrf_token": token, "game_ids": game_ids},
        follow_redirects=False,
    )
    assert separated.status_code == 303
    separated_page = client.get("/games").text
    assert "Gemeinsame Ankündigung erzeugen" not in separated_page

    connected = client.post(
        "/games/bundles/connect",
        data={"csrf_token": token, "game_ids": game_ids},
        follow_redirects=False,
    )
    assert connected.status_code == 303
    connected_page = client.get("/games").text
    assert connected_page.count("Gemeinsame Ankündigung erzeugen") == 1
    assert "bewusst verbunden" in connected_page


def test_matchday_post_page_shows_both_feeds_and_all_four_stories(browser, tmp_path):
    client, factory = browser
    with factory() as db:
        page = InstagramPage(
            internal_name="combined-post-dashboard",
            display_name="Gemeinsame Seite",
            username="combined_dashboard",
            club="Dashboard Testverein",
            active=True,
        )
        db.add(page)
        db.flush()
        posts = []
        teams = []
        for number, hour in ((1, 13), (2, 15)):
            team = Team(
                internal_name=f"combined-team-{number}",
                display_name=f"Dashboard Mannschaft {number}",
                short_name=f"DM {number}",
                slug=f"combined-team-{number}",
                club="Dashboard Testverein",
                fussball_url=f"https://example.invalid/combined-{number}",
                instagram_page_id=page.id,
                media_subdir=f"combined-team-{number}",
                rules={
                    "announcement_enabled": True,
                    "club_matchday_feed_mode": "announcements",
                },
            )
            db.add(team)
            db.flush()
            teams.append(team)
            game = Game(
                team_id=team.id,
                provider="mock",
                external_id=f"combined-dashboard-{number}",
                home_team=team.display_name,
                away_team=f"Gegner {number}",
                kickoff=datetime(2026, 8, 16, hour, tzinfo=timezone.utc),
                competition="Kreisliga",
                venue="Sportplatz",
                pitch="Rasenplatz",
                source_url=f"fixture://combined-dashboard-{number}",
            )
            db.add(game)
            db.flush()
            feed_path = tmp_path / f"combined-feed-{number}.png"
            Image.new("RGB", (1080, 1350), "blue").save(feed_path)
            post = Post(
                game_id=game.id,
                team_id=team.id,
                instagram_page_id=page.id,
                post_type="announcement",
                status=PostStatus.PENDING,
                text=f"Gemeinsamer Spieltag {number}",
                feed_path=str(feed_path),
                design_snapshot={},
                critical_warnings=[],
            )
            db.add(post)
            db.flush()
            db.add(
                PublicationJob(
                    post_id=post.id,
                    game_id=game.id,
                    team_id=team.id,
                    instagram_page_id=page.id,
                    kind="feed",
                    media_path=str(feed_path),
                    scheduled_at=game.kickoff - timedelta(days=2),
                    idempotency_key=f"{post.id}:feed:v1",
                )
            )
            for slot in (1, 2):
                story_path = tmp_path / f"combined-story-{number}-{slot}.png"
                Image.new("RGB", (1080, 1920), "navy").save(story_path)
                db.add(
                    PublicationJob(
                        post_id=post.id,
                        game_id=game.id,
                        team_id=team.id,
                        instagram_page_id=page.id,
                        kind="story",
                        media_path=str(story_path),
                        scheduled_at=game.kickoff - timedelta(hours=slot),
                        idempotency_key=f"{post.id}:story:{slot}:v1",
                    )
                )
            posts.append(post)
        db.commit()
        state = coordinate_club_matchday_feed(db, posts[-1], requested_by=None)
        db.commit()
        primary_id = state.primary_post_id
        member_id = next(post.id for post in posts if post.id != primary_id)
        team_ids = [team.id for team in teams]
        carousel_version = db.query(PublicationJob).filter_by(
            post_id=primary_id,
            kind="carousel",
        ).one().version

    response = client.get(f"/posts/{primary_id}")

    assert response.status_code == 200
    assert "Gemeinsamer Spieltagsbeitrag" in response.text
    assert response.text.count("<figcaption>KARUSSELL") == 2
    assert response.text.count("<figcaption>STORY") == 4
    assert response.text.count("Zeitpunkt ändern") == 5
    assert "Dashboard Mannschaft 1" in response.text
    assert "Dashboard Mannschaft 2" in response.text
    assert "Reihenfolge des Karussells" in response.text
    assert "Erstes Bild festlegen" in response.text
    assert 'id="select-all-ai-outputs"' in response.text
    assert 'id="clear-all-ai-outputs"' in response.text
    assert response.text.count('class="ai-output-choice"') == 6
    assert response.text.count('name="media_asset_choices"') == 2
    assert "Gemeinsamen Beitrag löschen" in response.text
    assert "Teilbeitrag einzeln bearbeiten" not in response.text

    member_response = client.get(f"/posts/{member_id}", follow_redirects=False)
    assert member_response.status_code == 303
    assert member_response.headers["location"].startswith(f"/posts/{primary_id}")

    reordered = client.post(
        f"/posts/{primary_id}/carousel/order",
        data={
            "csrf_token": session_csrf(client),
            "first_team_id": team_ids[1],
            "job_version": carousel_version,
        },
        follow_redirects=False,
    )
    assert reordered.status_code == 303
    with factory() as db:
        primary = db.get(Post, primary_id)
        members = matchday_bundle_jobs(db, primary)[1]
        assert [member.team_id for member in members] == [team_ids[1], team_ids[0]]


def test_incomplete_legacy_matchday_post_can_be_opened_and_deleted(browser, tmp_path):
    client, factory = browser
    with factory() as db:
        page = InstagramPage(
            internal_name="broken-bundle-page",
            display_name="Beschädigte Bündelung",
            username="broken_bundle",
            club="Dashboard Testverein",
            active=True,
        )
        db.add(page)
        db.flush()
        team = Team(
            internal_name="broken-bundle-team",
            display_name="Dashboard Mannschaft I",
            short_name="DM I",
            slug="broken-bundle-team",
            club="Dashboard Testverein",
            fussball_url="https://example.invalid/broken-bundle",
            instagram_page_id=page.id,
            media_subdir="broken-bundle-team",
        )
        db.add(team)
        db.flush()
        game = Game(
            team_id=team.id,
            provider="mock",
            external_id="broken-bundle-game",
            home_team=team.display_name,
            away_team="Gegner",
            kickoff=datetime(2026, 8, 16, 15, tzinfo=timezone.utc),
            competition="Kreisliga",
            venue="Sportplatz",
            pitch="Rasenplatz",
            source_url="fixture://broken-bundle-game",
        )
        db.add(game)
        db.flush()
        media_path = tmp_path / "broken-bundle-feed.png"
        Image.new("RGB", (1080, 1350), "blue").save(media_path)
        post = Post(
            game_id=game.id,
            team_id=team.id,
            instagram_page_id=page.id,
            post_type="announcement",
            status=PostStatus.PENDING,
            text="Unvollständiger gemeinsamer Beitrag",
            feed_path=str(media_path),
            critical_warnings=[],
        )
        db.add(post)
        db.flush()
        missing_id = "00000000-0000-0000-0000-000000000099"
        post.design_snapshot = {
            "club_matchday_carousel": {
                "primary_post_id": post.id,
                "member_post_ids": [post.id, missing_id],
                "role": "primary",
            }
        }
        publication = PublicationJob(
            post_id=post.id,
            game_id=game.id,
            team_id=team.id,
            instagram_page_id=page.id,
            kind="feed",
            media_path=str(media_path),
            scheduled_at=game.kickoff - timedelta(days=2),
            idempotency_key=f"{post.id}:feed:v1",
        )
        db.add(publication)
        db.commit()
        post_id = post.id
        version = post.version

    response = client.get(f"/posts/{post_id}")
    assert response.status_code == 200
    assert "Unvollständiger gemeinsamer Spieltagsbeitrag" in response.text
    assert "Gemeinsamen Beitrag löschen" in response.text
    assert "ausdrücklich freigeben" not in response.text

    deleted = client.post(
        f"/posts/{post_id}/delete",
        data={
            "csrf_token": session_csrf(client),
            "version": version,
            "confirmation": "BEITRAG LÖSCHEN",
        },
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    with factory() as db:
        assert db.get(Post, post_id) is None


def test_dashboard_admin_flow(browser):
    client, factory = browser
    token = session_csrf(client)
    with factory() as db:
        page = InstagramPage(
            internal_name="main",
            display_name="Hauptseite",
            username="club",
            club="SV",
            account_id="mock-42",
        )
        db.add(page)
        db.commit()
        version = page.version
    assert (
        client.post(
            f"/instagram/{page.id}/state",
            data={"csrf_token": token, "action": "mock-connect", "version": version},
            follow_redirects=False,
        ).status_code
        == 303
    )
    result = client.post(
        "/teams",
        data={
            "csrf_token": token,
            "internal_name": "erste",
            "display_name": "Erste Mannschaft",
            "short_name": "I",
            "slug": "erste",
            "club": "SV",
            "fussball_url": "https://www.fussball.de/team",
            "instagram_page_id": page.id,
            "media_subdir": "erste",
        },
        follow_redirects=False,
    )
    assert result.status_code == 303
    with factory() as db:
        team = db.query(Team).one()
    result = client.post(
        f"/rules/{team.id}/defaults",
        data={
            "csrf_token": token,
            "announcement_enabled": "true",
            "feed_before_minutes": "1440",
            "announcement_timing_mode": "weekday_fixed",
            "announcement_offset_direction": "before",
            "announcement_offset_minutes": "1440",
            "announcement_monday": "18:00",
            "announcement_tuesday": "18:05",
            "announcement_wednesday": "18:10",
            "announcement_thursday": "18:15",
            "announcement_friday": "18:20",
            "announcement_saturday": "10:00",
            "announcement_sunday": "09:00",
            "announcement_target_friday": "3",
            "announcement_target_sunday": "4",
            "late_approval": "manual",
            "result_wait_minutes": "120",
            "result_timing_mode": "result_detected",
            "result_offset_direction": "after",
            "result_offset_minutes": "120",
            "allow_provisional_games": "true",
            "automatic_sync_enabled": "true",
            "automatic_generation_enabled": "true",
            "generation_lead_days": "4",
            "sync_interval_hours": "24",
            "result_poll_interval_minutes": "15",
            "auto_approve_announcements": "true",
            "auto_approve_announcements_acknowledged": "true",
            "club_matchday_feed_mode": "announcements",
            "club_matchday_primary_team_id": team.id,
            "announcement_feed_output_count": "1",
            "announcement_story_output_count": "2",
            "reminder_feed_output_count": "1",
            "reminder_story_output_count": "1",
            "result_feed_output_count": "1",
            "result_story_output_count": "1",
        },
        follow_redirects=False,
    )
    assert result.status_code == 303
    with factory() as db:
        saved_team = db.get(Team, team.id)
        assert saved_team.rules["allow_provisional_games"] is True
        assert saved_team.rules["generation_lead_days"] == 4
        assert saved_team.rules["sync_interval_hours"] == 24
        assert saved_team.rules["result_poll_interval_minutes"] == 15
        assert saved_team.rules["auto_approve_announcements"] is True
        assert saved_team.rules["club_matchday_feed_mode"] == "announcements"
        assert saved_team.rules["club_matchday_primary_team_id"] == team.id
        assert saved_team.rules["announcement_timing_mode"] == "weekday_fixed"
        assert saved_team.rules["announcement_weekday_times"]["6"] == "09:00"
        assert saved_team.rules["announcement_weekday_targets"]["4"] == "3"
        assert saved_team.rules["announcement_weekday_targets"]["6"] == "4"
    assert "Vorläufige Spielpläne für Ankündigungen verwenden" in client.get(
        f"/rules?team_id={team.id}"
    ).text
    result = client.post(
        f"/rules/{team.id}/stories",
        data={
            "csrf_token": token,
            "name": "24 Stunden",
            "post_type": "announcement",
            "reference": "kickoff",
            "direction": "before",
            "offset_minutes": "1440",
            "fixed_time": "",
            "timing_mode": "weekday_fixed",
            "weekday_monday": "18:00",
            "weekday_tuesday": "18:05",
            "weekday_wednesday": "18:10",
            "weekday_thursday": "18:15",
            "weekday_friday": "18:20",
            "weekday_saturday": "10:00",
            "weekday_sunday": "09:00",
            "target_monday": "0",
            "target_tuesday": "1",
            "target_wednesday": "2",
            "target_thursday": "3",
            "target_friday": "4",
            "target_saturday": "5",
            "target_sunday": "4",
            "media_slot": "2",
            "template": "default-story",
            "sort_order": "1",
        },
        follow_redirects=False,
    )
    assert result.status_code == 303
    with factory() as db:
        story = db.query(StoryRule).one()
        assert story.timing_mode == "weekday_fixed"
        assert story.weekday_times["6"] == "09:00"
        assert story.weekday_targets["6"] == "4"
        assert story.media_slot == 2
    assert "24 Stunden" in client.get(f"/rules?team_id={team.id}").text
    result = client.post(
        "/games/mock",
        data={
            "csrf_token": token,
            "team_id": team.id,
            "opponent": "FC Fixture",
            "side": "home",
            "kickoff": "2026-08-10T18:00",
            "venue": "Testplatz",
        },
        follow_redirects=False,
    )
    assert result.status_code == 303
    with factory() as db:
        game = db.query(__import__("app.models", fromlist=["Game"]).Game).one()
    details = client.post(
        f"/games/{game.id}/details",
        data={
            "csrf_token": token,
            "competition": "Kreisliga A",
            "venue": "Habichtswaldstadion Ehlen",
            "pitch": "Rasenplatz",
        },
        follow_redirects=False,
    )
    assert details.status_code == 303
    with factory() as db:
        updated = db.get(Game, game.id)
        assert updated.competition == "Kreisliga A" and updated.pitch == "Rasenplatz"
    result = client.post(
        f"/games/{game.id}/generate",
        data={"csrf_token": token, "post_type": "announcement"},
        follow_redirects=False,
    )
    assert result.status_code == 303 and result.headers["location"].startswith("/generation-jobs/")
    with factory() as db:
        assert db.query(Post).count() == 0
        assert db.query(GenerationJob).count() == 1
        job_id = claim_next(db)
        assert job_id
        job = process_generation_job(db, job_id, get_settings())
        assert job.status == GenerationJobStatus.SUCCEEDED
    with factory() as db:
        post = db.query(Post).one()
        story_ids = [
            job.id for job in db.query(PublicationJob).filter_by(post_id=post.id, kind="story")
        ]
        post_version = post.version
        feed = db.query(PublicationJob).filter_by(post_id=post.id, kind="feed").one()
        feed.status = __import__("app.models", fromlist=["JobStatus"]).JobStatus.PUBLISHED
        feed.platform_id = "published-feed"
        db.commit()
        published_feed_state = (feed.media_path, feed.version, feed.platform_id)
        story_paths = {
            job.id: job.media_path
            for job in db.query(PublicationJob).filter_by(post_id=post.id, kind="story")
        }
    targeted_rerender = client.post(
        f"/posts/{post.id}/rerender",
        data={"csrf_token": token, "version": post_version, "story_job_ids": story_ids},
        follow_redirects=False,
    )
    assert targeted_rerender.status_code == 303 and targeted_rerender.headers["location"].startswith(
        "/generation-jobs/"
    )
    with factory() as db:
        queued = db.scalar(
            __import__("sqlalchemy", fromlist=["select"])
            .select(GenerationJob)
            .where(GenerationJob.job_type == "RERENDER_POST")
        )
        claimed = claim_next(db)
        assert claimed == queued.id
        completed = process_generation_job(db, claimed, get_settings())
        assert completed.status == GenerationJobStatus.SUCCEEDED
        db.expire_all()
        feed = db.query(PublicationJob).filter_by(post_id=post.id, kind="feed").one()
        assert feed.status == __import__("app.models", fromlist=["JobStatus"]).JobStatus.PUBLISHED
        assert (feed.media_path, feed.version, feed.platform_id) == published_feed_state
        rerendered_stories = db.query(PublicationJob).filter_by(post_id=post.id, kind="story").all()
        assert all(job.media_path != story_paths[job.id] for job in rerendered_stories)
    assert (
        client.post(
            f"/posts/{post.id}/rerender", data={"csrf_token": "wrong", "version": post_version}
        ).status_code
        == 403
    )
    with factory() as db:
        assert db.query(AuditLog).filter_by(action="generation.succeeded").count() >= 1
    records = FussballDeProvider().parse(
        open("tests/fixtures/fussball_sv_ehlen_2627.html", encoding="utf-8").read()
    )
    with factory() as db:
        snapshot = ProviderSnapshot(
            team_id=team.id,
            source_url=team.fussball_url,
            status_code=200,
            checksum="b" * 64,
            relative_path="dashboard/test.html",
            parser_result={
                "team_name": team.display_name,
                "games": [serialize(x) for x in records],
            },
        )
        db.add(snapshot)
        db.commit()
        snapshot_id = snapshot.id
    overview = client.get("/diagnostics")
    assert (
        overview.status_code == 200
        and "SV Ehlen" in overview.text
        and "provisional" in overview.text
    )
    preview = client.get(f"/diagnostics/{snapshot_id}/import")
    assert preview.status_code == 200 and "0318JUMQIS" in preview.text
    result = client.post(
        f"/diagnostics/{snapshot_id}/import",
        data={"csrf_token": token, "confirmation": "SPIELE ÜBERNEHMEN"},
        follow_redirects=False,
    )
    assert result.status_code == 303
    result = client.post(
        f"/diagnostics/{snapshot_id}/import",
        data={"csrf_token": token, "confirmation": "SPIELE ÜBERNEHMEN"},
        follow_redirects=False,
    )
    assert result.status_code == 303
    with factory() as db:
        assert db.query(Game).filter_by(provider="fussball.de").count() == 3
    result = client.post(
        "/users",
        data={
            "csrf_token": token,
            "email": "editor@test.invalid",
            "password": "Another-Secure-Test",
            "role": "editor",
        },
        follow_redirects=False,
    )
    assert result.status_code == 303
    assert "editor@test.invalid" in client.get("/users").text


def test_csrf_and_non_admin_are_rejected(browser):
    client, factory = browser
    assert (
        client.post(
            "/instagram",
            data={
                "csrf_token": "wrong",
                "internal_name": "x",
                "display_name": "x",
                "username": "x",
                "club": "x",
                "account_id": "",
            },
        ).status_code
        == 403
    )
    with factory() as db:
        editor = User(
            email="limited@test.invalid",
            password_hash=hash_password("Very-Secure-Test-Password"),
            role=Role.EDITOR,
            all_teams=False,
        )
        page = InstagramPage(
            internal_name="restricted-logo-page",
            display_name="Restricted",
            username="restricted",
            club="Restricted",
            active=True,
        )
        db.add_all([editor, page])
        db.flush()
        team = Team(
            internal_name="restricted-logo-team",
            display_name="Restricted",
            short_name="R",
            slug="restricted-logo-team",
            club="Restricted",
            fussball_url="https://www.fussball.de/restricted",
            instagram_page_id=page.id,
            media_subdir="restricted",
        )
        db.add(team)
        db.flush()
        game = Game(
            team_id=team.id,
            external_id="restricted-logo-game",
            home_team="Restricted",
            away_team="FC Fremd",
            kickoff=datetime.now(timezone.utc) + timedelta(days=2),
            source_url="fixture://restricted",
        )
        db.add(game)
        db.commit()
        team_id, game_id = team.id, game.id
    client.post("/logout")
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)', page.text).group(1)
    client.post(
        "/login",
        data={
            "email": "limited@test.invalid",
            "password": "Very-Secure-Test-Password",
            "csrf_token": token,
        },
    )
    assert client.get("/users").status_code == 403
    assert client.get(f"/games/{game_id}/opponent-logo").status_code == 403
    token = session_csrf(client)
    assert (
        client.post(
            f"/games/{game_id}/opponent-logo",
            data={"csrf_token": token, "action": "remove"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/teams/{team_id}/logo",
            data={"csrf_token": token},
            files={"file": ("logo.png", logo_png(), "image/png")},
        ).status_code
        == 403
    )


def test_story_rule_can_be_deleted_and_restored_without_removing_history(browser):
    client, factory = browser
    with factory() as db:
        page = InstagramPage(
            internal_name="story-rule-page",
            display_name="Story Rule Page",
            username="storyrules",
            club="SV Test",
            active=True,
        )
        db.add(page)
        db.flush()
        team = Team(
            internal_name="story-rule-team",
            display_name="SV Test I",
            short_name="SVT",
            slug="story-rule-team",
            club="SV Test",
            fussball_url="https://www.fussball.de/story-rule-team",
            instagram_page_id=page.id,
            media_subdir="story-rule-team",
        )
        db.add(team)
        db.flush()
        item = StoryRule(
            team_id=team.id,
            name="24 Stunden vorher",
            post_type="announcement",
            reference="kickoff",
            direction="before",
            offset_minutes=1440,
            template="default-story",
            prompt_template="default-image-story",
            sort_order=1,
        )
        db.add(item)
        db.commit()
        team_id, story_rule_id = team.id, item.id

    rejected = client.post(
        f"/rules/{team_id}/stories/{story_rule_id}/delete",
        data={"csrf_token": "ungueltig"},
        follow_redirects=False,
    )
    assert rejected.status_code == 403

    token = session_csrf(client)
    response = client.post(
        f"/rules/{team_id}/stories/{story_rule_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert f"team_id={team_id}&notice=" in response.headers["location"]
    assert "24 Stunden vorher" not in client.get(f"/rules?team_id={team_id}").text
    with factory() as db:
        item = db.get(StoryRule, story_rule_id)
        assert item is not None
        assert item.active is False
        deleted = db.query(AuditLog).filter_by(action="story_rule.deleted").one()
        assert deleted.entity_id == story_rule_id
        assert deleted.details["deletion_mode"] == "deactivated"

    response = client.post(
        f"/rules/{team_id}/stories",
        data={
            "csrf_token": token,
            "name": "24 Stunden vorher",
            "post_type": "announcement",
            "reference": "kickoff",
            "direction": "before",
            "offset_minutes": "720",
            "template": "default-story",
            "prompt_template": "default-image-story",
            "sort_order": "2",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with factory() as db:
        items = db.query(StoryRule).filter_by(team_id=team_id).all()
        assert len(items) == 1
        assert items[0].id == story_rule_id
        assert items[0].active is True
        assert items[0].offset_minutes == 720
        assert db.query(AuditLog).filter_by(action="story_rule.restored").count() == 1


def test_admin_assigns_editorial_roles_and_last_admin_is_protected(browser):
    client, factory = browser
    token = session_csrf(client)
    response = client.post(
        "/users",
        data={
            "csrf_token": token,
            "email": "author@test.invalid",
            "password": "Another-Secure-Test",
            "role": "editor",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/users")
    assert all(
        label in page.text
        for label in ("Vereinsadministrator", "Redakteur", "Autor", "Nur Lesen")
    )
    with factory() as db:
        author = db.query(User).filter_by(email="author@test.invalid").one()
        administrator = db.query(User).filter_by(email="admin@test.invalid").one()
        author_id, administrator_id = author.id, administrator.id
    response = client.post(
        f"/users/{author_id}/role",
        data={"csrf_token": token, "role": "approver"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with factory() as db:
        assert db.get(User, author_id).role == Role.APPROVER
        audit_item = db.query(AuditLog).filter_by(action="user.role_changed").one()
        assert audit_item.details == {"old_role": "editor", "new_role": "approver"}
    response = client.post(
        f"/users/{administrator_id}/role",
        data={"csrf_token": token, "role": "viewer"},
        follow_redirects=False,
    )
    assert response.status_code == 409


def test_club_admin_cannot_access_protected_prompt_dashboard(browser):
    client, _factory = browser
    token = session_csrf(client)
    assert client.get("/prompts").status_code == 403
    response = client.post(
        "/prompts/preview",
        data={
            "csrf_token": token,
            "prompt_kind": "image",
            "post_type": "announcement",
            "media_kind": "feed",
            "style_direction": "dramatisch",
            "prompt_body": "Geheimer Plattformprompt: {{ home_team }}",
        },
    )
    assert response.status_code == 403


def test_mock_game_uses_opponent_and_side_and_can_be_deleted(browser):
    client, factory = browser
    token = session_csrf(client)
    with factory() as db:
        page = InstagramPage(
            internal_name="mock-page",
            display_name="Mock-Seite",
            username="mockseite",
            club="SV Ehlen",
            active=True,
        )
        db.add(page)
        db.flush()
        team = Team(
            internal_name="erste-mannschaft",
            display_name="SV Ehlen I",
            short_name="I",
            slug="mock-team-delete",
            club="SV Ehlen",
            fussball_url="https://www.fussball.de/mock-team",
            instagram_page_id=page.id,
            media_subdir="erste_mannschaft/spieler",
        )
        db.add(team)
        db.commit()
        team_id = team.id

    page = client.get("/games")
    assert 'name="opponent"' in page.text
    assert 'name="side"' in page.text
    assert 'name="home_team"' not in page.text

    result = client.post(
        "/games/mock",
        data={
            "csrf_token": token,
            "team_id": team_id,
            "opponent": "Testverein Kassel",
            "side": "home",
            "kickoff": "2026-08-08T13:00",
            "competition": "Freundschaftsspiel",
            "venue": "Habichtswaldstadion Ehlen",
            "pitch": "Rasenplatz",
        },
        follow_redirects=False,
    )
    assert result.status_code == 303
    with factory() as db:
        game = db.query(Game).one()
        assert (game.home_team, game.away_team) == (
            "SV Ehlen I",
            "Testverein Kassel",
        )
        failed = GenerationJob(
            job_type=GenerationJobType.CREATE_POST,
            game_id=game.id,
            team_id=team_id,
            post_type="announcement",
            requested_by=db.query(User).one().id,
            status=GenerationJobStatus.FAILED,
            phase="generating_text",
            idempotency_key=f"failed:{game.id}",
            active_key=None,
        )
        asset = MediaAsset(
            team_id=team_id,
            relative_path="uploaded/mock-player.png",
            filename="mock-player.png",
            mime_type="image/png",
            size=123,
            checksum="mock-player-checksum",
            mtime=datetime.now(timezone.utc),
            reserved_game_id=game.id,
            uses=1,
        )
        db.add_all([failed, asset])
        db.commit()
        game_id = game.id
        asset_id = asset.id

    result = client.post(
        f"/games/{game_id}/delete-mock",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert result.status_code == 303
    with factory() as db:
        assert db.get(Game, game_id) is None
        assert db.query(GenerationJob).count() == 0
        asset = db.get(MediaAsset, asset_id)
        assert asset.reserved_game_id is None and asset.uses == 0
        deleted = db.query(AuditLog).filter_by(action="game.mock_deleted").one()
        assert deleted.entity_id == game_id

    result = client.post(
        "/games/mock",
        data={
            "csrf_token": token,
            "team_id": team_id,
            "opponent": "FC Auswärts",
            "side": "away",
            "kickoff": "2026-08-15T15:00",
        },
        follow_redirects=False,
    )
    assert result.status_code == 303
    with factory() as db:
        away_game = db.query(Game).one()
        assert (away_game.home_team, away_game.away_team) == (
            "FC Auswärts",
            "SV Ehlen I",
        )


def test_result_can_be_entered_confirmed_and_corrected_from_games_dashboard(browser):
    client, factory = browser
    with factory() as db:
        page = InstagramPage(
            internal_name="result-entry-page",
            display_name="Ergebnis-Seite",
            username="result_entry",
            club="Dashboard Testverein",
            active=True,
        )
        db.add(page)
        db.flush()
        team = Team(
            internal_name="result-entry-team",
            display_name="Dashboard Testelf",
            short_name="DTE",
            slug="result-entry-team",
            club="Dashboard Testverein",
            fussball_url="https://example.invalid/result-entry-team",
            instagram_page_id=page.id,
            media_subdir="result-entry/players",
            rules={"result_enabled": True},
        )
        db.add(team)
        db.flush()
        game = Game(
            team_id=team.id,
            provider="mock",
            external_id="result-entry-game",
            home_team="Dashboard Testelf",
            away_team="FC Ergebnis",
            kickoff=datetime.now(timezone.utc) - timedelta(hours=3),
            competition="Testliga",
            venue="Testplatz",
            pitch="Rasenplatz",
            source_url="fixture://result-entry-game",
        )
        db.add(game)
        db.commit()
        game_id = game.id
        game_version = game.version
        team_id = team.id
        page_id = page.id

    overview = client.get("/games")
    assert overview.status_code == 200
    assert "Ergebnis eintragen und bestätigen" in overview.text
    result_button = re.search(
        r'<button name="post_type" value="result"([^>]*)>Ergebnis</button>',
        overview.text,
    )
    assert result_button and "disabled" in result_button.group(1)

    token = session_csrf(client)
    missing_result = client.post(
        f"/games/{game_id}/generate",
        data={"csrf_token": token, "post_type": "result"},
        follow_redirects=False,
    )
    assert missing_result.status_code == 303
    assert missing_result.headers["location"].startswith("/games?notice=")
    assert (
        client.post(
            f"/games/{game_id}/result",
            data={
                "csrf_token": "wrong",
                "version": str(game_version),
                "home_score": "2",
                "away_score": "1",
                "confirmation": "true",
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/games/{game_id}/result",
            data={
                "csrf_token": token,
                "version": str(game_version),
                "home_score": "2",
                "away_score": "1",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/games/{game_id}/result",
            data={
                "csrf_token": token,
                "version": str(game_version),
                "home_score": "100",
                "away_score": "1",
                "confirmation": "true",
            },
        ).status_code
        == 422
    )

    confirmed = client.post(
        f"/games/{game_id}/result",
        data={
            "csrf_token": token,
            "version": str(game_version),
            "home_score": "2",
            "away_score": "1",
            "confirmation": "true",
        },
        follow_redirects=False,
    )
    assert confirmed.status_code == 303
    with factory() as db:
        game = db.get(Game, game_id)
        assert (game.home_score, game.away_score) == (2, 1)
        assert game.result_confirmed is True
        assert game.status == "finished"
        assert game.overrides["result_confirmation_source"] == "dashboard_manual"
        assert game.overrides["provider_score_candidate"] == "2:1"
        corrected_version = game.version
        assert (
            db.query(AuditLog).filter_by(action="game.result_confirmed_manually").count()
            == 1
        )

        user = db.query(User).filter_by(email="admin@test.invalid").one()
        post = Post(
            game_id=game.id,
            team_id=team_id,
            instagram_page_id=page_id,
            post_type="result",
            status=PostStatus.APPROVED,
            text="Altes Ergebnis 2:1",
            approved_version=1,
            approved_by=user.id,
            approved_at=datetime.now(timezone.utc),
        )
        db.add(post)
        db.flush()
        scheduled = PublicationJob(
            post_id=post.id,
            game_id=game.id,
            team_id=team_id,
            instagram_page_id=page_id,
            kind="feed",
            media_path="/tmp/result-feed.png",
            scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
            status=JobStatus.SCHEDULED,
            approval_status="approved",
            approved_post_version=post.version,
            idempotency_key="result-entry-scheduled",
        )
        published = PublicationJob(
            post_id=post.id,
            game_id=game.id,
            team_id=team_id,
            instagram_page_id=page_id,
            kind="story",
            media_path="/tmp/result-story.png",
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            status=JobStatus.PUBLISHED,
            approval_status="approved",
            approved_post_version=post.version,
            idempotency_key="result-entry-published",
            platform_id="published-result-story",
        )
        db.add_all([scheduled, published])
        db.commit()
        post_id = post.id
        scheduled_id = scheduled.id
        published_id = published.id

    stale = client.post(
        f"/games/{game_id}/result",
        data={
            "csrf_token": token,
            "version": str(game_version),
            "home_score": "3",
            "away_score": "1",
            "confirmation": "true",
        },
    )
    assert stale.status_code == 409

    corrected = client.post(
        f"/games/{game_id}/result",
        data={
            "csrf_token": token,
            "version": str(corrected_version),
            "home_score": "3",
            "away_score": "1",
            "confirmation": "true",
        },
        follow_redirects=False,
    )
    assert corrected.status_code == 303
    with factory() as db:
        post = db.get(Post, post_id)
        scheduled = db.get(PublicationJob, scheduled_id)
        published = db.get(PublicationJob, published_id)
        assert post.status == PostStatus.REAPPROVAL
        assert post.approved_version is None
        assert post.approved_by is None
        assert post.approved_at is None
        assert scheduled.status == JobStatus.UNAPPROVED
        assert scheduled.approval_status == "reapproval_required"
        assert published.status == JobStatus.PUBLISHED
        assert published.platform_id == "published-result-story"

    confirmed_overview = client.get("/games")
    assert "Ergebnis bestätigt: 3:1" in confirmed_overview.text
    result_button = re.search(
        r'<button name="post_type" value="result"([^>]*)>Ergebnis</button>',
        confirmed_overview.text,
    )
    assert result_button and "disabled" not in result_button.group(1)


def test_real_provider_game_is_suppressed_and_can_be_restored(browser):
    client, factory = browser
    token = session_csrf(client)
    with factory() as db:
        page = InstagramPage(
            internal_name="real-page",
            display_name="Real-Seite",
            username="realseite",
            club="SV Ehlen",
            active=True,
        )
        db.add(page)
        db.flush()
        team = Team(
            internal_name="real-team",
            display_name="SV Ehlen I",
            short_name="I",
            slug="real-team-delete-protection",
            club="SV Ehlen",
            fussball_url="https://www.fussball.de/real-team",
            instagram_page_id=page.id,
            media_subdir="erste_mannschaft/spieler",
        )
        db.add(team)
        db.flush()
        game = Game(
            team_id=team.id,
            provider="fussball.de",
            external_id="real-game-delete-protection",
            home_team="SV Ehlen",
            away_team="FC Real",
            kickoff=datetime.now(timezone.utc) + timedelta(days=2),
            source_url="https://www.fussball.de/spiel/real",
        )
        db.add(game)
        db.commit()
        game_id = game.id

    result = client.post(
        f"/games/{game_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert result.status_code == 303
    with factory() as db:
        game = db.get(Game, game_id)
        assert game is not None
        assert game.overrides["import_suppressed"] is True
        assert game.overrides["automation_blocked"] is True
        assert db.query(AuditLog).filter_by(action="game.provider_suppressed").count() == 1
    overview = client.get("/games")
    assert "Gelöschte Spiele" in overview.text
    result = client.post(
        f"/games/{game_id}/restore",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert result.status_code == 303
    with factory() as db:
        game = db.get(Game, game_id)
        assert not game.overrides.get("import_suppressed")
        assert db.query(AuditLog).filter_by(action="game.provider_restored").count() == 1


def test_mock_game_with_existing_post_is_safely_hidden_instead_of_destroyed(browser):
    client, factory = browser
    token = session_csrf(client)
    with factory() as db:
        page = InstagramPage(
            internal_name="mock-preserved-page",
            display_name="Mock erhalten",
            username="mockpreserved",
            club="SV Ehlen",
            active=True,
        )
        db.add(page); db.flush()
        team = Team(
            internal_name="mock-preserved-team",
            display_name="SV Ehlen I",
            short_name="I",
            slug="mock-preserved-team",
            club="SV Ehlen",
            fussball_url="https://www.fussball.de/mock-preserved",
            instagram_page_id=page.id,
            media_subdir="erste_mannschaft/spieler",
        )
        db.add(team); db.flush()
        game = Game(
            team_id=team.id,
            provider="mock",
            external_id="mock-preserved-game",
            home_team="SV Ehlen I",
            away_team="FC Archiv",
            kickoff=datetime.now(timezone.utc) + timedelta(days=2),
            source_url="fixture://dashboard",
        )
        db.add(game); db.flush()
        post = Post(
            game_id=game.id,
            team_id=team.id,
            instagram_page_id=page.id,
            post_type="announcement",
        )
        db.add(post); db.commit()
        game_id, post_id = game.id, post.id

    result = client.post(
        f"/games/{game_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert result.status_code == 303
    with factory() as db:
        game = db.get(Game, game_id)
        post = db.get(Post, post_id)
        assert game.overrides["dashboard_deleted"] is True
        assert post is not None and post.publishing_enabled is False
        assert db.query(AuditLog).filter_by(action="game.mock_suppressed").count() == 1


def test_publication_rule_slots_are_csrf_protected_validated_and_audited(browser):
    client, factory = browser
    with factory() as db:
        page = InstagramPage(
            internal_name="rules-page",
            display_name="Regelseite",
            username="rules_page",
            club="Dashboard Testverein",
            active=True,
        )
        db.add(page)
        db.flush()
        team = Team(
            internal_name="rules-team",
            display_name="Regelmannschaft",
            short_name="RM",
            slug="rules-team",
            club="Dashboard Testverein",
            fussball_url="https://example.invalid/rules-team",
            instagram_page_id=page.id,
            media_subdir="rules-team/players",
            rules={
                "announcement_feed_generation_count": 2,
                "announcement_story_generation_count": 3,
            },
        )
        db.add(team)
        db.commit()
        team_id = team.id
        page_id = page.id
        team_version = team.version

    payload = {
        "csrf_token": "ungueltig",
        "expected_team_version": str(team_version),
        "label": "Freitagabend",
        "post_type": "announcement",
        "media_kind": "feed",
        "variant_number": "1",
        "timing_model": "weekday_fixed",
        "match_weekday": "6",
        "target_weekday": "4",
        "local_time": "18:00",
        "reference": "kickoff",
        "direction": "before",
        "offset_minutes": "0",
        "instagram_page_id": page_id,
        "sort_order": "10",
    }
    denied = client.post(
        f"/rules/{team_id}/publication-slots", data=payload, follow_redirects=False
    )
    assert denied.status_code == 403

    payload["csrf_token"] = session_csrf(client)
    created = client.post(
        f"/rules/{team_id}/publication-slots", data=payload, follow_redirects=False
    )
    assert created.status_code == 303
    with factory() as db:
        saved_team = db.get(Team, team_id)
        saved_slot = db.scalar(
            select(PublicationRuleSlot).where(
                PublicationRuleSlot.club_id == saved_team.club_id,
                PublicationRuleSlot.media_kind == "feed",
            )
        )
        assert saved_slot is not None
        assert saved_slot.match_weekday == 6
        assert saved_slot.target_weekday == 4
        assert saved_slot.local_time == "18:00"
        assert saved_slot.variant_number == 1
        assert db.scalar(
            select(AuditLog.id).where(AuditLog.action == "publication_rule_slot.created")
        )
        second_version = saved_team.version

    duplicate = {
        **payload,
        "csrf_token": session_csrf(client),
        "expected_team_version": str(second_version),
        "label": "Zweite Veröffentlichung derselben Datei",
        "timing_model": "relative",
        "reference": "kickoff",
        "offset_minutes": "30",
        "local_time": "",
        "target_weekday": "",
    }
    rejected = client.post(
        f"/rules/{team_id}/publication-slots", data=duplicate, follow_redirects=False
    )
    assert rejected.status_code == 422
    assert "Wiederverwendung" in rejected.text

    overview = client.get(f"/rules?team_id={team_id}")
    assert overview.status_code == 200
    assert 'id="publication-rules"' in overview.text
    assert "Freitagabend" in overview.text

    with factory() as db:
        current_version = db.get(Team, team_id).version
    copied = client.post(
        f"/rules/{team_id}/publication-weekdays/copy",
        data={
            "csrf_token": session_csrf(client),
            "expected_team_version": str(current_version),
            "source_weekday": "6",
            "target_weekday": "5",
        },
        follow_redirects=False,
    )
    assert copied.status_code == 303
    with factory() as db:
        copied_slot = db.scalar(
            select(PublicationRuleSlot).where(
                PublicationRuleSlot.club_id == db.get(Team, team_id).club_id,
                PublicationRuleSlot.match_weekday == 5,
            )
        )
        assert copied_slot is not None
        assert copied_slot.target_weekday == 3
        assert db.scalar(
            select(AuditLog.id).where(
                AuditLog.action == "publication_weekday_rules.copied"
            )
        )


def test_automatic_posts_page_hides_platform_prompts_and_explains_safe_flow(browser):
    client, factory = browser
    team_id = create_automation_team(
        factory,
        suffix="page",
        rules={
            "text_prompt": "SECRET-PLATFORM-PROMPT-MUST-NOT-LEAK",
            "style_direction": "legacy value remains stored",
            "result_poll_interval_minutes": 15,
        },
    )

    response = client.get(f"/rules?team_id={team_id}")

    assert response.status_code == 200
    assert "Automatische Beiträge" in response.text
    assert "Empfohlene Grundeinstellung" in response.text
    assert "Zeitplanung testen" in response.text
    assert "Vereinsbranding" in response.text
    assert "SECRET-PLATFORM-PROMPT-MUST-NOT-LEAK" not in response.text
    assert "Vereinsstil" not in response.text


def test_recommended_preset_requires_csrf_and_replace_confirmation(browser):
    client, factory = browser
    team_id = create_automation_team(
        factory,
        suffix="preset",
        rules={
            "announcement_feed_generation_count": 7,
            "text_prompt": "protected-platform-prompt",
            "publication_rule_slots": [],
        },
    )
    with factory() as db:
        version = db.get(Team, team_id).version

    missing_csrf = client.post(
        f"/rules/{team_id}/recommended-preset",
        data={"expected_team_version": version, "mode": "append_missing"},
    )
    assert missing_csrf.status_code == 422

    replace_without_confirmation = client.post(
        f"/rules/{team_id}/recommended-preset",
        data={
            "csrf_token": session_csrf(client),
            "expected_team_version": version,
            "mode": "replace",
        },
    )
    assert replace_without_confirmation.status_code == 422

    applied = client.post(
        f"/rules/{team_id}/recommended-preset",
        data={
            "csrf_token": session_csrf(client),
            "expected_team_version": version,
            "mode": "append_missing",
        },
        follow_redirects=False,
    )
    assert applied.status_code == 303
    with factory() as db:
        team = db.get(Team, team_id)
        assert team.rules["announcement_feed_generation_count"] == 7
        assert team.rules["text_prompt"] == "protected-platform-prompt"
        assert len(team.rules["publication_rule_slots"]) == 8
        assert db.scalar(
            select(AuditLog.id).where(
                AuditLog.action == "automation_preset.applied",
                AuditLog.entity_id == team_id,
            )
        )


def test_schedule_preview_is_read_only_and_uses_configured_rules(browser):
    client, factory = browser
    team_id = create_automation_team(factory, suffix="preview")
    with factory() as db:
        version = db.get(Team, team_id).version
    applied = client.post(
        f"/rules/{team_id}/recommended-preset",
        data={
            "csrf_token": session_csrf(client),
            "expected_team_version": version,
            "mode": "append_missing",
        },
        follow_redirects=False,
    )
    assert applied.status_code == 303

    with factory() as db:
        before = {
            "team_version": db.get(Team, team_id).version,
            "posts": len(list(db.scalars(select(Post)))),
            "generation_jobs": len(list(db.scalars(select(GenerationJob)))),
            "publication_jobs": len(list(db.scalars(select(PublicationJob)))),
        }
    response = client.post(
        f"/rules/{team_id}/schedule-preview",
        data={
            "csrf_token": session_csrf(client),
            "kickoff_local": "2026-08-09T15:00",
            "result_local": "2026-08-09T17:07",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["weekday"] == "Sonntag"
    assert any(event["when"] == "Freitag, 07.08.2026 · 18:00 Uhr" for event in payload["events"])
    with factory() as db:
        after = {
            "team_version": db.get(Team, team_id).version,
            "posts": len(list(db.scalars(select(Post)))),
            "generation_jobs": len(list(db.scalars(select(GenerationJob)))),
            "publication_jobs": len(list(db.scalars(select(PublicationJob)))),
        }
    assert after == before


def test_rules_reject_unsafe_result_poll_and_unconfirmed_automatic_approval(browser):
    client, factory = browser
    legacy_times = {"6": "18:00"}
    legacy_targets = {str(day): str(day) for day in range(7)}
    team_id = create_automation_team(
        factory,
        suffix="validation",
        rules={
            "announcement_weekday_times": legacy_times,
            "announcement_weekday_targets": legacy_targets,
            "result_poll_interval_minutes": 15,
        },
    )
    with factory() as db:
        version = db.get(Team, team_id).version

    unsafe_poll = client.post(
        f"/rules/{team_id}/defaults",
        data={
            "csrf_token": session_csrf(client),
            "expected_team_version": version,
            "preserve_legacy_weekday_settings": "true",
            "late_approval": "manual",
            "result_wait_minutes": 0,
            "result_poll_interval_minutes": 9,
        },
    )
    assert unsafe_poll.status_code == 422
    assert "frühestens alle 10 Minuten" in unsafe_poll.text

    automatic_without_ack = client.post(
        f"/rules/{team_id}/defaults",
        data={
            "csrf_token": session_csrf(client),
            "expected_team_version": version,
            "preserve_legacy_weekday_settings": "true",
            "late_approval": "manual",
            "result_wait_minutes": 0,
            "result_poll_interval_minutes": 10,
            "auto_approve_announcements": "true",
        },
    )
    assert automatic_without_ack.status_code == 422
    assert "ausdrücklich bestätigt" in automatic_without_ack.text

    safe_save = client.post(
        f"/rules/{team_id}/defaults",
        data={
            "csrf_token": session_csrf(client),
            "expected_team_version": version,
            "preserve_legacy_weekday_settings": "true",
            "late_approval": "manual",
            "result_wait_minutes": 0,
            "result_poll_interval_minutes": 10,
        },
        follow_redirects=False,
    )
    assert safe_save.status_code == 303
    with factory() as db:
        rules = db.get(Team, team_id).rules
        assert rules["result_poll_interval_minutes"] == 10
        assert rules["announcement_weekday_times"] == legacy_times
        assert rules["announcement_weekday_targets"] == legacy_targets


def test_editor_can_view_but_cannot_change_automatic_post_rules(browser):
    client, factory = browser
    team_id = create_automation_team(factory, suffix="editor")
    with factory() as db:
        club_id = db.get(Team, team_id).club_id
        editor = User(
            email="automation-editor@test.invalid",
            password_hash=hash_password("Editor-Secure-Password"),
            role=Role.EDITOR,
            all_teams=True,
            club_id=club_id,
            active=True,
        )
        db.add(editor)
        db.commit()
        version = db.get(Team, team_id).version

    client.post("/logout")
    login_page = client.get("/login")
    login_csrf = re.search(
        r'name="csrf_token" value="([^"]+)', login_page.text
    ).group(1)
    logged_in = client.post(
        "/login",
        data={
            "email": "automation-editor@test.invalid",
            "password": "Editor-Secure-Password",
            "csrf_token": login_csrf,
        },
        follow_redirects=False,
    )
    assert logged_in.status_code == 303

    page = client.get(f"/rules?team_id={team_id}")
    assert page.status_code == 200
    assert "Automatische Beiträge" in page.text
    assert "Grundeinstellung übernehmen" not in page.text
    forbidden = client.post(
        f"/rules/{team_id}/recommended-preset",
        data={
            "csrf_token": session_csrf(client),
            "expected_team_version": version,
            "mode": "append_missing",
        },
    )
    assert forbidden.status_code == 403


def test_live_center_accepts_tenant_scoped_manual_event_with_csrf(browser):
    client, factory = browser
    with factory() as db:
        page = InstagramPage(
            internal_name="live-center-page",
            display_name="Live-Center-Seite",
            username="live_center_page",
            club="Dashboard Testverein",
            active=True,
        )
        db.add(page)
        db.flush()
        team = Team(
            internal_name="live-center-team",
            display_name="Live-Center-Mannschaft",
            short_name="LC",
            slug="live-center-team",
            club="Dashboard Testverein",
            fussball_url="https://example.invalid/live-center-team",
            instagram_page_id=page.id,
            media_subdir="live-center/players",
            timezone="Europe/Berlin",
        )
        db.add(team)
        db.flush()
        game = Game(
            team_id=team.id,
            provider="mock",
            external_id="live-center-game",
            home_team=team.display_name,
            away_team="Testgegner",
            kickoff=datetime.now(timezone.utc),
            competition="Testliga",
            venue="Testplatz",
            status="scheduled",
            source_url="fixture://live-center-game",
            checked_at=datetime.now(timezone.utc),
        )
        db.add(game)
        db.commit()
        game_id = game.id

    page = client.get("/live")
    assert page.status_code == 200
    assert "Live Center" in page.text
    assert "Live-Center-Mannschaft" in page.text

    rejected = client.post(
        f"/live/games/{game_id}/events",
        data={"csrf_token": "ungueltig", "event_type": "goal", "minute": 12},
    )
    assert rejected.status_code == 403

    created = client.post(
        f"/live/games/{game_id}/events",
        data={
            "csrf_token": session_csrf(client),
            "event_type": "goal",
            "minute": 12,
            "player_name": "Testspieler",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    with factory() as db:
        event = db.scalar(select(MatchEvent).where(MatchEvent.game_id == game_id))
        state = db.scalar(select(LiveGameState).where(LiveGameState.game_id == game_id))
        assert event is not None
        assert event.status == "confirmed"
        assert state is not None
        assert (state.home_score, state.away_score) == (1, 0)
