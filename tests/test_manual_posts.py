from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from app.approvals.service import approve
from app.config import Settings
from app.meta.scheduler import _candidate_ids
from app.models import (
    InstagramConnection,
    InstagramPage,
    JobStatus,
    PostStatus,
    PublicationJob,
    Role,
    Team,
    User,
)
from app.posts.manual import (
    ManualPostError,
    create_manual_post,
    parse_manual_publication_time,
    validate_manual_image,
)
from app.publishing.service import DryRunPublisher
from app.publishing.worker import process_job


def image_bytes(size=(1080, 1350), image_format="PNG") -> bytes:
    buffer = BytesIO()
    image = Image.new("RGB", size, (20, 60, 140))
    ImageDraw.Draw(image).rectangle((120, 120, 720, 900), fill=(240, 210, 30))
    image.save(buffer, image_format)
    return buffer.getvalue()


def setup_manual_context(db):
    user = User(
        email="manual@example.invalid",
        password_hash="not-used",
        role=Role.ADMIN,
        all_teams=True,
    )
    db.add(user)
    db.flush()
    page = InstagramPage(
        internal_name="manual-page",
        display_name="Manual Page",
        username="manualpage",
        account_id="ig-manual",
        club="SV Test",
        active=True,
        connection_status="connected",
        publishing_enabled=True,
        automatic_publishing_enabled=True,
        automatic_publishing_confirmed_by=user.id,
        automatic_publishing_confirmed_at=datetime.now(timezone.utc),
    )
    db.add(page)
    db.flush()
    team = Team(
        internal_name="manual-team",
        display_name="SV Test I",
        short_name="SVT",
        slug="manual-team",
        club="SV Test",
        fussball_url="https://example.invalid/team",
        instagram_page_id=page.id,
        media_subdir="manual/spieler",
        publishing_enabled=True,
    )
    db.add(team)
    db.commit()
    return user, page, team


def test_manual_feed_uses_normal_approval_and_dry_run_publishing(db, tmp_path):
    user, _page, team = setup_manual_context(db)
    image = validate_manual_image(
        "upload.png", "image/png", image_bytes(), "feed"
    )
    post, created = create_manual_post(
        db,
        Settings(
            generated_root=tmp_path / "generated",
            media_root=tmp_path / "media",
            upload_root=tmp_path / "uploads",
        ),
        team=team,
        user=user,
        submission_id="manual-feed-submission-1234567890",
        kind="feed",
        text="Unser selbst erstellter Beitrag",
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
        image=image,
    )
    assert created is True
    assert post.game_id is None
    assert post.status == PostStatus.PENDING
    job = db.query(PublicationJob).filter_by(post_id=post.id).one()
    assert job.game_id is None
    assert job.kind == "feed"
    assert job.status == JobStatus.UNAPPROVED
    assert job.text_snapshot == post.text

    duplicate, created_again = create_manual_post(
        db,
        Settings(generated_root=tmp_path / "generated"),
        team=team,
        user=user,
        submission_id="manual-feed-submission-1234567890",
        kind="feed",
        text="Wird wegen Idempotenz ignoriert",
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=2),
        image=image,
    )
    assert created_again is False
    assert duplicate.id == post.id

    approve(db, post, user)
    job.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    done = process_job(
        db,
        job.id,
        DryRunPublisher(),
        Settings(global_publish_enabled=True),
    )
    assert done.status == JobStatus.PUBLISHED
    assert post.status == PostStatus.PUBLISHED


def test_manual_story_has_no_platform_caption_and_needs_exact_dimensions(db, tmp_path):
    user, _page, team = setup_manual_context(db)
    with pytest.raises(ManualPostError, match="1080 × 1920"):
        validate_manual_image("wrong.png", "image/png", image_bytes(), "story")
    story_image = validate_manual_image(
        "story.webp", "image/webp", image_bytes((1080, 1920), "WEBP"), "story"
    )
    post, _ = create_manual_post(
        db,
        Settings(
            generated_root=tmp_path / "generated",
            media_root=tmp_path / "media",
            upload_root=tmp_path / "uploads",
        ),
        team=team,
        user=user,
        submission_id="manual-story-submission-123456789",
        kind="story",
        text="Interne Story-Dokumentation",
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
        image=story_image,
    )
    job = db.query(PublicationJob).filter_by(post_id=post.id).one()
    assert post.feed_path is None
    assert job.text_snapshot is None
    approve(db, post, user)
    assert job.status == JobStatus.SCHEDULED


def test_manual_time_rejects_past_and_dst_ambiguity():
    past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    with pytest.raises(ManualPostError, match="Zukunft"):
        parse_manual_publication_time(past, "Europe/Berlin")
    with pytest.raises(ManualPostError, match="Zeitumstellung"):
        parse_manual_publication_time("2026-10-25T02:30", "Europe/Berlin")


def test_automatic_scheduler_selects_due_manual_post(db, tmp_path):
    user, page, team = setup_manual_context(db)
    page.allowed_types = {"feed": True, "story": True}
    db.add(
        InstagramConnection(
            instagram_page_id=page.id,
            instagram_user_id="ig-manual",
            confirmed_username="manualpage",
            account_type="BUSINESS",
            scopes=["instagram_business_basic", "instagram_business_content_publish"],
            status="connected",
            token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            last_check_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    post, _ = create_manual_post(
        db,
        Settings(
            generated_root=tmp_path / "generated",
            media_root=tmp_path / "media",
            upload_root=tmp_path / "uploads",
        ),
        team=team,
        user=user,
        submission_id="manual-scheduler-submission-1234567",
        kind="feed",
        text="Automatisch nach Freigabe",
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
        image=validate_manual_image(
            "feed.png", "image/png", image_bytes(), "feed"
        ),
    )
    approve(db, post, user)
    job = db.query(PublicationJob).filter_by(post_id=post.id).one()
    job.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    ids = _candidate_ids(db, Settings(meta_scheduler_batch_size=5))
    assert ids == [job.id]
