from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select

from app.approvals.service import ApprovalError, approve
from app.config import Settings
from app.jobs import generation
from app.logos.service import (
    LogoCompositor,
    LogoValidationError,
    frozen_logo_set,
    normalize_club_name,
    store_logo,
)
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
from app.posts.service import recompose_post_logos


def image_bytes(fmt="PNG", color=(210, 20, 30, 255), size=(180, 160)):
    buffer = BytesIO()
    image = Image.new("RGBA", size, color)
    ImageDraw.Draw(image).ellipse((25, 20, 145, 140), fill=(20, 80, 190, 255))
    image.save(buffer, fmt)
    return buffer.getvalue()


def graph(db):
    user = User(
        email="logos@example.invalid",
        password_hash="x",
        role=Role.ADMIN,
        all_teams=True,
    )
    page = InstagramPage(
        internal_name="page",
        display_name="Page",
        username="page",
        club="SV Ehlen",
        active=True,
        connection_status="connected",
    )
    db.add_all([user, page])
    db.flush()
    team = Team(
        internal_name="erste",
        display_name="SV Ehlen",
        short_name="SVE",
        slug="sve-logos",
        club="SV Ehlen",
        fussball_url="https://www.fussball.de/team",
        instagram_page_id=page.id,
        media_subdir="erste",
    )
    db.add(team)
    db.flush()
    game = Game(
        team_id=team.id,
        external_id="logo-game",
        home_team="SV Ehlen",
        away_team="TSV Immenhausen II",
        kickoff=datetime.now(timezone.utc) + timedelta(days=7),
        competition="Kreisliga A",
        venue="Ehlen",
        pitch="Rasenplatz",
        source_url="fixture://logo",
    )
    db.add(game)
    db.commit()
    return user, page, team, game


def upload(db, root, user, team, kind, name, color):
    logo, created = store_logo(
        db,
        upload_root=root,
        logo_type=kind,
        team_id=team.id if kind == "team" else None,
        display_name=name,
        original_filename=f"{kind}.png",
        content_type="image/png",
        data=image_bytes(color=color),
        uploaded_by=user.id,
    )
    db.commit()
    assert created
    return logo


def test_secure_versioned_logo_storage_and_normalization(db, tmp_path):
    user, _, team, _ = graph(db)
    root = tmp_path / "uploads"
    logo = upload(db, root, user, team, "team", "SV Ehlen", (255, 0, 0, 255))
    assert (root / logo.original_path).read_bytes() == image_bytes(color=(255, 0, 0, 255))
    assert (root / logo.render_path).is_file()
    assert logo.version == 1
    duplicate, created = store_logo(
        db,
        upload_root=root,
        logo_type="team",
        team_id=team.id,
        display_name="SV Ehlen",
        original_filename="copy.png",
        content_type="image/png",
        data=image_bytes(color=(255, 0, 0, 255)),
        uploaded_by=user.id,
    )
    assert not created and duplicate.id == logo.id
    assert normalize_club_name("SG Weser/\u200bDiemel") == "sg weser diemel"
    assert normalize_club_name("TSV Immenhausen II") != normalize_club_name(
        "TSV Immenhausen"
    )
    with pytest.raises(LogoValidationError, match="MIME"):
        store_logo(
            db,
            upload_root=root,
            logo_type="opponent",
            team_id=None,
            display_name="FC Falsch",
            original_filename="falsch.png",
            content_type="image/webp",
            data=image_bytes(),
            uploaded_by=user.id,
        )
    with pytest.raises(LogoValidationError, match="technisch lesbares"):
        store_logo(
            db,
            upload_root=root,
            logo_type="opponent",
            team_id=None,
            display_name="FC Falsch",
            original_filename="falsch.png",
            content_type="image/png",
            data=b"not-an-image",
            uploaded_by=user.id,
        )
    with pytest.raises(LogoValidationError, match="5 MiB"):
        store_logo(
            db,
            upload_root=root,
            logo_type="opponent",
            team_id=None,
            display_name="FC Zu Groß",
            original_filename="gross.png",
            content_type="image/png",
            data=b"x" * (5 * 1024 * 1024 + 1),
            uploaded_by=user.id,
        )
    traversal, _ = store_logo(
        db,
        upload_root=root,
        logo_type="opponent",
        team_id=None,
        display_name="FC Sicher",
        original_filename="../../escape.png",
        content_type="image/png",
        data=image_bytes(color=(30, 40, 50, 255)),
        uploaded_by=user.id,
    )
    assert ".." not in traversal.original_path
    assert traversal.original_filename == "escape.png"


def test_deterministic_compositor_uses_originals_and_text_fallback(db, tmp_path):
    user, _, team, game = graph(db)
    root = tmp_path / "uploads"
    team_logo = upload(db, root, user, team, "team", team.club, (255, 0, 0, 255))
    team.logo_asset_id = team_logo.id
    db.commit()
    base = tmp_path / "base.png"
    Image.new("RGB", (1080, 1350), (12, 30, 65)).save(base)
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    logos = frozen_logo_set(db, game, team)
    compositor = LogoCompositor(root)
    report = compositor.compose(
        base_path=base, output_path=first, kind="feed", logos=logos
    )
    compositor.compose(base_path=base, output_path=second, kind="feed", logos=logos)
    assert first.read_bytes() == second.read_bytes()
    assert report["version"] == "verified-logo-compositor-v1"
    assert logos["opponent"]["fallback"] is True
    assert first.read_bytes() != base.read_bytes()


def test_logo_only_recomposition_reuses_ai_base_and_preserves_published_story(
    db, tmp_path, monkeypatch
):
    user, page, team, game = graph(db)
    upload_root = tmp_path / "uploads"
    generated = tmp_path / "generated"
    generated.mkdir()
    monkeypatch.setattr("app.posts.service.get_settings", lambda: Settings(
        generated_root=generated,
        media_root=tmp_path / "media",
        upload_root=upload_root,
    ))
    team_logo = upload(db, upload_root, user, team, "team", team.club, (255, 0, 0, 255))
    opponent_logo = upload(
        db, upload_root, user, team, "opponent", game.away_team, (0, 255, 0, 255)
    )
    team.logo_asset_id = team_logo.id
    game.opponent_logo_id = opponent_logo.id
    post = Post(
        game_id=game.id,
        team_id=team.id,
        instagram_page_id=page.id,
        post_type="announcement",
        status=PostStatus.APPROVED,
        text="Text",
        feed_path=str(generated / "old-feed.png"),
        design_snapshot={},
    )
    db.add(post)
    db.flush()
    feed_base = generated / post.id / "feed-base.png"
    story_base = generated / post.id / "story-base.png"
    published_base = generated / post.id / "published-base.png"
    feed_base.parent.mkdir(parents=True)
    Image.new("RGB", (1080, 1350), (20, 40, 80)).save(feed_base)
    Image.new("RGB", (1080, 1920), (30, 50, 90)).save(story_base)
    Image.new("RGB", (1080, 1920), (40, 60, 100)).save(published_base)
    feed = PublicationJob(
        post_id=post.id,
        game_id=game.id,
        team_id=team.id,
        instagram_page_id=page.id,
        kind="feed",
        media_path=post.feed_path,
        scheduled_at=game.kickoff,
        idempotency_key=f"{post.id}:feed:v1",
    )
    story = PublicationJob(
        post_id=post.id,
        game_id=game.id,
        team_id=team.id,
        instagram_page_id=page.id,
        story_rule_id="open-story",
        kind="story",
        media_path=str(generated / "old-story.png"),
        scheduled_at=game.kickoff,
        idempotency_key=f"{post.id}:story:open-story:v1",
    )
    published = PublicationJob(
        post_id=post.id,
        game_id=game.id,
        team_id=team.id,
        instagram_page_id=page.id,
        story_rule_id="published-story",
        kind="story",
        media_path=str(generated / "published.png"),
        scheduled_at=game.kickoff,
        idempotency_key=f"{post.id}:story:published-story:v1",
        status=JobStatus.PUBLISHED,
        platform_id="platform-1",
    )
    db.add_all([feed, story, published])
    db.flush()
    post.design_snapshot = {
        "logos": {},
        "media": {"feed": {"ai_base_path": str(feed_base)}},
        "stories": [
            {
                "rule_id": "open-story",
                "media_version": 1,
                "rendering": {"ai_base_path": str(story_base)},
            },
            {
                "rule_id": "published-story",
                "media_version": 1,
                "rendering": {"ai_base_path": str(published_base)},
            },
        ],
    }
    db.commit()
    old_published = (published.media_path, published.idempotency_key, published.platform_id)
    result = recompose_post_logos(
        db, post, [story.id], frozen_logo_set(db, game, team)
    )
    db.commit()
    assert result.feed_version == 2 and result.status == PostStatus.REAPPROVAL
    assert "ai_base_path" in result.design_snapshot["media"]["feed"]
    assert result.design_snapshot["logos"]["team"]["id"] == team_logo.id
    assert (published.media_path, published.idempotency_key, published.platform_id) == old_published
    assert story.media_path.endswith("-v2.png")

    renderer_calls = {"count": 0}

    def forbidden_renderer(_settings):
        renderer_calls["count"] += 1
        raise AssertionError("Eine reine Logo-Komposition darf keinen KI-Renderer starten")

    monkeypatch.setattr(generation, "build_renderer", forbidden_renderer)
    queued = generation.enqueue_logo_recompose(
        db, result, user, result.version, [story.id]
    )
    claimed = generation.claim_next(db, "logo-recompose-worker")
    assert claimed == queued.id
    completed = generation.process_generation_job(
        db,
        claimed,
        Settings(
            generated_root=generated,
            media_root=tmp_path / "media",
            upload_root=upload_root,
            image_generator_mode="openai",
            openai_api_key="not-used",
        ),
    )
    assert completed.status == GenerationJobStatus.SUCCEEDED
    assert renderer_calls["count"] == 0
    assert db.scalar(
        select(AuditLog).where(AuditLog.action == "graphics.logos_recomposed")
    )


def test_openai_job_without_team_logo_stops_before_generator(db, tmp_path, monkeypatch):
    user, _, team, game = graph(db)
    job, _ = generation.enqueue_create(db, game, team, user, "announcement")
    claimed = generation.claim_next(db, "logo-worker")
    calls = {"renderer": 0}

    def forbidden(_settings):
        calls["renderer"] += 1
        raise AssertionError("renderer must not be built")

    monkeypatch.setattr(generation, "build_renderer", forbidden)
    result = generation.process_generation_job(
        db,
        claimed,
        Settings(
            image_generator_mode="openai",
            text_generator_mode="mock",
            openai_api_key="test",
            generated_root=tmp_path / "generated",
            upload_root=tmp_path / "uploads",
        ),
    )
    assert result.status == GenerationJobStatus.MANUAL_REVIEW_REQUIRED
    assert result.error_category == "verified_logo_unavailable"
    assert calls["renderer"] == 0


def test_legacy_post_without_frozen_team_logo_cannot_be_approved(db, tmp_path):
    user, page, team, game = graph(db)
    media = tmp_path / "legacy.png"
    Image.new("RGB", (1080, 1350), (12, 30, 65)).save(media)
    post = Post(
        game_id=game.id,
        team_id=team.id,
        instagram_page_id=page.id,
        post_type="announcement",
        status=PostStatus.PENDING,
        text="Legacy",
        feed_path=str(media),
        design_snapshot={},
        critical_warnings=[],
    )
    db.add(post)
    db.flush()
    db.add(
        PublicationJob(
            post_id=post.id,
            game_id=game.id,
            team_id=team.id,
            instagram_page_id=page.id,
            kind="feed",
            media_path=str(media),
            scheduled_at=game.kickoff,
            idempotency_key=f"{post.id}:feed:v1",
        )
    )
    db.commit()
    with pytest.raises(ApprovalError, match="Mannschaftslogo"):
        approve(db, post, user)
