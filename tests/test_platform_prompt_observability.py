from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import admin_routes
from app.models import (
    AccountType,
    AiPromptDispatch,
    Game,
    GenerationJob,
    GenerationJobStatus,
    GenerationJobType,
    InstagramPage,
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

    with pytest.raises(HTTPException) as denied:
        platform_routes.ai_generation_prompts(
            request("/platform/ai-generations"),
            current=club_user,
            db=db,
        )
    assert denied.value.status_code == 403


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
