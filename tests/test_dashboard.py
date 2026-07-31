import re
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.auth.service import hash_password
from app.config import get_settings
from app.db import Base, get_db
from app.games.live_test import serialize
from app.games.provider import FussballDeProvider
from app.jobs.generation import claim_next, process_generation_job
from app.main import app
from app.models import (
    AuditLog,
    Game,
    GenerationJob,
    GenerationJobStatus,
    GenerationJobType,
    InstagramConnection,
    InstagramPage,
    LogoAsset,
    MediaAsset,
    Post,
    PromptTemplate,
    ProviderSnapshot,
    PublicationJob,
    Role,
    Team,
    User,
)


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
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        db.add(
            User(
                email="admin@test.invalid",
                password_hash=hash_password("Very-Secure-Test-Password"),
                role=Role.ADMIN,
                all_teams=True,
            )
        )
        db.commit()

    def override():
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


def test_instagram_meta_test_dashboard_is_explicit_and_blocks_mock_connect(
    browser, monkeypatch
):
    client, factory = browser
    import app.admin_routes as admin_routes

    monkeypatch.setattr(admin_routes.settings, "environment", "meta-test")
    monkeypatch.setattr(admin_routes.settings, "publisher_mode", "instagram")
    monkeypatch.setitem(
        admin_routes.templates.env.globals, "environment", "meta-test"
    )
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
    response = client.get("/instagram")
    assert response.status_code == 200
    assert "META-TEST – ECHTE INSTAGRAM-VERÖFFENTLICHUNGEN MÖGLICH" in response.text
    assert "Meta-Testassistent öffnen" in response.text
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
    assert "/static/style.css?v=20260731-player-uploads" in teams_page
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
    with factory() as db:
        assert db.get(Game, second_id).opponent_logo_id is None
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


def test_dashboard_admin_flow(browser):
    client, factory = browser
    token = session_csrf(client)
    result = client.post(
        "/instagram",
        data={
            "csrf_token": token,
            "internal_name": "main",
            "display_name": "Hauptseite",
            "username": "club",
            "club": "SV",
            "account_id": "mock-42",
        },
        follow_redirects=False,
    )
    assert result.status_code == 303
    with factory() as db:
        page = db.query(InstagramPage).one()
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
            "late_approval": "manual",
            "result_wait_minutes": "120",
            "allow_provisional_games": "true",
        },
        follow_redirects=False,
    )
    assert result.status_code == 303
    with factory() as db:
        saved_team = db.get(Team, team.id)
        assert saved_team.rules["allow_provisional_games"] is True
    assert "Vorläufige FUSSBALL.DE-Spielpläne" in client.get(
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
            "template": "default-story",
            "sort_order": "1",
        },
        follow_redirects=False,
    )
    assert result.status_code == 303
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
    conflict = client.post(
        f"/posts/{post.id}/rerender",
        data={"csrf_token": token, "version": post_version, "story_job_ids": story_ids},
        follow_redirects=False,
    )
    assert conflict.status_code == 303 and conflict.headers["location"].startswith(
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
        failed = process_generation_job(db, claimed, get_settings())
        assert failed.status == GenerationJobStatus.FAILED
        assert "Feed wurde bereits" in failed.error_message
    with factory() as db:
        feed = db.query(PublicationJob).filter_by(post_id=post.id, kind="feed").one()
        feed.status = __import__("app.models", fromlist=["JobStatus"]).JobStatus.UNAPPROVED
        feed.platform_id = None
        db.commit()
    assert (
        client.post(
            f"/posts/{post.id}/rerender", data={"csrf_token": "wrong", "version": post_version}
        ).status_code
        == 403
    )
    with factory() as db:
        assert db.query(AuditLog).filter_by(action="generation.failed").count() == 1
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


def test_prompt_dashboard_previews_without_api_and_versions_templates(browser):
    client, factory = browser
    token = session_csrf(client)
    page = client.get("/prompts")
    assert page.status_code == 200 and "KI-Promptvorlagen" in page.text
    body = "Dynamische Grafik: {{ home_team }} gegen {{ away_team }} in {{ venue_display }}"
    preview = client.post(
        "/prompts/preview",
        data={
            "csrf_token": token,
            "prompt_kind": "image",
            "post_type": "announcement",
            "media_kind": "feed",
            "style_direction": "dramatisch",
            "prompt_body": body,
        },
    )
    assert preview.status_code == 200
    assert "SV Ehlen gegen SG Beispiel" in preview.text
    for _version in (1, 2):
        response = client.post(
            "/prompts",
            data={
                "csrf_token": token,
                "name": "sve-feed",
                "prompt_kind": "image",
                "post_type": "announcement",
                "media_kind": "feed",
                "prompt_body": body,
                "style_direction": "dramatisch",
                "model": "gpt-image-2",
                "quality": "medium",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
    with factory() as db:
        items = (
            db.query(PromptTemplate)
            .filter_by(name="sve-feed")
            .order_by(PromptTemplate.version)
            .all()
        )
        assert [item.version for item in items] == [1, 2]
        assert db.query(AuditLog).filter_by(action="prompt.created").count() == 2
    rejected = client.post(
        "/prompts",
        data={
            "csrf_token": token,
            "name": "bad",
            "prompt_kind": "image",
            "post_type": "announcement",
            "media_kind": "feed",
            "prompt_body": "{{ invented }}",
            "model": "gpt-image-2",
            "quality": "medium",
        },
    )
    assert rejected.status_code == 422


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
