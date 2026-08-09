from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import Settings
from app.jobs import generation
from app.models import (
    AiPromptDispatch,
    Club,
    ClubStatus,
    Game,
    GenerationJob,
    GenerationJobStatus,
    InstagramPage,
    Post,
    PostStatus,
    Role,
    Team,
    UsageLedgerEntry,
    UsageStatus,
    User,
)
from app.textgen.service import GeneratedText


def graph(db):
    page = InstagramPage(
        internal_name="jobs", display_name="Jobs", username="jobs", club="SV", active=True
    )
    db.add(page)
    db.flush()
    team = Team(
        internal_name="jobs",
        display_name="SV Jobs",
        short_name="SVJ",
        slug="jobs",
        club="SV Jobs",
        fussball_url="https://www.fussball.de/jobs",
        instagram_page_id=page.id,
        media_subdir="jobs",
    )
    db.add(team)
    db.flush()
    game = Game(
        team_id=team.id,
        provider="mock",
        external_id="generation-job",
        home_team="SV Jobs",
        away_team="FC Test",
        kickoff=datetime.now(timezone.utc) + timedelta(days=2),
        source_url="fixture://jobs",
    )
    user = User(email="jobs@test.invalid", password_hash="x", role=Role.ADMIN, all_teams=True)
    db.add_all([game, user])
    db.commit()
    return page, team, game, user


def test_duplicate_create_click_returns_same_job(db):
    _, team, game, user = graph(db)
    first, post = generation.enqueue_create(db, game, team, user, "announcement")
    second, post2 = generation.enqueue_create(db, game, team, user, "announcement")
    assert post is None and post2 is None
    assert first.id == second.id
    assert (
        db.scalar(select(GenerationJob).where(GenerationJob.active_key.is_not(None))).id == first.id
    )


def test_worker_claim_and_success_are_persistent(db, monkeypatch, tmp_path):
    page, team, game, user = graph(db)
    job, _ = generation.enqueue_create(db, game, team, user, "announcement")
    claimed = generation.claim_next(db, "worker-a")
    assert claimed == job.id
    monkeypatch.setattr(generation, "build_renderer", lambda settings: object())
    monkeypatch.setattr(generation, "build_text_generator", lambda settings: object())

    def fake_create(
        session, game_arg, team_arg, generator, renderer, post_type, logo_snapshot=None
    ):
        post = Post(
            game_id=game_arg.id,
            team_id=team_arg.id,
            instagram_page_id=page.id,
            post_type=post_type,
            status=PostStatus.PENDING,
        )
        session.add(post)
        session.commit()
        return post

    monkeypatch.setattr(generation, "create_post", fake_create)
    result = generation.process_generation_job(
        db, claimed, Settings(generated_root=tmp_path, media_root=tmp_path)
    )
    assert result.status == GenerationJobStatus.SUCCEEDED
    assert result.result_post_id and result.progress == 100
    assert result.active_key is None


def test_automatic_generation_can_use_existing_approval_service(db, monkeypatch, tmp_path):
    _, team, game, user = graph(db)
    job, _ = generation.enqueue_create(db, game, team, user, "announcement")
    job.parameters = {
        **(job.parameters or {}),
        "trigger_mode": "automatic_fussball",
        "automatic_approval_requested": True,
    }
    db.commit()
    claimed = generation.claim_next(db, "worker-auto-approval")
    monkeypatch.setattr(generation, "build_renderer", lambda settings: object())
    monkeypatch.setattr(generation, "build_text_generator", lambda settings: object())

    def fake_create(
        session, game_arg, team_arg, generator, renderer, post_type, logo_snapshot=None
    ):
        post = Post(
            game_id=game_arg.id,
            team_id=team_arg.id,
            instagram_page_id=team_arg.instagram_page_id,
            post_type=post_type,
            status=PostStatus.PENDING,
        )
        session.add(post)
        session.commit()
        return post

    approved = []
    monkeypatch.setattr(generation, "create_post", fake_create)
    monkeypatch.setattr(
        generation,
        "approve",
        lambda session, post, actor: approved.append((post.id, actor.id)),
    )
    result = generation.process_generation_job(
        db, claimed, Settings(generated_root=tmp_path, media_root=tmp_path)
    )
    assert result.status == GenerationJobStatus.SUCCEEDED
    assert approved == [(result.result_post_id, user.id)]


def test_progress_renderer_passes_persistent_job_identity(db, tmp_path):
    _, team, game, user = graph(db)
    job, _ = generation.enqueue_create(db, game, team, user, "announcement")

    class CapturingRenderer:
        is_ai = True

        def render(self, kind, relative_path, context):
            self.kind = kind
            self.relative_path = relative_path
            self.context = context
            return tmp_path / "rendered.png"

    inner = CapturingRenderer()
    renderer = generation._ProgressRenderer(inner, db, job)
    renderer.render("feed", "post/feed-v1.png", {})

    assert inner.context["_generation_job_id"] == job.id


def test_progress_renderer_reuses_valid_output_without_new_usage_reservation(db, tmp_path):
    _, team, game, user = graph(db)
    job, _ = generation.enqueue_create(db, game, team, user, "announcement")
    existing = tmp_path / "existing.png"
    existing.write_bytes(b"already-validated-by-provider")

    class ReusableRenderer:
        is_ai = True

        def reusable_output(self, target, generation_job_id, kind):
            assert target == "post/feed-v1.png"
            assert generation_job_id == "previous-job"
            assert kind == "feed"
            return existing

        def render(self, kind, relative_path, context):
            assert context["_generation_job_id"] == job.id
            assert context["_reuse_generation_job_id"] == "previous-job"
            return existing

    result = generation._ProgressRenderer(
        ReusableRenderer(), db, job, reuse_generation_job_id="previous-job"
    ).render("feed", "post/feed-v1.png", {})

    assert result == existing
    assert job.completed_outputs == 1
    assert db.query(UsageLedgerEntry).count() == 0
    assert db.query(AiPromptDispatch).count() == 0


def test_progress_text_generator_records_exact_provider_prompt(db):
    _, _team, _game, user = graph(db)
    job = generation.enqueue_create(
        db,
        db.query(Game).one(),
        db.query(Team).one(),
        user,
        "announcement",
    )[0]

    class Prompt:
        rendered = "EXAKTER VERSANDPROMPT MIT LAUFZEITDATEN"
        name = "test-prompt"
        version = 7
        model = "test-text-model"
        template_id = None

    class TextProvider:
        is_ai = True

        def prepare_generate(self, data):
            return data["text_prompt"].rendered, "test-prompt:v7", "test-text-model"

        def generate(self, data):
            assert data["text_prompt"].rendered == Prompt.rendered
            return GeneratedText("Ergebnistext", "test-text-model", "test-prompt:v7")

    result = generation._ProgressTextGenerator(TextProvider(), db, job).generate(
        {"text_prompt": Prompt()}
    )
    dispatch = db.query(AiPromptDispatch).one()

    assert result.text == "Ergebnistext"
    assert dispatch.club_id == job.club_id
    assert dispatch.generation_job_id == job.id
    assert dispatch.prompt_kind == "text"
    assert dispatch.rendered_prompt == Prompt.rendered
    assert dispatch.prompt_name == "test-prompt"
    assert dispatch.prompt_version == 7
    assert dispatch.status == "completed"
    usage = db.query(UsageLedgerEntry).one()
    assert usage.idempotency_key == f"generation:{job.id}:attempt:1:text:1"


def test_known_technical_retry_gets_a_new_billable_usage_reservation(db):
    _, _team, _game, user = graph(db)
    job = generation.enqueue_create(
        db,
        db.query(Game).one(),
        db.query(Team).one(),
        user,
        "announcement",
    )[0]

    class Prompt:
        rendered = "TESTPROMPT"
        name = "test-prompt"
        version = 1
        model = "test-text-model"
        template_id = None

    class TextProvider:
        is_ai = True

        def __init__(self, fail):
            self.fail = fail

        def prepare_generate(self, data):
            return data["text_prompt"].rendered, "test-prompt:v1", "test-text-model"

        def generate(self, data):
            if self.fail:
                raise ConnectionError("peer closed connection")
            return GeneratedText("Ergebnistext", "test-text-model", "test-prompt:v1")

    job.attempts = 1
    with pytest.raises(ConnectionError):
        generation._ProgressTextGenerator(TextProvider(True), db, job).generate(
            {"text_prompt": Prompt()}
        )
    generation._finalize_job_usage(db, job, None)
    db.commit()

    job.attempts = 2
    result = generation._ProgressTextGenerator(TextProvider(False), db, job).generate(
        {"text_prompt": Prompt()}
    )
    entries = db.query(UsageLedgerEntry).order_by(UsageLedgerEntry.created_at).all()

    assert result.text == "Ergebnistext"
    assert [entry.status for entry in entries] == [
        UsageStatus.FAILED_TECHNICAL,
        UsageStatus.COMPLETED_BILLABLE,
    ]
    assert entries[0].idempotency_key.endswith(":attempt:1:text:1")
    assert entries[1].idempotency_key.endswith(":attempt:2:text:1")


def test_grouped_dashboard_click_enqueues_one_coordinator_job(db):
    page, first, first_game, user = graph(db)
    first_game.kickoff = first_game.kickoff.replace(
        hour=13,
        minute=0,
        second=0,
        microsecond=0,
    )
    first.rules = {
        **(first.rules or {}),
        "announcement_enabled": True,
        "club_matchday_feed_mode": "announcements_and_results",
    }
    second = Team(
        internal_name="jobs-two",
        display_name="SV Jobs II",
        short_name="SVJ II",
        slug="jobs-two",
        club=first.club,
        fussball_url="https://www.fussball.de/jobs-two",
        instagram_page_id=page.id,
        media_subdir="jobs-two",
        rules={
            "announcement_enabled": True,
            "club_matchday_feed_mode": "announcements_and_results",
        },
    )
    db.add(second)
    db.flush()
    second_game = Game(
        team_id=second.id,
        provider="mock",
        external_id="generation-job-two",
        home_team=second.display_name,
        away_team="FC Test II",
        kickoff=first_game.kickoff + timedelta(hours=2),
        source_url="fixture://jobs-two",
    )
    db.add(second_game)
    db.commit()

    job, post = generation.enqueue_bundle_create(
        db, second_game, second, user, "announcement"
    )

    assert post is None
    assert job.parameters["single_shared_text_prompt"] is True
    assert job.parameters["bundle_game_ids"] == [first_game.id, second_game.id]
    assert db.query(GenerationJob).count() == 1


def test_incomplete_bundle_click_opens_surviving_post_without_new_ai_job(db):
    page, first, first_game, user = graph(db)
    first_game.kickoff = first_game.kickoff.replace(
        hour=13,
        minute=0,
        second=0,
        microsecond=0,
    )
    first.rules = {
        **(first.rules or {}),
        "announcement_enabled": True,
        "club_matchday_feed_mode": "announcements",
    }
    second = Team(
        internal_name="jobs-incomplete-two",
        display_name="SV Jobs II",
        short_name="SVJ II",
        slug="jobs-incomplete-two",
        club=first.club,
        fussball_url="https://www.fussball.de/jobs-incomplete-two",
        instagram_page_id=page.id,
        media_subdir="jobs-incomplete-two",
        rules={
            "announcement_enabled": True,
            "club_matchday_feed_mode": "announcements",
        },
    )
    db.add(second)
    db.flush()
    second_game = Game(
        team_id=second.id,
        provider="mock",
        external_id="generation-job-incomplete-two",
        home_team=second.display_name,
        away_team="FC Test II",
        kickoff=first_game.kickoff + timedelta(hours=2),
        source_url="fixture://jobs-incomplete-two",
    )
    surviving_post = Post(
        game_id=first_game.id,
        team_id=first.id,
        instagram_page_id=page.id,
        post_type="announcement",
        status=PostStatus.PENDING,
        text="Vorhandener Teilbeitrag",
    )
    db.add_all([second_game, surviving_post])
    db.commit()

    job, existing = generation.enqueue_bundle_create(
        db, second_game, second, user, "announcement"
    )

    assert job is None
    assert existing.id == surviving_post.id
    assert db.query(GenerationJob).count() == 0


def test_progress_renderer_records_exact_image_provider_prompt(db, tmp_path):
    _, team, game, user = graph(db)
    job = generation.enqueue_create(db, game, team, user, "announcement")[0]

    class Prompt:
        rendered = "EXAKTER BILDPROMPT MIT SPIEL- UND BRANDINGDATEN"
        name = "image-test-prompt"
        version = 4
        model = "test-image-model"
        template_id = None

    class ImageProvider:
        is_ai = True

        def render(self, kind, relative_path, context):
            assert kind == "story"
            assert context["image_prompt"].rendered == Prompt.rendered
            return tmp_path / relative_path

    result = generation._ProgressRenderer(ImageProvider(), db, job).render(
        "story",
        "post/story-v1.png",
        {"image_prompt": Prompt()},
    )
    dispatch = db.query(AiPromptDispatch).one()

    assert result == tmp_path / "post/story-v1.png"
    assert dispatch.club_id == job.club_id
    assert dispatch.generation_job_id == job.id
    assert dispatch.prompt_kind == "image"
    assert dispatch.media_kind == "story"
    assert dispatch.rendered_prompt == Prompt.rendered
    assert dispatch.prompt_name == "image-test-prompt"
    assert dispatch.prompt_version == 4
    assert dispatch.status == "completed"


def test_ambiguous_openai_timeout_schedules_one_delayed_retry(db, monkeypatch, tmp_path):
    _, team, game, user = graph(db)
    job, _ = generation.enqueue_create(db, game, team, user, "announcement")
    claimed = generation.claim_next(db, "worker-a")
    monkeypatch.setattr(generation, "build_renderer", lambda settings: object())
    monkeypatch.setattr(generation, "build_text_generator", lambda settings: object())
    monkeypatch.setattr(
        generation,
        "create_post",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            TimeoutError("upstream timed out after request")
        ),
    )
    result = generation.process_generation_job(
        db,
        claimed,
        Settings(
            text_generator_mode="openai",
            openai_api_key="test",
            generated_root=tmp_path,
            media_root=tmp_path,
        ),
    )
    assert result.status == GenerationJobStatus.RETRY_WAIT
    assert result.error_category == "external_service_temporarily_unavailable"
    assert "frühestens in 60 Sekunden" in result.error_message
    assert "upstream timed out" not in result.error_message
    assert result.available_at >= datetime.now(timezone.utc) + timedelta(seconds=58)
    assert result.active_key == f"create:{game.id}:announcement"


def test_ambiguous_openai_timeout_stops_after_one_automatic_retry(
    db, monkeypatch, tmp_path
):
    _, team, game, user = graph(db)
    job, _ = generation.enqueue_create(db, game, team, user, "announcement")
    monkeypatch.setattr(generation, "build_renderer", lambda settings: object())
    monkeypatch.setattr(generation, "build_text_generator", lambda settings: object())
    monkeypatch.setattr(
        generation,
        "create_post",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ConnectionError("peer closed connection with Error code: 520")
        ),
    )
    settings = Settings(
        text_generator_mode="openai",
        openai_api_key="test",
        generated_root=tmp_path,
        media_root=tmp_path,
    )

    claimed = generation.claim_next(db, "worker-a")
    first = generation.process_generation_job(db, claimed, settings)
    assert first.status == GenerationJobStatus.RETRY_WAIT
    first.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    claimed = generation.claim_next(db, "worker-b")
    second = generation.process_generation_job(db, claimed, settings)
    assert second.status == GenerationJobStatus.MANUAL_REVIEW_REQUIRED
    assert second.error_category == "ambiguous_external_response"
    assert "begrenzten Wiederholungsversuch" in second.error_message
    assert "Error code: 520" not in second.error_message
    assert second.attempts == 2
    assert second.active_key is None


def test_legacy_job_with_exhausted_budget_does_not_claim_an_automatic_retry(
    db, monkeypatch, tmp_path
):
    _, team, game, user = graph(db)
    job, _ = generation.enqueue_create(db, game, team, user, "announcement")
    job.attempts = 8
    db.commit()
    monkeypatch.setattr(generation, "build_renderer", lambda settings: object())
    monkeypatch.setattr(generation, "build_text_generator", lambda settings: object())
    monkeypatch.setattr(
        generation,
        "create_post",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ConnectionError("peer closed connection with Error code: 520")
        ),
    )

    claimed = generation.claim_next(db, "worker-legacy")
    result = generation.process_generation_job(
        db,
        claimed,
        Settings(
            text_generator_mode="openai",
            openai_api_key="test",
            generated_root=tmp_path,
            media_root=tmp_path,
        ),
    )

    assert result.status == GenerationJobStatus.MANUAL_REVIEW_REQUIRED
    assert result.attempts == 9
    assert "kein weiterer automatischer Wiederholungsversuch" in result.error_message
    assert "auch beim begrenzten Wiederholungsversuch" not in result.error_message


def test_ambiguous_openai_timeout_retries_only_missing_output_after_usable_output(
    db, monkeypatch, tmp_path
):
    _, team, game, user = graph(db)
    job, _ = generation.enqueue_create(db, game, team, user, "announcement")
    claimed = generation.claim_next(db, "worker-a")
    job = db.get(GenerationJob, claimed)
    job.completed_outputs = 1
    db.commit()
    monkeypatch.setattr(generation, "build_renderer", lambda settings: object())
    monkeypatch.setattr(generation, "build_text_generator", lambda settings: object())
    monkeypatch.setattr(
        generation,
        "create_post",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ConnectionError("peer closed connection with Error code: 520")
        ),
    )

    result = generation.process_generation_job(
        db,
        claimed,
        Settings(
            text_generator_mode="openai",
            openai_api_key="test",
            generated_root=tmp_path,
            media_root=tmp_path,
        ),
    )

    assert result.status == GenerationJobStatus.RETRY_WAIT
    assert "Bereits gespeicherte Ergebnisse bleiben erhalten" in result.error_message
    assert result.completed_outputs == 1
    assert result.active_key == f"create:{game.id}:announcement"


def test_stale_job_during_costly_phase_is_not_retried(db):
    _, team, game, user = graph(db)
    job, _ = generation.enqueue_create(db, game, team, user, "announcement")
    generation.claim_next(db, "dead-worker")
    job = db.get(GenerationJob, job.id)
    job.phase = "generating_feed"
    job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    assert generation.recover_stale_jobs(db) == 1
    db.refresh(job)
    assert job.status == GenerationJobStatus.MANUAL_REVIEW_REQUIRED


def test_cancel_queued_job_and_manual_retry(db):
    _, team, game, user = graph(db)
    job, _ = generation.enqueue_create(db, game, team, user, "announcement")
    generation.request_cancel(db, job)
    assert job.status == GenerationJobStatus.CANCELLED
    job.status = GenerationJobStatus.FAILED
    job.attempts = 9
    db.commit()
    retry = generation.retry_job(db, job, user)
    assert retry.id != job.id
    assert retry.status == GenerationJobStatus.QUEUED
    assert retry.attempts == 0
    assert retry.active_key == f"create:{game.id}:announcement"
    assert retry.parameters["manual_retry_of_job_id"] == job.id
    assert ":manual-retry:" in retry.idempotency_key
    db.refresh(job)
    assert job.status == GenerationJobStatus.FAILED
    assert job.attempts == 9
    assert job.active_key is None


def test_manual_retry_is_blocked_after_usable_output(db):
    _, team, game, user = graph(db)
    job, _ = generation.enqueue_create(db, game, team, user, "announcement")
    job.status = GenerationJobStatus.MANUAL_REVIEW_REQUIRED
    job.completed_outputs = 1
    job.active_key = None
    db.commit()

    with pytest.raises(ValueError, match="verwendbare Ausgabe"):
        generation.retry_job(db, job, user)

    assert db.scalar(select(GenerationJob).where(GenerationJob.id != job.id)) is None


def test_manual_retry_continues_linked_incomplete_post_with_fresh_budget(db):
    page, team, game, user = graph(db)
    game.home_score = 2
    game.away_score = 1
    game.result_confirmed = True
    db.commit()
    job, _ = generation.enqueue_create(db, game, team, user, "result")
    partial = Post(
        game_id=game.id,
        team_id=team.id,
        instagram_page_id=page.id,
        post_type="result",
        status=PostStatus.INCOMPLETE,
        text="Ein bereits verwendbarer Ergebnistext",
        critical_warnings=[generation.PARTIAL_GENERATION_WARNING],
    )
    db.add(partial)
    db.flush()
    job.status = GenerationJobStatus.MANUAL_REVIEW_REQUIRED
    job.completed_outputs = 1
    job.post_id = partial.id
    job.result_post_id = partial.id
    job.active_key = None
    db.commit()

    retry = generation.retry_job(db, job, user)

    assert retry.post_id == partial.id
    assert retry.parameters["resume_incomplete_post_id"] == partial.id
    assert retry.parameters["resume_generation_job_id"] == job.id
    assert retry.max_attempts >= retry.planned_outputs + 1
    assert retry.completed_outputs == 0


def test_partial_bundle_marks_every_member_and_can_resume_from_primary(db):
    page, first_team, first_game, user = graph(db)
    second_team = Team(
        internal_name="jobs-bundle-two",
        display_name="SV Jobs II",
        short_name="SVJ II",
        slug="jobs-bundle-two",
        club=first_team.club,
        fussball_url="https://www.fussball.de/jobs-bundle-two",
        instagram_page_id=page.id,
        media_subdir="jobs-bundle-two",
    )
    db.add(second_team)
    db.flush()
    second_game = Game(
        team_id=second_team.id,
        provider="mock",
        external_id="generation-job-bundle-two",
        home_team=second_team.display_name,
        away_team="FC Test II",
        kickoff=first_game.kickoff + timedelta(hours=2),
        source_url="fixture://jobs-bundle-two",
    )
    db.add(second_game)
    db.flush()
    first_post = Post(
        game_id=first_game.id,
        team_id=first_team.id,
        instagram_page_id=page.id,
        post_type="announcement",
        status=PostStatus.PENDING,
        text="Bereits erzeugter gemeinsamer Begleittext",
        feed_path="first/feed.png",
    )
    second_post = Post(
        game_id=second_game.id,
        team_id=second_team.id,
        instagram_page_id=page.id,
        post_type="announcement",
        status=PostStatus.CREATING,
        text="Bereits erzeugter gemeinsamer Begleittext",
    )
    db.add_all([first_post, second_post])
    db.flush()
    job = GenerationJob(
        club_id=first_team.club_id,
        job_type=generation.GenerationJobType.CREATE_POST,
        game_id=first_game.id,
        team_id=first_team.id,
        requested_by=user.id,
        post_type="announcement",
        status=GenerationJobStatus.MANUAL_REVIEW_REQUIRED,
        phase="generating_ai_composition",
        planned_outputs=4,
        completed_outputs=2,
        attempts=2,
        max_attempts=3,
        idempotency_key="bundle-partial-source",
        parameters={
            "matchday_bundle_key": "test-bundle",
            "bundle_game_ids": [first_game.id, second_game.id],
        },
    )
    db.add(job)
    db.flush()

    generation._capture_partial_post(db, job)
    job.active_key = None
    db.commit()

    for post in (first_post, second_post):
        db.refresh(post)
        assert post.status == PostStatus.INCOMPLETE
        assert generation.PARTIAL_GENERATION_WARNING in post.critical_warnings
    assert job.result_post_id == first_post.id

    retry = generation.retry_job(db, job, user)

    assert retry.post_id == first_post.id
    assert retry.parameters["resume_incomplete_post_id"] == first_post.id
    assert retry.parameters["resume_generation_job_id"] == job.id
    assert retry.parameters["bundle_game_ids"] == [first_game.id, second_game.id]


def test_worker_stops_when_club_is_suspended_after_claim(db, monkeypatch, tmp_path):
    _, team, game, user = graph(db)
    job, _ = generation.enqueue_create(db, game, team, user, "announcement")
    claimed = generation.claim_next(db, "worker-suspended-club")
    club = db.get(Club, job.club_id)
    club.status = ClubStatus.SUSPENDED
    db.commit()

    called = False

    def unexpected_create(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("Bei gesperrtem Verein darf keine Generierung beginnen")

    monkeypatch.setattr(generation, "create_post", unexpected_create)
    result = generation.process_generation_job(
        db, claimed, Settings(generated_root=tmp_path, media_root=tmp_path)
    )

    assert called is False
    assert result.status == GenerationJobStatus.FAILED
    assert result.error_category == "permission_changed"
    assert "Verein ist gesperrt" in result.error_message
