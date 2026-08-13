from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import admin_routes
from app.config import Settings
from app.models import (
    AccountType,
    AiPromptDispatch,
    Club,
    Game,
    GenerationJob,
    GenerationJobStatus,
    GenerationJobType,
    InstagramPage,
    LogoAsset,
    MediaAsset,
    PlanProfile,
    PromptStatus,
    PromptTemplate,
    Role,
    Team,
    User,
)
from app.platform import routes as platform_routes
from app.tenancy.state import platform_scope, system_scope


def request(path: str, csrf: str = "platform-csrf") -> Request:
    parsed = urlsplit(path)
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": parsed.path,
            "query_string": parsed.query.encode("ascii"),
            "headers": [],
            "scheme": "https",
            "server": ("testserver", 443),
            "client": ("testclient", 123),
            "session": {"csrf": csrf},
        }
    )


def graph(db):
    page = InstagramPage(
        internal_name="prompt-observability",
        display_name="Prompt Observability",
        username="prompt_observability",
        club="Beispielverein",
        active=True,
    )
    db.add(page)
    db.flush()
    team = Team(
        internal_name="prompt-observability",
        display_name="Beispielverein Erste",
        short_name="Erste",
        slug="prompt-observability",
        club="Beispielverein",
        fussball_url="https://example.invalid/team",
        instagram_page_id=page.id,
        media_subdir="prompt-observability",
    )
    db.add(team)
    db.flush()
    game = Game(
        team_id=team.id,
        provider="fixture",
        external_id="prompt-observability-game",
        home_team=team.display_name,
        away_team="Gastverein",
        kickoff=datetime.now(timezone.utc) + timedelta(days=2),
        competition="Bezirksliga",
        venue="Beispielstadion",
        pitch="Rasenplatz",
        source_url="https://example.invalid/game",
    )
    club_user = User(
        email="club-prompt@example.invalid",
        password_hash="x",
        role=Role.ADMIN,
        all_teams=True,
    )
    db.add_all([game, club_user])
    db.flush()
    job = GenerationJob(
        job_type=GenerationJobType.CREATE_POST,
        game_id=game.id,
        team_id=team.id,
        post_type="announcement",
        requested_by=club_user.id,
        status=GenerationJobStatus.RUNNING,
        attempts=1,
        idempotency_key="prompt-observability-job",
        active_key="prompt-observability-active",
    )
    db.add(job)
    db.commit()
    return team, game, club_user, job


def platform_admin(db) -> User:
    with system_scope("PlatformAdmin für Prompt-Tests anlegen"):
        actor = User(
            email="platform-prompts@example.invalid",
            password_hash="x",
            role=Role.ADMIN,
            account_type=AccountType.PLATFORM_ADMIN,
            club_id=None,
        )
        db.add(actor)
        db.commit()
        return actor


def test_platform_prompt_history_filters_exact_prompts_and_blocks_club_user(db):
    team, game, club_user, job = graph(db)
    text_secret = "EXAKTER TEXT-PROMPT NUR FÜR PLATFORMADMIN"
    image_secret = "EXAKTER BILD-PROMPT NUR FÜR PLATFORMADMIN"
    db.add_all(
        [
            AiPromptDispatch(
                club_id=job.club_id,
                generation_job_id=job.id,
                team_id=team.id,
                game_id=game.id,
                prompt_kind="text",
                post_type="announcement",
                media_kind="none",
                provider="openai",
                model="text-model",
                prompt_checksum="a" * 64,
                rendered_prompt=text_secret,
                attempt_number=1,
                call_index=1,
                status="completed",
                idempotency_key="exact-text-prompt",
                dispatched_at=datetime.now(timezone.utc),
            ),
            AiPromptDispatch(
                club_id=job.club_id,
                generation_job_id=job.id,
                team_id=team.id,
                game_id=game.id,
                prompt_kind="image",
                post_type="announcement",
                media_kind="feed",
                provider="openai",
                model="image-model",
                prompt_checksum="b" * 64,
                rendered_prompt=image_secret,
                reference_images=[
                    {"role": "player", "media_asset_id": "protected-player-reference"}
                ],
                attempt_number=1,
                call_index=1,
                status="completed",
                idempotency_key="exact-image-prompt",
                dispatched_at=datetime.now(timezone.utc),
            ),
        ]
    )
    db.commit()
    actor = platform_admin(db)

    with platform_scope(actor.id):
        response = platform_routes.ai_generation_prompts(
            request("/platform/ai-generations"),
            club_id=job.club_id,
            prompt_kind="text",
            status="completed",
            limit=100,
            current=actor,
            db=db,
        )
    html = response.body.decode("utf-8")
    assert text_secret in html
    assert image_secret not in html
    assert team.display_name in html

    with platform_scope(actor.id):
        image_response = platform_routes.ai_generation_prompts(
            request("/platform/ai-generations"),
            club_id=job.club_id,
            prompt_kind="image",
            status="completed",
            limit=100,
            current=actor,
            db=db,
        )
    image_html = image_response.body.decode("utf-8")
    image_dispatch = db.query(AiPromptDispatch).filter_by(prompt_kind="image").one()
    assert "An die KI gesendete Bilder (1)" in image_html
    assert "Spielerbild" in image_html
    assert f"/platform/ai-generations/{image_dispatch.id}/references/0" in image_html

    with pytest.raises(HTTPException) as denied:
        platform_routes.ai_generation_prompts(
            request("/platform/ai-generations"),
            current=club_user,
            db=db,
        )
    assert denied.value.status_code == 403


def test_prompt_reference_image_is_platform_only_and_tenant_bound(db, tmp_path, monkeypatch):
    team, game, club_user, job = graph(db)
    actor = platform_admin(db)
    upload_root = tmp_path / "uploads"
    media_root = tmp_path / "external"
    generated_root = tmp_path / "generated"
    image_path = upload_root / "players" / "reference.png"
    image_path.parent.mkdir(parents=True)
    media_root.mkdir()
    generated_root.mkdir()
    image_path.write_bytes(b"protected-image-bytes")
    asset = MediaAsset(
        club_id=job.club_id,
        team_id=team.id,
        storage_kind="upload",
        relative_path="players/reference.png",
        filename="reference.png",
        mime_type="image/png",
        size=image_path.stat().st_size,
        width=1080,
        height=1350,
        checksum="c" * 64,
        mtime=datetime.now(timezone.utc),
        uploaded_by=club_user.id,
        active=True,
        available=True,
    )
    dispatch = AiPromptDispatch(
        club_id=job.club_id,
        generation_job_id=job.id,
        team_id=team.id,
        game_id=game.id,
        prompt_kind="image",
        post_type="announcement",
        media_kind="feed",
        provider="openai",
        model="image-model",
        prompt_checksum="d" * 64,
        rendered_prompt="Geschützter Bildprompt",
        reference_images=[],
        attempt_number=1,
        call_index=1,
        status="completed",
        idempotency_key="protected-reference-route",
    )
    db.add_all([asset, dispatch])
    db.flush()
    dispatch.reference_images = [{"role": "player", "media_asset_id": asset.id}]
    db.commit()
    monkeypatch.setattr(
        platform_routes,
        "get_settings",
        lambda: Settings(
            upload_root=upload_root,
            media_root=media_root,
            generated_root=generated_root,
        ),
    )

    with pytest.raises(HTTPException) as denied:
        platform_routes.ai_generation_reference_image(
            dispatch.id,
            0,
            current=club_user,
            db=db,
        )
    assert denied.value.status_code == 403

    with system_scope("Fremde Bildreferenz für Isolationstest anlegen"):
        profile = db.query(PlanProfile).first()
        other_club = Club(
            name="Anderer Verein",
            short_name="AV",
            slug="anderer-verein-prompt-test",
            status="ACTIVE",
            timezone="Europe/Berlin",
            plan_profile_id=profile.id,
        )
        db.add(other_club)
        db.flush()
        foreign_logo = LogoAsset(
            club_id=other_club.id,
            logo_type="opponent",
            display_name="Fremdes Logo",
            normalized_name="fremdes-logo",
            original_path="foreign/logo.png",
            original_filename="logo.png",
            mime_type="image/png",
            size=100,
            width=100,
            height=100,
            checksum="e" * 64,
            active=True,
            uploaded_by=actor.id,
        )
        db.add(foreign_logo)
        db.flush()
        dispatch.reference_images = [
            {"role": "opponent_logo", "logo_asset_id": foreign_logo.id}
        ]
        db.commit()
    with platform_scope(actor.id), pytest.raises(HTTPException) as cross_tenant:
        platform_routes.ai_generation_reference_image(
            dispatch.id,
            0,
            current=actor,
            db=db,
        )
    assert cross_tenant.value.status_code == 404

    dispatch.reference_images = [{"role": "player", "media_asset_id": asset.id}]
    db.commit()
    with platform_scope(actor.id):
        response = platform_routes.ai_generation_reference_image(
            dispatch.id,
            0,
            current=actor,
            db=db,
        )
    assert response.status_code == 200
    assert Path(response.path).resolve() == image_path.resolve()
    assert response.headers["cache-control"] == "private, no-store"


def test_existing_prompt_can_be_opened_and_saved_as_new_version(db):
    _team, _game, _club_user, _job = graph(db)
    actor = platform_admin(db)
    with platform_scope(actor.id):
        original = PromptTemplate(
            name="central-announcement-text",
            prompt_kind="text",
            post_type="announcement",
            media_kind="none",
            prompt_body="Alte geschützte Vorlage: {{ home_team }}",
            model="text-model",
            quality="default",
            status=PromptStatus.ACTIVE,
            active=True,
            version=1,
            created_by=actor.id,
        )
        db.add(original)
        db.commit()
        editor = admin_routes.prompts(
            request(f"/prompts?edit={original.id}"), current=actor, db=db
        )
        editor_html = editor.body.decode("utf-8")
        assert "Alte geschützte Vorlage" in editor_html
        assert "Neue Entwurfsversion speichern" in editor_html

        response = admin_routes.create_prompt(
            request("/prompts", csrf="edit-csrf"),
            csrf_token_value="edit-csrf",
            name="wird-serverseitig-ignoriert",
            prompt_kind="image",
            post_type="result",
            media_kind="feed",
            prompt_body="Neue geschützte Vorlage: {{ home_team }} gegen {{ away_team }}",
            style_direction="",
            model="text-model-v2",
            quality="default",
            base_prompt_id=original.id,
            change_description="Formulierung verständlicher gemacht",
            current=actor,
            db=db,
        )

    assert response.status_code == 303
    versions = (
        db.query(PromptTemplate)
        .filter(PromptTemplate.name == original.name)
        .order_by(PromptTemplate.version)
        .all()
    )
    assert [item.version for item in versions] == [1, 2]
    assert versions[0].prompt_body == "Alte geschützte Vorlage: {{ home_team }}"
    assert versions[1].prompt_kind == "text"
    assert versions[1].post_type == "announcement"
    assert versions[1].media_kind == "none"
    assert versions[1].status == PromptStatus.DRAFT
    assert versions[1].change_description == "Formulierung verständlicher gemacht"


def test_builtin_prompt_can_be_opened_and_saved_as_controlled_draft(db):
    _team, _game, _club_user, _job = graph(db)
    actor = platform_admin(db)
    builtin_key = "image:result:story"
    with platform_scope(actor.id):
        editor = admin_routes.prompts(
            request(f"/prompts?builtin={builtin_key}"), current=actor, db=db
        )
        editor_html = editor.body.decode("utf-8")
        assert "Bild · Ergebnis · Story" in editor_html
        assert "eingebaute Version 3 bleibt unverändert" in editor_html
        assert 'name="builtin_prompt_key" value="image:result:story"' in editor_html

        response = admin_routes.create_prompt(
            request("/prompts", csrf="builtin-csrf"),
            csrf_token_value="builtin-csrf",
            name="manipulierter-name",
            prompt_kind="text",
            post_type="announcement",
            media_kind="none",
            prompt_body="Neue Ergebnis-Story: {{ home_team }} {{ score }} {{ away_team }}",
            style_direction="kontrastreich",
            model="gpt-image-2",
            quality="medium",
            builtin_prompt_key=builtin_key,
            change_description="Ergebnisdarstellung verbessert",
            current=actor,
            db=db,
        )

    assert response.status_code == 303
    saved = db.scalar(
        db.query(PromptTemplate)
        .filter(
            PromptTemplate.name == "default-image-story",
            PromptTemplate.post_type == "result",
        )
        .statement
    )
    assert saved is not None
    assert saved.prompt_kind == "image"
    assert saved.media_kind == "story"
    assert saved.status == PromptStatus.DRAFT
