from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.creative.flags import creative_feature
from app.creative.types import (
    SENTIMENTS,
    CreativeValidationError,
    default_sentiment,
    normalize_action,
    normalize_content_type,
    normalize_free_text,
    normalize_modality,
    normalize_reason_codes,
    normalize_source,
    normalize_traits,
)
from app.models import (
    CreativeFeedbackEvent,
    GeneratedMediaSlot,
    GeneratedMediaVersion,
    GenerationJob,
    Post,
    PostTextVersion,
    Team,
    uid,
)
from app.tenancy.context import TenantContext, TenantContextError, tenant_get


def _validate_optional_reference(
    db: Session, context: TenantContext, model: type, object_id: str | None, label: str
) -> None:
    if object_id and tenant_get(db, model, object_id, context) is None:
        raise TenantContextError(f"{label} gehört nicht zum aktuellen Verein")


def record_feedback(
    db: Session,
    context: TenantContext,
    *,
    modality: str,
    content_type: str,
    action: str,
    source: str,
    idempotency_key: str,
    sentiment: str | None = None,
    reason_codes: Sequence[str] | None = None,
    free_text: str | None = None,
    traits: Mapping[str, Any] | None = None,
    team_id: str | None = None,
    post_id: str | None = None,
    media_version_id: str | None = None,
    generated_media_slot_id: str | None = None,
    text_version_id: str | None = None,
    generation_job_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    correction_of_id: str | None = None,
    force: bool = False,
) -> CreativeFeedbackEvent | None:
    """Append one idempotent feedback event after strict tenant validation."""

    context.assert_club(context.club_id)
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 255:
        raise CreativeValidationError("Idempotenzschlüssel fehlt oder ist zu lang")
    if not force:
        feature = creative_feature(db, context.club_id)
        if not feature.enabled or not bool(feature.value.get("learning_enabled", True)):
            return None
        sampling_rate = max(
            0.0, min(1.0, float(feature.value.get("feedback_sampling_rate", 1.0)))
        )
        sample = int.from_bytes(
            hashlib.sha256(f"{context.club_id}:{key}".encode()).digest()[:8], "big"
        ) / float(2**64 - 1)
        if sample >= sampling_rate:
            return None
    existing = db.scalar(
        select(CreativeFeedbackEvent).where(
            CreativeFeedbackEvent.club_id == context.club_id,
            CreativeFeedbackEvent.idempotency_key == key,
        )
    )
    if existing is not None:
        return existing
    normalized_modality = normalize_modality(modality)
    normalized_content_type = normalize_content_type(normalized_modality, content_type)
    normalized_action = normalize_action(action)
    normalized_source = normalize_source(source)
    normalized_sentiment = str(sentiment or default_sentiment(normalized_action)).casefold()
    if normalized_sentiment not in SENTIMENTS:
        raise CreativeValidationError("Unbekannte Feedbackwertung")
    _validate_optional_reference(db, context, Team, team_id, "Mannschaft")
    _validate_optional_reference(db, context, Post, post_id, "Beitrag")
    _validate_optional_reference(
        db, context, GeneratedMediaVersion, media_version_id, "Bildversion"
    )
    _validate_optional_reference(
        db, context, GeneratedMediaSlot, generated_media_slot_id, "Bildausgabe"
    )
    _validate_optional_reference(db, context, PostTextVersion, text_version_id, "Textversion")
    _validate_optional_reference(
        db, context, GenerationJob, generation_job_id, "Generierungsauftrag"
    )
    correction = None
    if correction_of_id:
        correction = tenant_get(db, CreativeFeedbackEvent, correction_of_id, context)
        if correction is None:
            raise TenantContextError("Das zu korrigierende Feedback gehört nicht zum Verein")
    event = CreativeFeedbackEvent(
        id=uid(),
        club_id=context.club_id,
        team_id=team_id,
        post_id=post_id,
        generated_media_slot_id=generated_media_slot_id,
        media_version_id=media_version_id,
        text_version_id=text_version_id,
        generation_job_id=generation_job_id,
        user_id=context.actor_user_id,
        modality=normalized_modality,
        content_type=normalized_content_type,
        action=normalized_action,
        source=normalized_source,
        sentiment=normalized_sentiment,
        reason_codes=normalize_reason_codes(reason_codes),
        free_text=normalize_free_text(free_text),
        traits_snapshot=normalize_traits(traits),
        event_metadata=dict(metadata or {}),
        correction_of_id=correction.id if correction else None,
        idempotency_key=key,
    )
    db.add(event)
    db.flush()
    return event


def safe_record_feedback(*args: Any, **kwargs: Any) -> CreativeFeedbackEvent | None:
    """Best-effort hook used by existing workflows.

    Creative learning must never block approval, selection or publishing.  A
    nested transaction keeps a failed feedback write from poisoning the caller's
    transaction.
    """

    db: Session = args[0] if args else kwargs["db"]
    try:
        with db.begin_nested():
            return record_feedback(*args, **kwargs)
    except Exception:
        return None
