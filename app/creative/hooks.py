from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.creative.feedback import safe_record_feedback
from app.models import (
    GeneratedMediaSlot,
    GeneratedMediaVersion,
    Post,
    PostTextVersion,
    PublicationJob,
    PublicationMediaItem,
)
from app.tenancy.context import TenantContext

# Only known, non-executable design data may flow from generation metadata into
# a learned preference.  In particular, arbitrary prompt fragments and user
# instructions are deliberately ignored.
IMAGE_TRAIT_KEYS = frozenset(
    {
        "graphic_style",
        "visual_impact",
        "background_style",
        "text_alignment",
        "logo_position",
        "player_position",
        "spacing",
        "image_text_amount",
        "player_focus",
        "dynamics",
        "individualization",
        "composition",
        "composition_style",
        "contrast",
        "typography",
        "typography_style",
        "brightness",
        "atmosphere",
        "text_density",
        "information_density",
        "effect_intensity",
        "color_intensity",
        "visual_energy",
        "player_prominence",
    }
)
TEXT_TRAIT_KEYS = frozenset(
    {
        "tone",
        "text_length",
        "emoji_usage",
        "call_to_action",
        "hashtag_style",
    }
)


def _content_type(post: Post) -> str | None:
    value = str(post.post_type or "").strip().casefold()
    aliases = {
        "announcement": "announcement",
        "result": "result",
        "reminder": "reminder",
        "goal": "goal",
        "live": "live",
    }
    return aliases.get(value)


def _simple_value(value: Any) -> str | list[str] | None:
    if isinstance(value, bool):
        return "ja" if value else "nein"
    if isinstance(value, (str, int, float)):
        normalized = str(value).strip()
        return normalized[:100] if normalized else None
    if isinstance(value, list):
        result = [str(item).strip()[:100] for item in value if str(item).strip()]
        return list(dict.fromkeys(result))[:20]
    return None


def _structured_traits(metadata: dict | None, *, modality: str) -> dict:
    allowed = IMAGE_TRAIT_KEYS if modality == "image" else TEXT_TRAIT_KEYS
    sources: list[dict] = []
    payload = metadata if isinstance(metadata, dict) else {}
    sources.append(payload)
    for key in ("creative_traits", "traits", "recipe", "design", "text_traits"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            sources.append(nested)
    result: dict[str, str | list[str]] = {}
    for source in sources:
        for raw_key, raw_value in source.items():
            key = str(raw_key or "").strip().casefold()
            if key not in allowed or key in result:
                continue
            value = _simple_value(raw_value)
            if value is not None:
                result[key] = value
    return result


def _context(post: Post, actor_user_id: str | None) -> TenantContext | None:
    if not actor_user_id or not post.club_id:
        return None
    return TenantContext(club_id=post.club_id, actor_user_id=actor_user_id)


def _media_traits(version: GeneratedMediaVersion) -> dict:
    return _structured_traits(version.design_metadata or {}, modality="image")


def _text_traits(version: PostTextVersion) -> dict:
    return _structured_traits(version.metadata_json or {}, modality="text")


def record_media_selection(
    db: Session,
    *,
    post: Post,
    actor_user_id: str,
    slot: GeneratedMediaSlot,
    selected: GeneratedMediaVersion,
    previous: GeneratedMediaVersion | None,
) -> None:
    content_type = _content_type(post)
    context = _context(post, actor_user_id)
    if content_type is None or context is None:
        return
    action = "reverted" if selected.version_number < (previous.version_number if previous else 0) else "selected"
    safe_record_feedback(
        db,
        context,
        modality="image",
        content_type=content_type,
        action=action,
        source="normal_usage",
        idempotency_key=f"media-selection:{post.id}:{post.version}:{slot.id}:{selected.id}",
        team_id=slot.team_id,
        post_id=post.id,
        generated_media_slot_id=slot.id,
        media_version_id=selected.id,
        generation_job_id=selected.generation_job_id,
        traits=_media_traits(selected),
        metadata={"version_number": selected.version_number, "previous_id": previous.id if previous else None},
    )
    if previous is not None and previous.id != selected.id:
        safe_record_feedback(
            db,
            context,
            modality="image",
            content_type=content_type,
            action="replaced",
            source="normal_usage",
            idempotency_key=f"media-replaced:{post.id}:{post.version}:{slot.id}:{previous.id}:{selected.id}",
            team_id=slot.team_id,
            post_id=post.id,
            generated_media_slot_id=slot.id,
            media_version_id=previous.id,
            generation_job_id=previous.generation_job_id,
            traits=_media_traits(previous),
            metadata={"replacement_id": selected.id},
        )


def record_text_selection(
    db: Session,
    *,
    post: Post,
    actor_user_id: str,
    selected: PostTextVersion,
    previous: PostTextVersion | None,
) -> None:
    content_type = _content_type(post)
    context = _context(post, actor_user_id)
    if content_type is None or context is None:
        return
    action = "reverted" if selected.version_number < (previous.version_number if previous else 0) else "selected"
    safe_record_feedback(
        db,
        context,
        modality="text",
        content_type=content_type,
        action=action,
        source="normal_usage",
        idempotency_key=f"text-selection:{post.id}:{post.version}:{selected.id}",
        team_id=post.team_id,
        post_id=post.id,
        text_version_id=selected.id,
        generation_job_id=selected.generation_job_id,
        traits=_text_traits(selected),
        metadata={"version_number": selected.version_number, "previous_id": previous.id if previous else None},
    )
    if previous is not None and previous.id != selected.id:
        safe_record_feedback(
            db,
            context,
            modality="text",
            content_type=content_type,
            action="replaced",
            source="normal_usage",
            idempotency_key=f"text-replaced:{post.id}:{post.version}:{previous.id}:{selected.id}",
            team_id=post.team_id,
            post_id=post.id,
            text_version_id=previous.id,
            generation_job_id=previous.generation_job_id,
            traits=_text_traits(previous),
            metadata={"replacement_id": selected.id},
        )


def _selected_media(db: Session, post: Post) -> list[tuple[GeneratedMediaSlot, GeneratedMediaVersion]]:
    rows = db.execute(
        select(GeneratedMediaSlot, GeneratedMediaVersion)
        .join(GeneratedMediaVersion, GeneratedMediaVersion.id == GeneratedMediaSlot.selected_version_id)
        .where(
            GeneratedMediaSlot.club_id == post.club_id,
            GeneratedMediaSlot.post_id == post.id,
        )
    )
    return [(slot, version) for slot, version in rows]


def _selected_text(db: Session, post: Post) -> PostTextVersion | None:
    if not post.selected_text_version_id:
        return None
    return db.scalar(
        select(PostTextVersion).where(
            PostTextVersion.id == post.selected_text_version_id,
            PostTextVersion.club_id == post.club_id,
            PostTextVersion.post_id == post.id,
        )
    )


def record_post_decision(
    db: Session,
    *,
    post: Post,
    actor_user_id: str | None,
    action: str,
    reason_codes: list[str] | None = None,
    free_text: str | None = None,
) -> None:
    content_type = _content_type(post)
    context = _context(post, actor_user_id)
    if content_type is None or context is None:
        return
    for slot, version in _selected_media(db, post):
        safe_record_feedback(
            db,
            context,
            modality="image",
            content_type=content_type,
            action=action,
            source="normal_usage",
            idempotency_key=f"post-{action}:image:{post.id}:{post.version}:{slot.id}:{version.id}",
            reason_codes=reason_codes,
            free_text=free_text,
            team_id=slot.team_id,
            post_id=post.id,
            generated_media_slot_id=slot.id,
            media_version_id=version.id,
            generation_job_id=version.generation_job_id,
            traits=_media_traits(version),
        )
    text = _selected_text(db, post)
    if text is not None:
        safe_record_feedback(
            db,
            context,
            modality="text",
            content_type=content_type,
            action=action,
            source="normal_usage",
            idempotency_key=f"post-{action}:text:{post.id}:{post.version}:{text.id}",
            reason_codes=reason_codes,
            free_text=free_text,
            team_id=post.team_id,
            post_id=post.id,
            text_version_id=text.id,
            generation_job_id=text.generation_job_id,
            traits=_text_traits(text),
        )


def record_publication_success(
    db: Session, *, post: Post, job: PublicationJob, actor_user_id: str | None
) -> None:
    """Record only the outputs actually used by the confirmed publication."""

    content_type = _content_type(post)
    context = _context(post, actor_user_id)
    if content_type is None or context is None:
        return
    version_ids = set(
        db.scalars(
            select(PublicationMediaItem.media_version_id).where(
                PublicationMediaItem.publication_job_id == job.id,
                PublicationMediaItem.media_version_id.is_not(None),
            )
        )
    )
    if not version_ids:
        version_ids.update(
            db.scalars(
                select(GeneratedMediaVersion.id)
                .join(GeneratedMediaSlot, GeneratedMediaSlot.id == GeneratedMediaVersion.slot_id)
                .where(
                    GeneratedMediaVersion.club_id == post.club_id,
                    GeneratedMediaVersion.media_path == job.media_path,
                    GeneratedMediaSlot.post_id == post.id,
                )
            )
        )
    for version in db.scalars(
        select(GeneratedMediaVersion).where(
            GeneratedMediaVersion.club_id == post.club_id,
            GeneratedMediaVersion.id.in_(version_ids),
        )
    ):
        slot = db.get(GeneratedMediaSlot, version.slot_id)
        if slot is None or slot.club_id != post.club_id:
            continue
        safe_record_feedback(
            db,
            context,
            modality="image",
            content_type=content_type,
            action="published",
            source="normal_usage",
            idempotency_key=f"publication:image:{job.id}:{version.id}",
            team_id=slot.team_id,
            post_id=post.id,
            generated_media_slot_id=slot.id,
            media_version_id=version.id,
            generation_job_id=version.generation_job_id,
            traits=_media_traits(version),
        )
    text = db.get(PostTextVersion, job.text_version_id) if getattr(job, "text_version_id", None) else _selected_text(db, post)
    if text is not None and text.club_id == post.club_id and job.kind in {"feed", "carousel"}:
        safe_record_feedback(
            db,
            context,
            modality="text",
            content_type=content_type,
            action="published",
            source="normal_usage",
            idempotency_key=f"publication:text:{job.id}:{text.id}",
            team_id=post.team_id,
            post_id=post.id,
            text_version_id=text.id,
            generation_job_id=text.generation_job_id,
            traits=_text_traits(text),
        )


def record_regeneration_request(
    db: Session,
    *,
    post: Post,
    actor_user_id: str,
    request_key: str,
    reason_codes: list[str] | None = None,
    free_text: str | None = None,
) -> None:
    content_type = _content_type(post)
    context = _context(post, actor_user_id)
    if content_type is None or context is None:
        return
    for slot, version in _selected_media(db, post):
        safe_record_feedback(
            db,
            context,
            modality="image",
            content_type=content_type,
            action="regenerated",
            source="normal_usage",
            idempotency_key=f"regenerated:image:{request_key}:{slot.id}:{version.id}",
            reason_codes=reason_codes,
            free_text=free_text,
            team_id=slot.team_id,
            post_id=post.id,
            generated_media_slot_id=slot.id,
            media_version_id=version.id,
            generation_job_id=version.generation_job_id,
            traits=_media_traits(version),
        )
    text = _selected_text(db, post)
    if text is not None:
        safe_record_feedback(
            db,
            context,
            modality="text",
            content_type=content_type,
            action="regenerated",
            source="normal_usage",
            idempotency_key=f"regenerated:text:{request_key}:{text.id}",
            reason_codes=reason_codes,
            free_text=free_text,
            team_id=post.team_id,
            post_id=post.id,
            text_version_id=text.id,
            generation_job_id=text.generation_job_id,
            traits=_text_traits(text),
        )


def record_material_text_edit(
    db: Session,
    *,
    post: Post,
    actor_user_id: str,
    previous: PostTextVersion | None,
    old_text: str,
    new_text: str,
) -> bool:
    """Ignore typo-level edits; record only a meaningfully rewritten caption."""

    old_normalized = " ".join((old_text or "").split())
    new_normalized = " ".join((new_text or "").split())
    if not old_normalized or old_normalized == new_normalized:
        return False
    similarity = SequenceMatcher(None, old_normalized.casefold(), new_normalized.casefold()).ratio()
    length_delta = abs(len(old_normalized) - len(new_normalized)) / max(len(old_normalized), 1)
    if similarity >= 0.92 and length_delta < 0.08:
        return False
    content_type = _content_type(post)
    context = _context(post, actor_user_id)
    if content_type is None or context is None:
        return False
    safe_record_feedback(
        db,
        context,
        modality="text",
        content_type=content_type,
        action="manually_edited",
        source="normal_usage",
        idempotency_key=f"text-edit:{post.id}:{post.version}:{previous.id if previous else 'legacy'}",
        team_id=post.team_id,
        post_id=post.id,
        text_version_id=previous.id if previous else None,
        generation_job_id=previous.generation_job_id if previous else None,
        traits=_text_traits(previous) if previous else {},
        metadata={"similarity": round(similarity, 4), "length_delta": round(length_delta, 4)},
    )
    return True
