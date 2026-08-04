from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select

from app.approvals.service import approve
from app.config import Settings
from app.meta.scheduler import _candidate_ids
from app.models import (
    InstagramConnection,
    InstagramPage,
    JobStatus,
    PostStatus,
    PublicationJob,
    PublicationMediaItem,
    Role,
    Team,
    User,
)
from app.posts.manual import (
    ManualPostError,
    create_manual_post,
    parse_manual_crop_specs,
    parse_manual_publication_time,
    parse_manual_user_tag_specs,
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
    image = validate_manual_image("upload.png", "image/png", image_bytes(), "feed")
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
        images=[image],
    )
    assert created is True
    assert post.game_id is None
    assert post.status == PostStatus.PENDING
    job = db.query(PublicationJob).filter_by(post_id=post.id).one()
    assert job.game_id is None
    assert job.kind == "feed"
    assert job.status == JobStatus.UNAPPROVED
    assert job.text_snapshot == post.text
    image_snapshot = post.design_snapshot["manual_upload"]["images"][0]
    original_path = Path(image_snapshot["original_path"])
    assert original_path.read_bytes() == image_bytes()
    assert image_snapshot["source_width"] == 1080
    assert image_snapshot["source_height"] == 1350

    duplicate, created_again = create_manual_post(
        db,
        Settings(generated_root=tmp_path / "generated"),
        team=team,
        user=user,
        submission_id="manual-feed-submission-1234567890",
        kind="feed",
        text="Wird wegen Idempotenz ignoriert",
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=2),
        images=[image],
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


def test_manual_story_crops_other_dimensions_and_has_no_platform_caption(db, tmp_path):
    user, _page, team = setup_manual_context(db)
    story_image = validate_manual_image(
        "story.webp", "image/webp", image_bytes((1400, 1000), "WEBP"), "story"
    )
    assert (story_image.width, story_image.height) == (1080, 1920)
    assert (story_image.source_width, story_image.source_height) == (1400, 1000)
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
        images=[story_image],
    )
    job = db.query(PublicationJob).filter_by(post_id=post.id).one()
    assert post.feed_path is None
    assert job.text_snapshot is None
    approve(db, post, user)
    assert job.status == JobStatus.SCHEDULED


def test_manual_image_uses_selected_crop_and_preserves_original():
    source = Image.new("RGB", (2000, 1000), (220, 30, 30))
    ImageDraw.Draw(source).rectangle((1200, 0, 1999, 999), fill=(20, 70, 220))
    buffer = BytesIO()
    source.save(buffer, "PNG")
    payload = buffer.getvalue()
    crop = {"x": 0.6, "y": 0, "width": 0.4, "height": 1}

    validated = validate_manual_image("wide.png", "image/png", payload, "feed", crop)

    assert validated.original == payload
    assert (validated.source_width, validated.source_height) == (2000, 1000)
    assert validated.crop == crop
    with Image.open(BytesIO(validated.png)) as output:
        assert output.size == (1080, 1350)
        assert output.getpixel((540, 675)) == (20, 70, 220)


def test_manual_crop_metadata_is_strictly_validated():
    assert parse_manual_crop_specs("", 2) == [None, None]
    assert parse_manual_crop_specs('[{"x":0.1,"y":0,"width":0.8,"height":1}]', 1) == [
        {"x": 0.1, "y": 0.0, "width": 0.8, "height": 1.0}
    ]
    with pytest.raises(ManualPostError, match="passen nicht"):
        parse_manual_crop_specs("[]", 1)
    with pytest.raises(ManualPostError, match="außerhalb"):
        parse_manual_crop_specs('[{"x":0.5,"y":0,"width":0.6,"height":1}]', 1)


def test_manual_instagram_user_tags_are_normalized_and_strictly_validated():
    parsed = parse_manual_user_tag_specs(
        '[[{"username":"@SV.Ehlen1901","x":0.25,"y":0.75}]]',
        1,
        "feed",
    )
    assert parsed == [[{"username": "sv.ehlen1901", "x": 0.25, "y": 0.75}]]
    with pytest.raises(ManualPostError, match="mehrfach"):
        parse_manual_user_tag_specs(
            '[[{"username":"svehlen1901","x":0.2,"y":0.3},'
            '{"username":"@svehlen1901","x":0.7,"y":0.8}]]',
            1,
            "feed",
        )
    with pytest.raises(ManualPostError, match="außerhalb"):
        parse_manual_user_tag_specs(
            '[[{"username":"svehlen1901","x":1.2,"y":0.3}]]', 1, "feed"
        )
    with pytest.raises(ManualPostError, match="Storys"):
        parse_manual_user_tag_specs(
            '[[{"username":"svehlen1901","x":0.5,"y":0.5}]]', 1, "story"
        )


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
        images=[validate_manual_image("feed.png", "image/png", image_bytes(), "feed")],
    )
    approve(db, post, user)
    job = db.query(PublicationJob).filter_by(post_id=post.id).one()
    job.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    ids = _candidate_ids(db, Settings(meta_scheduler_batch_size=5))
    assert ids == [job.id]


def test_manual_carousel_persists_selected_order_and_shared_caption(db, tmp_path):
    user, _page, team = setup_manual_context(db)
    first = validate_manual_image("zuerst.png", "image/png", image_bytes(), "carousel")
    second_payload = image_bytes()
    second = validate_manual_image("danach.png", "image/png", second_payload, "carousel")
    post, created = create_manual_post(
        db,
        Settings(
            generated_root=tmp_path / "generated",
            media_root=tmp_path / "media",
            upload_root=tmp_path / "uploads",
        ),
        team=team,
        user=user,
        submission_id="manual-carousel-submission-123456789",
        kind="carousel",
        text="Eine gemeinsame Karussell-Bildunterschrift",
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
        images=[first, second],
        user_tags_by_image=[
            [{"username": "erster_spieler", "x": 0.2, "y": 0.3}],
            [{"username": "zweiter.spieler", "x": 0.8, "y": 0.7}],
        ],
    )
    assert created is True
    job = db.query(PublicationJob).filter_by(post_id=post.id).one()
    media = list(
        db.scalars(
            select(PublicationMediaItem)
            .where(PublicationMediaItem.publication_job_id == job.id)
            .order_by(PublicationMediaItem.position)
        )
    )
    assert job.kind == "carousel"
    assert job.text_snapshot == post.text
    assert [item.position for item in media] == [1, 2]
    assert [Path(item.media_path).name for item in media] == [
        "carousel-01-v1.png",
        "carousel-02-v1.png",
    ]
    assert post.design_snapshot["manual_upload"]["images"][0]["original_filename"] == "zuerst.png"
    assert post.design_snapshot["manual_upload"]["images"][0]["user_tags"] == [
        {"username": "erster_spieler", "x": 0.2, "y": 0.3}
    ]
    assert post.design_snapshot["manual_upload"]["images"][1]["user_tags"] == [
        {"username": "zweiter.spieler", "x": 0.8, "y": 0.7}
    ]
    approve(db, post, user)
    assert job.status == JobStatus.SCHEDULED


def test_manual_carousel_requires_two_to_ten_images(db, tmp_path):
    user, _page, team = setup_manual_context(db)
    image = validate_manual_image("single.png", "image/png", image_bytes(), "carousel")
    with pytest.raises(ManualPostError, match="2 bis 10"):
        create_manual_post(
            db,
            Settings(generated_root=tmp_path / "generated"),
            team=team,
            user=user,
            submission_id="manual-carousel-too-small-123456",
            kind="carousel",
            text="Zu klein",
            scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
            images=[image],
        )
