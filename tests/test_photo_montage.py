import hashlib
from io import BytesIO

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageOps
from sqlalchemy import select

from app.imagegen.photo_montage import POLICY, PhotoMontageRenderer, background_prompt
from app.imagegen.service import AIImageRenderer, ImageGenerationError
from app.jobs import generation
from app.models import AiPromptDispatch
from app.prompts.service import builtin_prompt
from app.rendering.service import RenderValidationError
from tests.test_generation_jobs import graph


class BackgroundProvider:
    def __init__(self):
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        image = Image.new("RGB", (320, 400), "#123b65")
        ImageDraw.Draw(image).line((0, 400, 320, 0), fill="#456789", width=40)
        buffer = BytesIO()
        image.save(buffer, "PNG")
        return buffer.getvalue()


def setup_renderer(tmp_path, kind="feed", transparent=False):
    media = tmp_path / "media"
    uploads = tmp_path / "uploads"
    media.mkdir()
    uploads.mkdir()
    player = media / "original.png"
    image = Image.new("RGBA" if transparent else "RGB", (900, 600), "orange")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 20, 440, 500), fill="red")
    draw.ellipse((400, 100, 880, 580), fill="green")
    if transparent:
        draw.rectangle((0, 0, 100, 599), fill=(0, 0, 0, 0))
    image.save(player)
    logo = uploads / "logo.png"
    Image.new("RGBA", (100, 100), "white").save(logo)
    data = {
        "post_type": "announcement",
        "home_team": "SV Ehlen",
        "away_team": "SG Obermeiser/Westuffeln II",
        "own_team": "SV Ehlen",
        "kickoff": "2026-09-13T11:00:00+00:00",
        "venue": "Sportplatz Ehlen",
        "pitch": "Rasenplatz",
        "competition": "Kreisliga A",
        "side_label": "Heimspiel",
        "player_image": str(player),
        "team_logo": str(logo),
        "image_creation_mode": "photo_montage",
        "player_media_asset_id": "original-id",
        "_generation_job_id": "montage-job",
    }
    data["image_prompt"] = builtin_prompt("image", "announcement", kind, data)
    provider = BackgroundProvider()
    renderer = AIImageRenderer(tmp_path / "generated", media, uploads, provider)
    return renderer, provider, data, player


@pytest.mark.parametrize("kind", ["feed", "story"])
@pytest.mark.parametrize("transparent", [False, True])
def test_montage_preserves_original_pixels_and_never_sends_references(tmp_path, kind, transparent):
    renderer, provider, data, player = setup_renderer(tmp_path, kind, transparent)
    original_bytes = player.read_bytes()
    output = renderer.render(kind, f"post/{kind}.png", data)
    metadata = renderer.metadata_for(output)
    assert metadata["photo_preservation"]["policy"] == POLICY
    assert (
        metadata["photo_preservation"]["source_checksum"]
        == hashlib.sha256(original_bytes).hexdigest()
    )
    x, y, width, height = metadata["photo_preservation"]["photo_box"]
    with Image.open(player) as photo:
        fitted = ImageOps.contain(
            photo.convert("RGBA"), (952, renderer.sizes[kind][1] - 740), Image.Resampling.LANCZOS
        )
    with Image.open(output) as final:
        assert final.size == renderer.sizes[kind]
        crop = final.convert("RGB").crop((x, y, x + width, y + height))
        # Every fully opaque source pixel must exactly match local resampling.
        difference = ImageChops.difference(crop, fitted.convert("RGB"))
        opaque = fitted.getchannel("A").point(lambda value: 255 if value == 255 else 0)
        assert (
            ImageChops.multiply(difference, Image.merge("RGB", (opaque, opaque, opaque))).getbbox()
            is None
        )
    assert player.read_bytes() == original_bytes
    assert len(provider.calls) == 1
    assert provider.calls[0]["references"] == []
    assert provider.calls[0]["prompt"] == background_prompt(data)
    assert "SV Ehlen" not in provider.calls[0]["prompt"]
    assert data["image_prompt"].rendered not in provider.calls[0]["prompt"]
    assert renderer.render(kind, f"post/{kind}.png", data) == output
    assert len(provider.calls) == 1
    assert renderer.metadata_for(output)["reused_final"] is True


@pytest.mark.parametrize(
    "fault", ["outside", "missing_photo", "sponsor_checksum", "overflow", "target"]
)
def test_montage_rejects_invalid_local_inputs_before_paid_request(tmp_path, fault):
    renderer, provider, data, _ = setup_renderer(tmp_path)
    target = "post/feed.png"
    if fault == "outside":
        data["player_image"] = str(tmp_path / "outside.png")
    elif fault == "missing_photo":
        data["player_image"] = None
    elif fault == "sponsor_checksum":
        data["sponsor_references"] = [{"path": data["team_logo"], "checksum": "wrong"}]
    elif fault == "overflow":
        data["away_team"] = "X" * 301
    else:
        target = "../escape.png"
    with pytest.raises((ImageGenerationError, RenderValidationError)):
        renderer.render("feed", target, data)
    assert provider.calls == []


def test_montage_progress_records_background_prompt_without_photo_references(db, tmp_path):
    renderer, provider, data, _ = setup_renderer(tmp_path)
    _, team, game, user = graph(db)
    job, _ = generation.enqueue_create(db, game, team, user, "announcement")
    progress = generation._ProgressRenderer(renderer, db, job)
    output = progress.render("feed", "post/feed.png", data)
    dispatch = db.scalar(
        select(AiPromptDispatch).where(AiPromptDispatch.generation_job_id == job.id)
    )
    assert dispatch.status == "completed"
    assert dispatch.reference_images == []
    assert dispatch.rendered_prompt == PhotoMontageRenderer.provider_prompt(data)
    assert "SV Ehlen" not in dispatch.rendered_prompt
    assert progress.metadata_for(output)["image_creation_mode"] == "photo_montage"
    assert len(provider.calls) == 1


@pytest.mark.parametrize("kind", ["feed", "story"])
def test_worker_montage_creates_version_and_revokes_approval(db, tmp_path, monkeypatch, kind):
    from datetime import datetime, timezone

    from app.config import Settings
    from app.logos.service import store_logo
    from app.models import (
        GeneratedMediaSlot,
        GeneratedMediaVersion,
        GenerationJobStatus,
        JobStatus,
        MediaAsset,
        PostStatus,
        PublicationJob,
        StoryRule,
    )
    from app.posts.service import create_post
    from app.textgen.service import FixtureTextGenerator

    renderer, provider, data, player = setup_renderer(tmp_path, kind)
    _, team, game, user = graph(db)
    game.venue = "Teststadion"
    game.competition = "Testliga"
    game.pitch = "Rasenplatz"
    settings = Settings(
        generated_root=renderer.root,
        media_root=renderer.media_root,
        upload_root=renderer.upload_root,
        image_generator_mode="openai",
        openai_api_key="test-key",
        text_generator_mode="fixture",
    )
    monkeypatch.setattr("app.posts.service.get_settings", lambda: settings)
    monkeypatch.setattr(generation, "build_renderer", lambda _settings: renderer)
    logo, _ = store_logo(
        db,
        upload_root=renderer.upload_root,
        logo_type="team",
        team_id=team.id,
        display_name="Test",
        original_filename="logo.png",
        content_type="image/png",
        data=(renderer.upload_root / "logo.png").read_bytes(),
        uploaded_by=user.id,
    )
    team.logo_asset_id = logo.id
    asset = MediaAsset(
        team_id=team.id,
        relative_path=player.name,
        filename=player.name,
        mime_type="image/png",
        size=player.stat().st_size,
        checksum=hashlib.sha256(player.read_bytes()).hexdigest(),
        mtime=datetime.now(timezone.utc),
    )
    db.add_all(
        [
            asset,
            StoryRule(
                team_id=team.id,
                name="Teststory",
                post_type="announcement",
                reference="kickoff",
                direction="before",
                offset_minutes=60,
                template="default-story",
            ),
        ]
    )
    db.commit()
    post = create_post(db, game, team, FixtureTextGenerator(), renderer)
    slots = list(
        db.scalars(select(GeneratedMediaSlot).where(GeneratedMediaSlot.post_id == post.id))
    )
    slot = next(slot for slot in slots if slot.media_kind == kind)
    previous = db.get(GeneratedMediaVersion, slot.selected_version_id)
    previous_path = previous.media_path
    from pathlib import Path

    previous_bytes = Path(previous_path).read_bytes()
    publications = list(db.scalars(select(PublicationJob).where(PublicationJob.post_id == post.id)))
    post.status = PostStatus.APPROVED
    post.approved_version = post.version
    for publication in publications:
        publication.status = JobStatus.APPROVED
        publication.approved_post_version = post.version
    db.commit()
    job = generation.enqueue_ai_revision(
        db,
        post,
        user,
        post.version,
        "",
        revise_text=False,
        revise_graphics=True,
        revise_feed=kind == "feed",
        story_job_ids=[],
        feed_positions=[1] if kind == "feed" else None,
        story_variant_numbers=[1] if kind == "story" else None,
        revision_mode="photo_montage",
        target_media_slot_id=slot.id,
    )
    provider.calls.clear()
    assert generation.claim_next(db, "montage-test-worker") == job.id
    result = generation.process_generation_job(db, job.id, settings)
    assert result.status == GenerationJobStatus.SUCCEEDED, result.error_message
    db.refresh(post)
    db.refresh(slot)
    latest = db.get(GeneratedMediaVersion, slot.latest_version_id)
    assert latest.id != previous.id
    assert latest.design_metadata["image_creation_mode"] == "photo_montage"
    assert latest.design_metadata["photo_preservation"]["source_media_asset_id"] == asset.id
    assert Path(previous_path).read_bytes() == previous_bytes
    assert post.status == PostStatus.REAPPROVAL
    assert post.approved_version is None
    assert len(provider.calls) == 1 and provider.calls[0]["references"] == []
    for publication in publications:
        db.refresh(publication)
        assert publication.status == JobStatus.UNAPPROVED
        assert publication.approved_post_version is None
