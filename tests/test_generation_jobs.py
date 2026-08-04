from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import Settings
from app.jobs import generation
from app.models import (
    Game,
    GenerationJob,
    GenerationJobStatus,
    InstagramPage,
    Post,
    PostStatus,
    Role,
    Team,
    User,
)


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


def test_ambiguous_openai_timeout_requires_manual_review(db, monkeypatch, tmp_path):
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
    assert result.status == GenerationJobStatus.MANUAL_REVIEW_REQUIRED
    assert result.error_category == "ambiguous_external_response"
    assert result.active_key is None


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
    db.commit()
    generation.retry_job(db, job)
    assert job.status == GenerationJobStatus.QUEUED
    assert job.active_key == f"create:{game.id}:announcement"
