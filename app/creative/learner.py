from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.creative.flags import creative_feature, learning_enabled
from app.creative.types import normalize_content_type, normalize_modality
from app.creative.usage import record_internal_usage
from app.models import (
    CreativeExampleReference,
    CreativeFeedbackEvent,
    CreativePreferenceProfile,
    uid,
)
from app.tenancy.context import TenantContext

LEARNER_VERSION = "deterministic-v1"

SENTIMENT_SIGNS = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}


def _recency_weight(
    occurred_at: datetime,
    current: datetime,
    *,
    half_life_days: float,
    daily_weight: float | None = None,
) -> float:
    value = occurred_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (current - value.astimezone(timezone.utc)).total_seconds() / 86400)
    # Half-life of roughly six months, with a floor so historic explicit
    # onboarding preferences never disappear completely.
    if daily_weight is not None:
        return max(0.35, math.pow(max(0.5, min(1.0, daily_weight)), age_days))
    return max(0.35, math.pow(0.5, age_days / max(half_life_days, 1.0)))


def _trait_items(traits: dict) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for key, raw_value in (traits or {}).items():
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        for value in values:
            items.append((str(key), str(value)))
    return items


def active_profile(
    db: Session, club_id: str, modality: str, content_type: str
) -> CreativePreferenceProfile | None:
    return db.scalar(
        select(CreativePreferenceProfile)
        .where(
            CreativePreferenceProfile.club_id == club_id,
            CreativePreferenceProfile.modality == modality,
            CreativePreferenceProfile.content_type == content_type,
            CreativePreferenceProfile.status == "active",
        )
        .order_by(CreativePreferenceProfile.profile_version.desc())
    )


def _refresh_examples(
    db: Session,
    *,
    club_id: str,
    profile: CreativePreferenceProfile,
    events: list[CreativeFeedbackEvent],
    action_weights: dict,
    source_weights: dict,
    half_life_days: float,
    daily_weight: float | None,
    current: datetime,
    positive_limit: int,
    negative_limit: int,
) -> None:
    """Replace the active reference set with the strongest traceable outputs.

    References always point to an immutable media/text version from the same
    tenant.  Events without such a version still influence the aggregated
    profile, but can never become a visual/text reference.
    """

    for existing in db.scalars(
        select(CreativeExampleReference).where(
            CreativeExampleReference.club_id == club_id,
            CreativeExampleReference.modality == profile.modality,
            CreativeExampleReference.content_type == profile.content_type,
            CreativeExampleReference.active.is_(True),
        )
    ):
        existing.active = False

    ranked: dict[tuple[str, str], tuple[float, CreativeFeedbackEvent]] = {}
    for event in events:
        version_id = (
            event.media_version_id if profile.modality == "image" else event.text_version_id
        )
        if not version_id:
            continue
        sign = SENTIMENT_SIGNS.get(event.sentiment or "neutral", 0.0)
        if sign == 0:
            continue
        strength = abs(
            float(action_weights.get(event.action, 0.5))
            * float(source_weights.get(event.source, 1.0))
            * _recency_weight(
                event.occurred_at,
                current,
                half_life_days=half_life_days,
                daily_weight=daily_weight,
            )
        )
        sentiment = "positive" if sign > 0 else "negative"
        key = (sentiment, version_id)
        previous = ranked.get(key)
        if previous is None or strength > previous[0]:
            ranked[key] = (strength, event)

    for sentiment, limit in (
        ("positive", max(0, min(int(positive_limit), 5))),
        ("negative", max(0, min(int(negative_limit), 3))),
    ):
        selected = sorted(
            (value for (kind, _), value in ranked.items() if kind == sentiment),
            key=lambda item: (-item[0], item[1].occurred_at),
        )[:limit]
        for rank, (score, event) in enumerate(selected, 1):
            db.add(
                CreativeExampleReference(
                    id=uid(),
                    club_id=club_id,
                    profile_id=profile.id,
                    modality=profile.modality,
                    content_type=profile.content_type,
                    sentiment=sentiment,
                    media_version_id=(
                        event.media_version_id if profile.modality == "image" else None
                    ),
                    text_version_id=(
                        event.text_version_id if profile.modality == "text" else None
                    ),
                    rank=rank,
                    traits=event.traits_snapshot or {},
                    score=score,
                    active=True,
                    created_by=event.user_id,
                )
            )


def rebuild_profile(
    db: Session,
    context: TenantContext,
    *,
    modality: str,
    content_type: str,
    reason: str = "threshold",
    force: bool = False,
    now: datetime | None = None,
) -> CreativePreferenceProfile | None:
    normalized_modality = normalize_modality(modality)
    normalized_content = normalize_content_type(normalized_modality, content_type)
    if not force and not learning_enabled(db, context.club_id):
        return None
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    events = list(
        db.scalars(
            select(CreativeFeedbackEvent)
            .where(
                CreativeFeedbackEvent.club_id == context.club_id,
                CreativeFeedbackEvent.modality == normalized_modality,
                CreativeFeedbackEvent.content_type == normalized_content,
            )
            .order_by(CreativeFeedbackEvent.occurred_at)
        )
    )
    corrected_ids = {item.correction_of_id for item in events if item.correction_of_id}
    usable = [item for item in events if item.id not in corrected_ids and item.traits_snapshot]
    feature = creative_feature(db, context.club_id)
    minimum_samples = max(1, int(feature.value.get("minimum_samples", 5)))
    action_weights = feature.value.get("action_weights") or {}
    source_weights = feature.value.get("source_weights") or {}
    half_life_days = float(feature.value.get("recency_half_life_days", 180))
    daily_weight = float(feature.value.get("preference_recency_weight", 0.985))
    if len(usable) < minimum_samples and not force:
        return None

    previous = active_profile(db, context.club_id, normalized_modality, normalized_content)
    if previous is not None and not force:
        previous_feedback_at = previous.last_feedback_at
        if previous_feedback_at is not None:
            if previous_feedback_at.tzinfo is None:
                previous_feedback_at = previous_feedback_at.replace(tzinfo=timezone.utc)
            new_events = [
                item
                for item in usable
                if (
                    item.occurred_at.replace(tzinfo=timezone.utc)
                    if item.occurred_at.tzinfo is None
                    else item.occurred_at.astimezone(timezone.utc)
                )
                > previous_feedback_at.astimezone(timezone.utc)
            ]
            # Regelmaessige Laeufe verdichten nur echte neue Signale. So
            # entstehen weder leere Profilversionen noch ein Versionsanstieg
            # durch Worker-Neustarts.
            if len(new_events) < minimum_samples:
                return None

    scores: dict[tuple[str, str], float] = defaultdict(float)
    observations: Counter[tuple[str, str]] = Counter()
    sources: Counter[str] = Counter()
    for event in usable:
        action_weight = float(action_weights.get(event.action, 0.5))
        source_weight = float(source_weights.get(event.source, 1.0))
        sign = SENTIMENT_SIGNS.get(event.sentiment or "neutral", 0.0)
        weight = action_weight * source_weight * sign * _recency_weight(
            event.occurred_at,
            current,
            half_life_days=half_life_days,
            daily_weight=daily_weight,
        )
        sources[event.source] += 1
        for item in _trait_items(event.traits_snapshot):
            scores[item] += weight
            observations[item] += 1

    ranked = sorted(scores.items(), key=lambda item: (-abs(item[1]), item[0]))
    positive = [
        {"key": key, "value": value, "score": round(score, 4), "samples": observations[(key, value)]}
        for (key, value), score in ranked
        if score >= 1.0
    ][:20]
    negative = [
        {"key": key, "value": value, "score": round(abs(score), 4), "samples": observations[(key, value)]}
        for (key, value), score in ranked
        if score <= -1.0
    ][:20]
    if not positive and not negative and not force:
        return None
    total_signal = sum(abs(value) for value in scores.values())
    dominant_signal = sum(abs(item["score"]) for item in positive + negative)
    sample_confidence = min(1.0, len(usable) / max(minimum_samples * 2, 1))
    dominance = min(1.0, dominant_signal / max(total_signal, 1.0))
    confidence = round(min(1.0, 0.2 + 0.5 * sample_confidence + 0.3 * dominance), 4)

    next_version = int(
        db.scalar(
            select(func.coalesce(func.max(CreativePreferenceProfile.profile_version), 0)).where(
                CreativePreferenceProfile.club_id == context.club_id,
                CreativePreferenceProfile.modality == normalized_modality,
                CreativePreferenceProfile.content_type == normalized_content,
            )
        )
        or 0
    ) + 1
    if previous is not None:
        previous.status = "superseded"
        previous.superseded_at = current
    profile = CreativePreferenceProfile(
        id=uid(),
        club_id=context.club_id,
        modality=normalized_modality,
        content_type=normalized_content,
        profile_version=next_version,
        status="active",
        preferences={"traits": positive},
        avoidances={"traits": negative},
        confidence=confidence,
        sample_count=len(usable),
        source_summary=dict(sources),
        learner_version=LEARNER_VERSION,
        generated_by="deterministic_preference_learner",
        build_reason=str(reason)[:40],
        last_feedback_at=max((item.occurred_at for item in usable), default=None),
        activated_at=current,
    )
    db.add(profile)
    db.flush()
    _refresh_examples(
        db,
        club_id=context.club_id,
        profile=profile,
        events=usable,
        action_weights=action_weights,
        source_weights=source_weights,
        half_life_days=half_life_days,
        daily_weight=daily_weight,
        current=current,
        positive_limit=int(feature.value.get("positive_example_limit", 5)),
        negative_limit=int(feature.value.get("negative_example_limit", 3)),
    )
    record_internal_usage(
        db,
        context,
        usage_type="preference_learning",
        idempotency_key=f"creative:preference-profile:{profile.id}",
        model=str(feature.value.get("preference_learning_model") or LEARNER_VERSION),
        details={
            "profile_id": profile.id,
            "profile_version": profile.profile_version,
            "modality": profile.modality,
            "content_type": profile.content_type,
            "build_reason": profile.build_reason,
        },
    )
    db.flush()
    return profile
