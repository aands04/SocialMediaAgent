import re
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import create_engine
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
    InstagramPage,
    LogoAsset,
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


def logo_png(color=(20, 90, 200, 255)):
    buffer = BytesIO()
    image = Image.new("RGBA", (160, 160), color)
    ImageDraw.Draw(image).rectangle((35, 35, 125, 125), fill=(255, 255, 255, 255))
    image.save(buffer, "PNG")
    return buffer.getvalue()


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
    assert "verifiziert" in client.get("/teams").text
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
        assert db.get(Game, second_id).opponent_logo_id == logo_id


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
            "home_team": "SV Test",
            "away_team": "FC Fixture",
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
