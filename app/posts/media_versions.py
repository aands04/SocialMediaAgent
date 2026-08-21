"""Immutable media/text versioning for generated posts.

Legacy path columns remain authoritative for old deployments during the staged
migration.  This module mirrors them into explicit version records and then
keeps both representations in sync until the legacy columns can be retired.
"""

from __future__ import annotations

import re
from hashlib import sha256
from pathlib import Path

from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    GeneratedMediaSlot,
    GeneratedMediaVersion,
    JobStatus,
    MetaPublishingAttempt,
    Post,
    PostStatus,
    PostTextVersion,
    PublicationJob,
    PublicationMediaItem,
    StoryRule,
)


class MediaVersionError(ValueError):
    pass


EDITABLE_PUBLICATION_STATUSES = {
    JobStatus.DRAFT,
    JobStatus.UNAPPROVED,
    JobStatus.APPROVED,
    JobStatus.SCHEDULED,
    JobStatus.WAITING,
}


def _prompt_metadata(post: Post, media_kind: str) -> dict:
    prompts = (post.design_snapshot or {}).get("prompts") or {}
    prompt = prompts.get(media_kind) or {}
    if not isinstance(prompt, dict):
        return {}
    # Never copy rendered/system prompt text into tenant-visible records.
    return {
        key: prompt.get(key)
        for key in ("id", "version", "checksum", "name")
        if prompt.get(key) is not None
    }


def _creative_traits(post: Post, modality: str) -> dict:
    """Copy only structured, non-executable traits into immutable versions."""

    snapshot = post.design_snapshot or {}
    branding = snapshot.get("branding") or {}
    image = branding.get("image") or snapshot.get("image_settings") or {}
    text = branding.get("text") or snapshot.get("text_settings") or {}
    source = image if modality == "image" else text
    allowed = (
        {
            "graphic_style",
            "image_effects",
            "background_style",
            "text_alignment",
            "logo_placement",
            "safe_margins",
            "player_position",
            "image_text_amount",
            "player_background_ratio",
            "dynamics",
            "individualization",
        }
        if modality == "image"
        else {"tone", "text_length", "emoji_usage", "cta_type"}
    )
    result = {
        key: value
        for key, value in source.items()
        if key in allowed and isinstance(value, (str, int, float, bool, list))
    }
    prompt_profile = (snapshot.get("prompts") or {}).get("creative_profile") or {}
    recipe = prompt_profile.get("recipe")
    if isinstance(recipe, dict):
        result.update(
            {
                str(key): value
                for key, value in recipe.items()
                if isinstance(value, (str, int, float, bool, list))
            }
        )
    return result


def _path_metadata(path_value: str, *, allow_missing: bool = False) -> dict:
    path = Path(path_value)
    try:
        payload = path.read_bytes()
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            mime_type = Image.MIME.get(image.format, "image/png")
    except (OSError, ValueError) as exc:
        if not allow_missing:
            raise MediaVersionError("Die Mediendatei fehlt oder ist technisch unlesbar") from exc
        payload = b""
        width = height = 0
        mime_type = "image/png"
    return {
        "checksum": sha256(payload or path_value.encode("utf-8")).hexdigest(),
        "mime_type": mime_type,
        "file_size": len(payload),
        "width": width,
        "height": height,
        "validation_status": "valid" if payload else "legacy_unverified",
    }


def _slot(
    db: Session,
    post: Post,
    *,
    slot_key: str,
    media_kind: str,
    output_position: int,
    label: str,
    story_rule_id: str | None = None,
    variant_number: int = 1,
) -> GeneratedMediaSlot:
    item = db.scalar(
        select(GeneratedMediaSlot).where(
            GeneratedMediaSlot.club_id == post.club_id,
            GeneratedMediaSlot.post_id == post.id,
            GeneratedMediaSlot.slot_key == slot_key,
        )
    )
    if item:
        return item
    item = GeneratedMediaSlot(
        club_id=post.club_id,
        post_id=post.id,
        game_id=post.game_id,
        team_id=post.team_id,
        story_rule_id=story_rule_id,
        slot_key=slot_key,
        media_kind=media_kind,
        output_position=output_position,
        variant_number=variant_number,
        label=label,
        selection_mode="auto_latest",
    )
    db.add(item)
    db.flush()
    return item


def register_media_version(
    db: Session,
    post: Post,
    slot: GeneratedMediaSlot,
    media_path: str,
    *,
    generation_job_id: str | None = None,
    created_by: str | None = None,
    legacy_import: bool = False,
    metadata: dict | None = None,
) -> GeneratedMediaVersion:
    if slot.club_id != post.club_id or slot.post_id != post.id:
        raise MediaVersionError("Medienausgabe gehört nicht zu diesem Beitrag und Verein")
    locked = db.scalar(
        select(GeneratedMediaSlot).where(GeneratedMediaSlot.id == slot.id).with_for_update()
    )
    existing = db.scalar(
        select(GeneratedMediaVersion).where(
            GeneratedMediaVersion.slot_id == slot.id,
            GeneratedMediaVersion.media_path == media_path,
        )
    )
    if existing:
        return existing
    number = int(
        db.scalar(
            select(func.max(GeneratedMediaVersion.version_number)).where(
                GeneratedMediaVersion.slot_id == slot.id
            )
        )
        or 0
    ) + 1
    technical = metadata or _path_metadata(media_path, allow_missing=legacy_import)
    prompt = _prompt_metadata(post, "story" if slot.media_kind == "story" else "feed")
    item = GeneratedMediaVersion(
        club_id=post.club_id,
        slot_id=slot.id,
        version_number=number,
        media_path=media_path,
        checksum=technical["checksum"],
        mime_type=technical.get("mime_type", "image/png"),
        file_size=int(technical.get("file_size", 0)),
        width=int(technical.get("width", 0)),
        height=int(technical.get("height", 0)),
        generation_status="completed",
        validation_status=technical.get("validation_status", "valid"),
        generation_job_id=generation_job_id,
        source_media_asset_id=post.media_asset_id,
        prompt_template_id=prompt.get("id"),
        prompt_version=prompt.get("version"),
        prompt_checksum=prompt.get("checksum"),
        logo_references=(post.design_snapshot or {}).get("logos") or {},
        design_metadata={
            "prompt_name": prompt.get("name"),
            "post_version": post.version,
            "media_kind": slot.media_kind,
            "creative_traits": _creative_traits(post, "image"),
        },
        created_by=created_by,
        legacy_import=legacy_import,
    )
    db.add(item)
    db.flush()
    locked.latest_version_id = item.id
    if locked.selection_mode == "auto_latest" or not locked.selected_version_id:
        locked.selected_version_id = item.id
    return item


def ensure_text_version(
    db: Session,
    post: Post,
    *,
    generation_job_id: str | None = None,
    created_by: str | None = None,
    source: str = "generation",
) -> PostTextVersion:
    selected = db.get(PostTextVersion, post.selected_text_version_id) if post.selected_text_version_id else None
    if selected and selected.text == (post.text or ""):
        return selected
    locked = db.scalar(select(Post).where(Post.id == post.id).with_for_update())
    number = int(
        db.scalar(select(func.max(PostTextVersion.version_number)).where(PostTextVersion.post_id == post.id))
        or 0
    ) + 1
    prompt = _prompt_metadata(post, "text")
    item = PostTextVersion(
        club_id=post.club_id,
        post_id=post.id,
        version_number=number,
        text=post.text or "",
        generation_job_id=generation_job_id,
        prompt_template_id=prompt.get("id"),
        prompt_version=prompt.get("version"),
        prompt_checksum=prompt.get("checksum"),
        created_by=created_by,
        source=source,
        validation_status="valid",
        metadata_json={
            "prompt_name": prompt.get("name"),
            "post_version": post.version,
            "creative_traits": _creative_traits(post, "text"),
        },
    )
    db.add(item)
    db.flush()
    locked.latest_text_version_id = item.id
    if locked.text_selection_mode == "auto_latest" or not locked.selected_text_version_id:
        locked.selected_text_version_id = item.id
    else:
        active = db.get(PostTextVersion, locked.selected_text_version_id)
        if active:
            locked.text = active.text
    locked.text_version = max(locked.text_version, number)
    return item


def _story_position(db: Session, job: PublicationJob) -> int:
    post = db.get(Post, job.post_id)
    snapshots = (post.design_snapshot or {}).get("stories", []) if post else []
    if isinstance(snapshots, dict):
        snapshots = list(snapshots.values())
    for entry in snapshots if isinstance(snapshots, list) else []:
        if not isinstance(entry, dict):
            continue
        if job.story_rule_id and str(entry.get("rule_id")) == str(job.story_rule_id):
            return max(1, int(entry.get("media_slot", 1) or 1))
        if entry.get("path") == job.media_path:
            return max(1, int(entry.get("media_slot", 1) or 1))
    match = re.search(r"story-slot-(\d+)-v\d+\.png$", job.media_path or "")
    if match:
        return max(1, int(match.group(1)))
    rule = db.get(StoryRule, job.story_rule_id) if job.story_rule_id else None
    if rule:
        return max(1, int(getattr(rule, "media_slot", 1) or 1))
    # Very old posts can have several story publication jobs without a rule or
    # design snapshot.  They are separate visible outputs, not versions of one
    # output, so preserve their stable publication order as distinct slots.
    legacy_story_ids = list(
        db.scalars(
            select(PublicationJob.id)
            .where(
                PublicationJob.club_id == job.club_id,
                PublicationJob.post_id == job.post_id,
                PublicationJob.kind == "story",
            )
            .order_by(
                PublicationJob.scheduled_at,
                PublicationJob.created_at,
                PublicationJob.id,
            )
        )
    )
    try:
        return legacy_story_ids.index(job.id) + 1
    except ValueError:
        return 1


def synchronize_post_versions(
    db: Session,
    post: Post,
    *,
    generation_job_id: str | None = None,
    created_by: str | None = None,
    legacy_import: bool = False,
) -> list[GeneratedMediaSlot]:
    """Mirror current job paths to immutable records, idempotently."""
    ensure_text_version(
        db,
        post,
        generation_job_id=generation_job_id,
        created_by=created_by,
        source="legacy_import" if legacy_import else "generation",
    )
    jobs = list(
        db.scalars(
            select(PublicationJob)
            .where(PublicationJob.club_id == post.club_id, PublicationJob.post_id == post.id)
            .order_by(PublicationJob.created_at, PublicationJob.id)
        )
    )
    seen: dict[str, GeneratedMediaSlot] = {}
    snapshot_path_slots: dict[str, GeneratedMediaSlot] = {}
    media_snapshot = (post.design_snapshot or {}).get("media") or {}
    snapshot_candidates = [
        ("feed", candidate) for candidate in (media_snapshot.get("feed_variants") or [])
    ]
    snapshot_candidates.extend(
        ("story", candidate)
        for candidate in ((post.design_snapshot or {}).get("story_variants") or [])
    )
    for media_kind, candidate in snapshot_candidates:
        if not isinstance(candidate, dict) or not candidate.get("path"):
            continue
        path_value = str(candidate["path"])
        output_position = max(1, int(candidate.get("output_position", 1) or 1))
        variant_number = max(1, int(candidate.get("variant_number", 1) or 1))
        slot = _slot(
            db,
            post,
            slot_key=f"{media_kind}:{output_position}:variant:{variant_number}",
            media_kind=media_kind,
            output_position=output_position,
            variant_number=variant_number,
            label=str(candidate.get("label") or f"{media_kind.title()}-Variante {variant_number}"),
        )
        register_media_version(
            db,
            post,
            slot,
            path_value,
            generation_job_id=generation_job_id,
            created_by=created_by,
            legacy_import=legacy_import,
        )
        snapshot_path_slots[path_value] = slot
        seen[slot.id] = slot
    for job in jobs:
        published = job.status == JobStatus.PUBLISHED
        if job.kind == "carousel":
            items = list(
                db.scalars(
                    select(PublicationMediaItem)
                    .where(
                        PublicationMediaItem.club_id == post.club_id,
                        PublicationMediaItem.publication_job_id == job.id,
                    )
                    .order_by(PublicationMediaItem.position)
                )
            )
            for media in items:
                slot = snapshot_path_slots.get(media.media_path)
                if slot is None:
                    slot_key = f"feed:{media.position}:variant:1"
                    slot = _slot(
                        db,
                        post,
                        slot_key=slot_key,
                        media_kind="feed",
                        output_position=media.position,
                        label=f"Feed-Bild {media.position}",
                    )
                version = register_media_version(
                    db,
                    post,
                    slot,
                    media.media_path,
                    generation_job_id=generation_job_id,
                    created_by=created_by,
                    legacy_import=legacy_import,
                    metadata={
                        "checksum": media.checksum,
                        "mime_type": media.mime_type,
                        "file_size": media.file_size,
                        "width": media.width,
                        "height": media.height,
                        "validation_status": "valid",
                    },
                )
                # A published publication is an immutable historical record.
                # Registering legacy/version metadata must never retarget it.
                selected = (
                    db.get(GeneratedMediaVersion, media.media_version_id)
                    if published and media.media_version_id
                    else db.get(GeneratedMediaVersion, slot.selected_version_id)
                ) or version
                if not published:
                    media.media_version_id = selected.id
                    media.media_path = selected.media_path
                    media.checksum = selected.checksum
                    media.mime_type = selected.mime_type
                    media.file_size = selected.file_size
                    media.width = selected.width
                    media.height = selected.height
                    if media.position == 1:
                        job.media_version_id = selected.id
                        job.media_path = selected.media_path
                seen[slot.id] = slot
        else:
            position = _story_position(db, job) if job.kind == "story" else 1
            slot = snapshot_path_slots.get(job.media_path)
            if slot is None:
                slot_key = (
                    f"story:{position}:variant:1"
                    if job.kind == "story"
                    else "feed:1:variant:1"
                )
                slot = _slot(
                    db,
                    post,
                    slot_key=slot_key,
                    media_kind="story" if job.kind == "story" else "feed",
                    output_position=position,
                    label=f"Story-Ausgabe {position}" if job.kind == "story" else "Feed-Bild 1",
                    story_rule_id=job.story_rule_id,
                )
            version = register_media_version(
                db,
                post,
                slot,
                job.media_path,
                generation_job_id=generation_job_id,
                created_by=created_by,
                legacy_import=legacy_import,
            )
            selected = (
                db.get(GeneratedMediaVersion, job.media_version_id)
                if published and job.media_version_id
                else db.get(GeneratedMediaVersion, slot.selected_version_id)
            ) or version
            if not published:
                job.media_version_id = selected.id
                job.media_path = selected.media_path
            seen[slot.id] = slot
        if not published and not job.text_version_id:
            job.text_version_id = post.selected_text_version_id
    db.flush()
    return sorted(seen.values(), key=lambda item: (item.media_kind, item.output_position, item.variant_number))


def freeze_publication_versions(db: Session, post: Post, jobs: list[PublicationJob]) -> None:
    """Freeze exact selected versions immediately before approval."""
    slots = synchronize_post_versions(db, post)
    version_to_slot = {
        version.id: version.slot_id
        for version in db.scalars(
            select(GeneratedMediaVersion).where(
                GeneratedMediaVersion.slot_id.in_([slot.id for slot in slots])
            )
        )
    }
    selected_by_slot = {slot.id: slot.selected_version_id for slot in slots}
    for job in jobs:
        if job.status == JobStatus.PUBLISHED:
            continue
        if job.kind == "carousel":
            for media in db.scalars(
                select(PublicationMediaItem).where(
                    PublicationMediaItem.club_id == post.club_id,
                    PublicationMediaItem.publication_job_id == job.id,
                )
            ):
                slot_id = version_to_slot.get(media.media_version_id)
                selected_id = selected_by_slot.get(slot_id)
                selected = db.get(GeneratedMediaVersion, selected_id) if selected_id else None
                if selected:
                    media.media_version_id = selected.id
                    media.media_path = selected.media_path
                    media.checksum = selected.checksum
                    media.mime_type = selected.mime_type
                    media.file_size = selected.file_size
                    media.width = selected.width
                    media.height = selected.height
            first = db.scalar(
                select(PublicationMediaItem)
                .where(PublicationMediaItem.publication_job_id == job.id)
                .order_by(PublicationMediaItem.position)
            )
            if first:
                job.media_version_id = first.media_version_id
                job.media_path = first.media_path
        else:
            slot_id = version_to_slot.get(job.media_version_id)
            selected_id = selected_by_slot.get(slot_id)
            selected = db.get(GeneratedMediaVersion, selected_id) if selected_id else None
            if selected:
                job.media_version_id = selected.id
                job.media_path = selected.media_path
        job.text_version_id = post.selected_text_version_id
        selected_text = db.get(PostTextVersion, post.selected_text_version_id)
        if selected_text:
            job.text_snapshot = selected_text.text


def invalidate_post_approval(post: Post) -> None:
    post.version += 1
    post.approved_version = None
    post.approved_by = None
    post.approved_at = None
    if post.status in {PostStatus.APPROVED, PostStatus.SCHEDULED, PostStatus.PARTIAL}:
        post.status = PostStatus.REAPPROVAL


def _mark_publication_for_reapproval(job: PublicationJob, message: str) -> None:
    job.status = JobStatus.UNAPPROVED
    job.approval_status = "reapproval_required"
    job.approved_post_version = None
    job.error = message


def _copy_version_to_media_item(
    media: PublicationMediaItem,
    version: GeneratedMediaVersion,
) -> None:
    media.media_version_id = version.id
    media.media_path = version.media_path
    media.checksum = version.checksum
    media.mime_type = version.mime_type
    media.file_size = version.file_size
    media.width = version.width
    media.height = version.height


def synchronize_bundle_publication_version(
    db: Session,
    post: Post,
    slot: GeneratedMediaSlot,
    version: GeneratedMediaVersion,
) -> tuple[Post | None, bool]:
    """Propagate a member feed selection to its frozen carousel position.

    A bundled carousel publication belongs to its primary post while each feed
    output and its versions remain attached to the respective member post.
    Therefore the ordinary per-post freeze cannot discover a version selected
    on a non-primary member.  The bundle metadata provides the verified,
    tenant-local position mapping used here.  Published history remains
    immutable and no AI provider is called.
    """

    if slot.media_kind != "feed":
        return None, False
    bundle = (post.design_snapshot or {}).get("club_matchday_carousel") or {}
    primary_id = str(bundle.get("primary_post_id") or "").strip()
    member_ids = [
        str(item).strip()
        for item in (bundle.get("member_post_ids") or [])
        if str(item).strip()
    ]
    member_ids = list(dict.fromkeys(member_ids))
    if not primary_id or primary_id not in member_ids or post.id not in member_ids:
        return None, False
    members = {
        member.id: member
        for member in db.scalars(
            select(Post).where(
                Post.club_id == post.club_id,
                Post.id.in_(member_ids),
            )
        )
    }
    if len(members) != len(member_ids):
        raise MediaVersionError("Der gemeinsame Karussellbeitrag ist unvollständig")
    for member_id in member_ids:
        member_bundle = (members[member_id].design_snapshot or {}).get(
            "club_matchday_carousel"
        ) or {}
        if (
            str(member_bundle.get("primary_post_id") or "") != primary_id
            or list(member_bundle.get("member_post_ids") or []) != member_ids
        ):
            raise MediaVersionError(
                "Die Zuordnung des gemeinsamen Karussells ist widersprüchlich"
            )
    primary = db.scalar(
        select(Post).where(
            Post.id == primary_id,
            Post.club_id == post.club_id,
        ).with_for_update()
    )
    if not primary:
        raise MediaVersionError("Der Hauptbeitrag des gemeinsamen Karussells fehlt")
    position = member_ids.index(post.id) + 1
    changed = False
    jobs = list(
        db.scalars(
            select(PublicationJob)
            .where(
                PublicationJob.club_id == post.club_id,
                PublicationJob.post_id == primary.id,
                PublicationJob.kind == "carousel",
                PublicationJob.status.in_(EDITABLE_PUBLICATION_STATUSES),
            )
            .with_for_update()
        )
    )
    for job in jobs:
        if job.published_at or job.platform_id or job.attempts or job.locked_at:
            raise MediaVersionError(
                "Die Karussell-Veröffentlichung wird bereits durch die Plattform verarbeitet"
            )
        if db.scalar(
            select(MetaPublishingAttempt.id).where(
                MetaPublishingAttempt.club_id == post.club_id,
                MetaPublishingAttempt.publication_job_id == job.id,
                MetaPublishingAttempt.active_key.is_not(None),
            )
        ):
            raise MediaVersionError(
                "Für die Karussell-Veröffentlichung läuft bereits ein Plattformvorgang"
            )
        media = db.scalar(
            select(PublicationMediaItem).where(
                PublicationMediaItem.club_id == post.club_id,
                PublicationMediaItem.publication_job_id == job.id,
                PublicationMediaItem.position == position,
            ).with_for_update()
        )
        if not media:
            raise MediaVersionError(
                "Die zugehörige Position im gemeinsamen Karussell wurde nicht gefunden"
            )
        if (
            media.media_version_id == version.id
            and media.media_path == version.media_path
        ):
            continue
        _copy_version_to_media_item(media, version)
        if media.position == 1:
            job.media_version_id = version.id
            job.media_path = version.media_path
        _mark_publication_for_reapproval(
            job,
            "Medienversion im gemeinsamen Karussell geändert; erneute Freigabe erforderlich",
        )
        job.version += 1
        changed = True
    return primary, changed


def select_media_version(
    db: Session,
    post: Post,
    slot_id: str,
    version_id: str,
) -> GeneratedMediaVersion:
    slot = db.scalar(
        select(GeneratedMediaSlot).where(
            GeneratedMediaSlot.id == slot_id,
            GeneratedMediaSlot.club_id == post.club_id,
            GeneratedMediaSlot.post_id == post.id,
        ).with_for_update()
    )
    version = db.scalar(
        select(GeneratedMediaVersion).where(
            GeneratedMediaVersion.id == version_id,
            GeneratedMediaVersion.club_id == post.club_id,
            GeneratedMediaVersion.slot_id == slot_id,
        )
    )
    if not slot or not version:
        raise MediaVersionError("Medienversion gehört nicht zu diesem Beitrag und Verein")
    if version.generation_status != "completed" or version.validation_status not in {
        "valid",
        "legacy_unverified",
    }:
        raise MediaVersionError("Diese Medienversion ist nicht verwendbar")
    if not Path(version.media_path).is_file():
        raise MediaVersionError("Die Datei dieser Medienversion ist nicht mehr verfügbar")
    selection_changed = not (
        slot.selected_version_id == version.id and slot.selection_mode == "manual"
    )
    primary, bundle_changed = synchronize_bundle_publication_version(
        db,
        post,
        slot,
        version,
    )
    if not selection_changed and not bundle_changed:
        return version
    if selection_changed:
        slot.selection_mode = "manual"
        slot.selected_version_id = version.id
    posts_to_invalidate = {post.id: post} if selection_changed else {}
    if bundle_changed and primary is not None:
        posts_to_invalidate[primary.id] = primary
    for affected_post in posts_to_invalidate.values():
        invalidate_post_approval(affected_post)
    for job in db.scalars(
        select(PublicationJob).where(
            PublicationJob.club_id == post.club_id,
            PublicationJob.post_id == post.id,
            PublicationJob.status.in_(EDITABLE_PUBLICATION_STATUSES),
        )
    ):
        _mark_publication_for_reapproval(
            job,
            "Ausgewählte Medienversion wurde geändert; erneute Freigabe erforderlich",
        )
    open_jobs = list(
        db.scalars(
            select(PublicationJob).where(
                PublicationJob.club_id == post.club_id,
                PublicationJob.post_id == post.id,
                PublicationJob.status.in_(EDITABLE_PUBLICATION_STATUSES),
            )
        )
    )
    freeze_publication_versions(db, post, open_jobs)
    return version


def select_latest_media_automatically(
    db: Session,
    post: Post,
    slot_id: str,
) -> GeneratedMediaVersion:
    """Return a slot to automatic selection without touching published jobs."""
    slot = db.scalar(
        select(GeneratedMediaSlot).where(
            GeneratedMediaSlot.id == slot_id,
            GeneratedMediaSlot.club_id == post.club_id,
            GeneratedMediaSlot.post_id == post.id,
        ).with_for_update()
    )
    if not slot or not slot.latest_version_id:
        raise MediaVersionError("Fuer diese Medienausgabe ist keine neueste Version vorhanden")
    latest = db.scalar(
        select(GeneratedMediaVersion).where(
            GeneratedMediaVersion.id == slot.latest_version_id,
            GeneratedMediaVersion.club_id == post.club_id,
            GeneratedMediaVersion.slot_id == slot.id,
        )
    )
    if not latest or latest.generation_status != "completed" or latest.validation_status not in {
        "valid",
        "legacy_unverified",
    }:
        raise MediaVersionError("Die neueste Medienversion ist nicht verwendbar")
    if not Path(latest.media_path).is_file():
        raise MediaVersionError("Die Datei der neuesten Medienversion ist nicht verfuegbar")
    selection_changed = not (
        slot.selection_mode == "auto_latest" and slot.selected_version_id == latest.id
    )
    primary, bundle_changed = synchronize_bundle_publication_version(
        db,
        post,
        slot,
        latest,
    )
    if not selection_changed and not bundle_changed:
        return latest
    if selection_changed:
        slot.selection_mode = "auto_latest"
        slot.selected_version_id = latest.id
    posts_to_invalidate = {post.id: post} if selection_changed else {}
    if bundle_changed and primary is not None:
        posts_to_invalidate[primary.id] = primary
    for affected_post in posts_to_invalidate.values():
        invalidate_post_approval(affected_post)
    open_jobs = list(
        db.scalars(
            select(PublicationJob).where(
                PublicationJob.club_id == post.club_id,
                PublicationJob.post_id == post.id,
                PublicationJob.status.in_(EDITABLE_PUBLICATION_STATUSES),
            )
        )
    )
    for job in open_jobs:
        _mark_publication_for_reapproval(
            job,
            "Automatische Medienauswahl aktiviert; erneute Freigabe erforderlich",
        )
    freeze_publication_versions(db, post, open_jobs)
    return latest


def select_publication_media_variant(
    db: Session,
    post: Post,
    *,
    publication_job_id: str,
    slot_id: str,
    publication_media_item_id: str | None = None,
    allowed_post_ids: set[str] | None = None,
    allow_feed_candidates_for_same_post: bool = False,
) -> GeneratedMediaVersion:
    """Assign an existing candidate variant to one open publication slot.

    This is deliberately separate from ``select_media_version``: selecting a
    variant changes the publication mapping, while selecting a version changes
    the immutable version used inside that variant.  Neither operation invokes
    an AI provider.
    """
    job = db.scalar(
        select(PublicationJob).where(
            PublicationJob.id == publication_job_id,
            PublicationJob.club_id == post.club_id,
        ).with_for_update()
    )
    slot = db.scalar(
        select(GeneratedMediaSlot).where(
            GeneratedMediaSlot.id == slot_id,
            GeneratedMediaSlot.club_id == post.club_id,
        )
    )
    permitted_posts = allowed_post_ids or {post.id}
    if not job or job.post_id != post.id:
        raise MediaVersionError("Veröffentlichungsauftrag gehört nicht zu diesem Beitrag")
    if not slot or slot.post_id not in permitted_posts:
        raise MediaVersionError("Medienvariante gehört nicht zu diesem Beitrag und Verein")
    if job.status not in EDITABLE_PUBLICATION_STATUSES:
        raise MediaVersionError(
            "Diese Veröffentlichung kann in ihrem aktuellen Status nicht geändert werden"
        )
    if job.published_at or job.platform_id or job.attempts or job.locked_at:
        raise MediaVersionError("Die Plattformverarbeitung hat bereits begonnen")
    if db.scalar(
        select(MetaPublishingAttempt.id).where(
            MetaPublishingAttempt.club_id == post.club_id,
            MetaPublishingAttempt.publication_job_id == job.id,
            MetaPublishingAttempt.active_key.is_not(None),
        )
    ):
        raise MediaVersionError("Für diese Veröffentlichung läuft bereits ein Plattformvorgang")
    version = db.scalar(
        select(GeneratedMediaVersion).where(
            GeneratedMediaVersion.id == slot.selected_version_id,
            GeneratedMediaVersion.club_id == post.club_id,
            GeneratedMediaVersion.slot_id == slot.id,
        )
    )
    if not version or version.generation_status != "completed" or version.validation_status not in {
        "valid",
        "legacy_unverified",
    }:
        raise MediaVersionError("Die ausgewählte Variante besitzt keine verwendbare Version")
    if not Path(version.media_path).is_file():
        raise MediaVersionError("Die Datei der ausgewählten Variante ist nicht verfügbar")

    if job.kind == "carousel":
        if not publication_media_item_id or slot.media_kind != "feed":
            raise MediaVersionError("Für ein Karussell muss eine konkrete Position gewählt werden")
        media = db.scalar(
            select(PublicationMediaItem).where(
                PublicationMediaItem.id == publication_media_item_id,
                PublicationMediaItem.club_id == post.club_id,
                PublicationMediaItem.publication_job_id == job.id,
            ).with_for_update()
        )
        if not media:
            raise MediaVersionError("Karussellposition wurde nicht gefunden")
        current_slot_id = (
            db.scalar(
                select(GeneratedMediaVersion.slot_id).where(
                    GeneratedMediaVersion.id == media.media_version_id
                )
            )
            if media.media_version_id
            else None
        )
        current_slot = db.get(GeneratedMediaSlot, current_slot_id) if current_slot_id else None
        if current_slot and (
            slot.post_id != current_slot.post_id
            or (
                slot.output_position != current_slot.output_position
                and not allow_feed_candidates_for_same_post
            )
        ):
            raise MediaVersionError("Variante gehört nicht zu dieser Karussellposition")
        media.media_version_id = version.id
        media.media_path = version.media_path
        media.checksum = version.checksum
        media.mime_type = version.mime_type
        media.file_size = version.file_size
        media.width = version.width
        media.height = version.height
        if media.position == 1:
            job.media_version_id = version.id
            job.media_path = version.media_path
    else:
        expected_kind = "story" if job.kind == "story" else "feed"
        if slot.media_kind != expected_kind:
            raise MediaVersionError("Variante passt nicht zum Veröffentlichungsformat")
        current_slot_id = db.scalar(
            select(GeneratedMediaVersion.slot_id).where(
                GeneratedMediaVersion.id == job.media_version_id
            )
        )
        current_slot = db.get(GeneratedMediaSlot, current_slot_id) if current_slot_id else None
        if current_slot and (
            slot.post_id != current_slot.post_id
            or slot.output_position != current_slot.output_position
        ):
            raise MediaVersionError("Variante gehört nicht zu diesem Veröffentlichungsslot")
        job.media_version_id = version.id
        job.media_path = version.media_path

    post.version += 1
    post.approved_version = None
    post.approved_by = None
    post.approved_at = None
    if post.status in {PostStatus.APPROVED, PostStatus.SCHEDULED, PostStatus.PARTIAL}:
        post.status = PostStatus.REAPPROVAL
    for open_job in db.scalars(
        select(PublicationJob).where(
            PublicationJob.club_id == post.club_id,
            PublicationJob.post_id == post.id,
            PublicationJob.status.in_(EDITABLE_PUBLICATION_STATUSES),
        )
    ):
        open_job.status = JobStatus.UNAPPROVED
        open_job.approval_status = "reapproval_required"
        open_job.approved_post_version = None
        open_job.error = "Medienvariante geändert; erneute Freigabe erforderlich"
    return version


def select_text_version(db: Session, post: Post, version_id: str) -> PostTextVersion:
    version = db.scalar(
        select(PostTextVersion).where(
            PostTextVersion.id == version_id,
            PostTextVersion.club_id == post.club_id,
            PostTextVersion.post_id == post.id,
        )
    )
    if not version:
        raise MediaVersionError("Textversion gehört nicht zu diesem Beitrag und Verein")
    post.text_selection_mode = "manual"
    post.selected_text_version_id = version.id
    post.text = version.text
    post.text_version = version.version_number
    post.version += 1
    post.approved_version = None
    post.approved_by = None
    post.approved_at = None
    if post.status in {PostStatus.APPROVED, PostStatus.SCHEDULED, PostStatus.PARTIAL}:
        post.status = PostStatus.REAPPROVAL
    for job in db.scalars(
        select(PublicationJob).where(
            PublicationJob.club_id == post.club_id,
            PublicationJob.post_id == post.id,
        )
    ):
        if job.status != JobStatus.PUBLISHED:
            job.status = JobStatus.UNAPPROVED
            job.approval_status = "reapproval_required"
            job.approved_post_version = None
            job.text_version_id = version.id
            job.text_snapshot = version.text
            job.error = "Ausgewählte Textversion wurde geändert; erneute Freigabe erforderlich"
    return version


def select_latest_text_automatically(db: Session, post: Post) -> PostTextVersion:
    latest = db.scalar(
        select(PostTextVersion).where(
            PostTextVersion.id == post.latest_text_version_id,
            PostTextVersion.club_id == post.club_id,
            PostTextVersion.post_id == post.id,
        )
    )
    if not latest:
        raise MediaVersionError("Fuer diesen Beitrag ist keine neueste Textversion vorhanden")
    if post.text_selection_mode == "auto_latest" and post.selected_text_version_id == latest.id:
        return latest
    post.text_selection_mode = "auto_latest"
    post.selected_text_version_id = latest.id
    post.text = latest.text
    post.text_version = latest.version_number
    post.version += 1
    post.approved_version = None
    post.approved_by = None
    post.approved_at = None
    if post.status in {PostStatus.APPROVED, PostStatus.SCHEDULED, PostStatus.PARTIAL}:
        post.status = PostStatus.REAPPROVAL
    for job in db.scalars(
        select(PublicationJob).where(
            PublicationJob.club_id == post.club_id,
            PublicationJob.post_id == post.id,
            PublicationJob.status != JobStatus.PUBLISHED,
        )
    ):
        job.status = JobStatus.UNAPPROVED
        job.approval_status = "reapproval_required"
        job.approved_post_version = None
        job.text_version_id = latest.id
        job.text_snapshot = latest.text
        job.error = "Automatische Textauswahl aktiviert; erneute Freigabe erforderlich"
    return latest


def post_media_catalog(db: Session, post: Post) -> list[dict]:
    slots = synchronize_post_versions(db, post, legacy_import=True)
    result = []
    for slot in slots:
        versions = list(
            db.scalars(
                select(GeneratedMediaVersion)
                .where(
                    GeneratedMediaVersion.club_id == post.club_id,
                    GeneratedMediaVersion.slot_id == slot.id,
                )
                .order_by(GeneratedMediaVersion.version_number.desc())
            )
        )
        result.append({"slot": slot, "versions": versions})
    return result
