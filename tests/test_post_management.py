from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def test_deleting_bundle_removes_every_unpublished_member_and_file(db, tmp_path):
    page, first_team, first_game, user = graph(db)
    generated = tmp_path / "generated"
    upload = tmp_path / "uploads"
    generated.mkdir()
    upload.mkdir()
    second_team = Team(
        internal_name="post-management-two",
        display_name="SV Test II",
        short_name="SVT II",
        slug="post-management-two",
        club=first_team.club,
        fussball_url="https://www.fussball.de/test-two",
        instagram_page_id=page.id,
        media_subdir="test-two",
    )
    db.add(second_team)
    db.flush()
    second_game = Game(
        team_id=second_team.id,
        provider="mock",
        external_id="post-management-two",
        home_team="SV Test II",
        away_team="FC Beispiel II",
        kickoff=first_game.kickoff + timedelta(hours=2),
        competition="Testliga",
        venue="Teststadion",
        pitch="Rasenplatz",
        source_url="fixture://post-management-two",
    )
    db.add(second_game)
    db.flush()
    first_media = generated / "first.png"
    second_media = generated / "second.png"
    first_media.write_bytes(b"first")
    second_media.write_bytes(b"second")
    primary, first_publication = post_with_feed(
        db, page, first_team, first_game, first_media
    )
    member, second_publication = post_with_feed(
        db, page, second_team, second_game, second_media
    )
    member_ids = [primary.id, member.id]
    for item, role in ((primary, "primary"), (member, "member")):
        item.design_snapshot = {
            "club_matchday_carousel": {
                "primary_post_id": primary.id,
                "member_post_ids": member_ids,
                "role": role,
            }
        }
    db.commit()

    result = delete_unpublished_post(
        db,
        Settings(generated_root=generated, upload_root=upload),
        primary,
        user,
        expected_version=primary.version,
    )

    assert result.posts == 2
    assert result.publication_jobs == 2
    assert db.get(Post, primary.id) is None
    assert db.get(Post, member.id) is None
    assert db.get(PublicationJob, first_publication.id) is None
    assert db.get(PublicationJob, second_publication.id) is None
    assert not first_media.exists()
    assert not second_media.exists()
    assert db.query(AuditLog).filter_by(action="post.deleted").count() == 2


def test_deleting_incomplete_legacy_bundle_removes_surviving_post(db, tmp_path):
    page, team, game, user = graph(db)
    generated = tmp_path / "generated"
    upload = tmp_path / "uploads"
    generated.mkdir()
    upload.mkdir()
    media = generated / "legacy.png"
    media.write_bytes(b"legacy")
    post, publication = post_with_feed(db, page, team, game, media)
    missing_post_id = "00000000-0000-0000-0000-000000000099"
    post.design_snapshot = {
        "club_matchday_carousel": {
            "primary_post_id": post.id,
            "member_post_ids": [post.id, missing_post_id],
            "role": "primary",
        }
    }
    db.commit()

    result = delete_unpublished_post(
        db,
        Settings(generated_root=generated, upload_root=upload),
        post,
        user,
        expected_version=post.version,
    )

    assert result.posts == 1
    assert db.get(Post, post.id) is None
    assert db.get(PublicationJob, publication.id) is None
    assert not media.exists()


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


def test_targeted_media_revision_enqueue_persists_exact_source_and_one_output(db):
    page, team, game, user = graph(db)
    post, _ = post_with_feed(db, page, team, game, "unused.png")

    job = generation.enqueue_ai_revision(
        db,
        post,
        user,
        post.version,
        "Verschiebe nur den Spieler etwas nach rechts und ändere sonst nichts.",
        revise_text=False,
        revise_graphics=True,
        revise_feed=True,
        story_job_ids=[],
        feed_positions=[2],
        revision_mode="targeted_edit",
        source_media_version_id="selected-version-id",
        target_media_slot_id="selected-slot-id",
    )

    assert job.status == GenerationJobStatus.QUEUED
    assert job.planned_outputs == 1
    assert job.parameters["revision_mode"] == "targeted_edit"
    assert job.parameters["source_media_version_id"] == "selected-version-id"
    assert job.parameters["target_media_slot_id"] == "selected-slot-id"
    assert job.parameters["feed_positions"] == [2]
    assert job.parameters["revise_text"] is False


def test_post_detail_uses_one_media_catalog_with_per_image_actions():
    source = Path("app/templates/post_detail.html").read_text(encoding="utf-8")

    assert source.count("Medien für die Veröffentlichung") == 1
    assert "Zur Veröffentlichung: Version" in source
    assert "Im Entwurf ausgewählt: Version" in source
    assert "Für Veröffentlichung übernehmen" in source
    assert "media_version.id==slot.selected_version_id %}selected" not in source
    assert "Medienausgaben und Versionen" not in source
    assert "Dieses Karussell enthält genau" in source
    assert "publication.selected_version" in source
    assert "aktuell eingeplant" in source
    assert "/ai-edit" in source
    assert "Dieses Bild gezielt ändern" in source
    assert "Dieses Bild komplett neu erstellen" in source
