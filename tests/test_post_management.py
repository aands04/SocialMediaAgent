from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import Settings
from app.jobs import generation
from app.models import (
    AuditLog,
    Game,
    GenerationJobStatus,
    InstagramPage,
    JobStatus,
    Post,
    PostStatus,
    PublicationJob,
    Role,
    Team,
    User,
)
from app.posts.deletion import PostDeletionConflict, delete_unpublished_post
from app.posts.service import revise_post
from app.textgen.service import FixtureTextGenerator


def graph(db):
    page = InstagramPage(
        internal_name="post-management",
        display_name="Post Management",
        username="post-management",
        club="SV",
        active=True,
    )
    db.add(page)
    db.flush()
    team = Team(
        internal_name="post-management",
        display_name="SV Test",
        short_name="SVT",
        slug="post-management",
        club="SV Test",
        fussball_url="https://www.fussball.de/test",
        instagram_page_id=page.id,
        media_subdir="test",
    )
    db.add(team)
    db.flush()
    game = Game(
        team_id=team.id,
        provider="mock",
        external_id="post-management",
        home_team="SV Test",
        away_team="FC Beispiel",
        kickoff=datetime.now(timezone.utc) + timedelta(days=2),
        competition="Testliga",
        venue="Teststadion",
        pitch="Rasenplatz",
        source_url="fixture://post-management",
    )
    user = User(
        email="post-management@test.invalid",
        password_hash="x",
        role=Role.ADMIN,
        all_teams=True,
    )
    db.add_all([game, user])
    db.commit()
    return page, team, game, user


def post_with_feed(db, page, team, game, media_path, status=PostStatus.PENDING):
    post = Post(
        game_id=game.id,
        team_id=team.id,
        instagram_page_id=page.id,
        post_type="announcement",
        status=status,
        text="Alter Begleittext",
        feed_path=str(media_path),
    )
    db.add(post)
    db.flush()
    publication = PublicationJob(
        post_id=post.id,
        game_id=game.id,
        team_id=team.id,
        instagram_page_id=page.id,
        kind="feed",
        media_path=str(media_path),
        text_snapshot=post.text,
        scheduled_at=game.kickoff - timedelta(days=1),
        idempotency_key=f"{post.id}:feed:v1",
    )
    db.add(publication)
    db.commit()
    return post, publication


def test_unpublished_post_is_deleted_with_files_jobs_and_audit(db, tmp_path):
    page, team, game, user = graph(db)
    generated = tmp_path / "generated"
    upload = tmp_path / "uploads"
    media = generated / "post" / "feed.png"
    media.parent.mkdir(parents=True)
    upload.mkdir()
    media.write_bytes(b"png-placeholder")
    post, publication = post_with_feed(db, page, team, game, media)

    result = delete_unpublished_post(
        db,
        Settings(generated_root=generated, upload_root=upload),
        post,
        user,
        expected_version=post.version,
        reason="Fehlentwurf",
    )

    assert db.get(Post, post.id) is None
    assert db.get(PublicationJob, publication.id) is None
    assert not media.exists()
    assert result.removed_files == 1
    audit = db.scalar(select(AuditLog).where(AuditLog.action == "post.deleted"))
    assert audit and audit.entity_id == post.id
    assert audit.details["reason"] == "Fehlentwurf"


def test_published_post_cannot_be_deleted(db, tmp_path):
    page, team, game, user = graph(db)
    generated = tmp_path / "generated"
    upload = tmp_path / "uploads"
    generated.mkdir()
    upload.mkdir()
    media = generated / "published.png"
    media.write_bytes(b"published")
    post, publication = post_with_feed(
        db, page, team, game, media, status=PostStatus.PUBLISHED
    )
    publication.status = JobStatus.PUBLISHED
    publication.platform_id = "instagram-1"
    db.commit()

    with pytest.raises(PostDeletionConflict, match="veröffentlichte Beiträge"):
        delete_unpublished_post(
            db,
            Settings(generated_root=generated, upload_root=upload),
            post,
            user,
            expected_version=post.version,
            reason="darf nicht",
        )
    assert media.exists()
    assert db.get(Post, post.id) is not None


def test_text_only_ai_revision_versions_text_and_revokes_rejection(db, tmp_path):
    page, team, game, _ = graph(db)
    post, publication = post_with_feed(
        db, page, team, game, tmp_path / "unused.png", status=PostStatus.REJECTED
    )
    publication.approval_status = "rejected"
    db.commit()

    revised = revise_post(
        db,
        post,
        instruction="Formuliere den Text emotionaler und einladender.",
        revise_text=True,
        revise_graphics=False,
        text_generator=FixtureTextGenerator(),
    )
    db.commit()

    assert revised.version == 2
    assert revised.text_version == 2
    assert revised.status == PostStatus.PENDING
    assert "Fixture-Änderungswunsch" in revised.text
    assert publication.status == JobStatus.UNAPPROVED
    assert publication.approval_status == "unapproved"
    assert revised.design_snapshot["ai_revisions"][-1]["text"] is True


def test_ai_revision_enqueue_is_idempotent(db):
    page, team, game, user = graph(db)
    post, _ = post_with_feed(db, page, team, game, "unused.png")
    first = generation.enqueue_ai_revision(
        db,
        post,
        user,
        post.version,
        "Erzeuge eine emotionalere Abendstimmung im Bild.",
        revise_text=False,
        revise_graphics=True,
        revise_feed=False,
        story_job_ids=["story-target"],
    )
    second = generation.enqueue_ai_revision(
        db,
        post,
        user,
        post.version,
        "Erzeuge eine emotionalere Abendstimmung im Bild.",
        revise_text=False,
        revise_graphics=True,
        revise_feed=False,
        story_job_ids=["story-target"],
    )
    assert first.id == second.id
    assert first.status == GenerationJobStatus.QUEUED
    assert first.parameters["operation"] == "ai_revision"
    assert first.parameters["revise_feed"] is False
    assert first.parameters["story_job_ids"] == ["story-target"]
    assert first.planned_outputs == 1
