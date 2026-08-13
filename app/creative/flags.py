from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FeatureFlag

CREATIVE_FLAG = "creative_intelligence_enabled"
ONBOARDING_FLAG = "onboarding_calibration_enabled"

DEFAULT_CREATIVE_SETTINGS: dict[str, Any] = {
    "default_clubs_enabled": False,
    "learning_enabled": True,
    "application_enabled": True,
    "minimum_samples": 5,
    "minimum_confidence": 0.55,
    "recency_half_life_days": 180,
    "preference_recency_weight": 0.985,
    "positive_example_limit": 5,
    "negative_example_limit": 3,
    "vision_analysis_enabled": False,
    "preference_learning_model": "deterministic-v1",
    "creative_director_model": "structured-v1",
    "visual_trait_analysis_model": "",
    "onboarding_cost_limit_cents": 0,
    "feedback_sampling_rate": 1.0,
    "profile_rebuild_schedule": "nightly",
    "confidence_thresholds": {
        "first_tendencies": 0.35,
        "well_trained": 0.55,
        "high_confidence": 0.8,
    },
    "action_weights": {
        "selected": 1.5,
        "published": 3.0,
        "approved": 2.0,
        "rejected": 2.0,
        "regenerated": 1.5,
        "reverted": 2.0,
        "manually_edited": 1.25,
        "replaced": 1.5,
        "skipped": 0.25,
    },
    "source_weights": {
        "onboarding_explicit": 2.5,
        "onboarding_calibration": 2.0,
        "normal_usage": 1.0,
        "explicit_feedback": 2.5,
        "platform_admin_override": 3.0,
    },
}

DEFAULT_ONBOARDING_SETTINGS: dict[str, Any] = {
    "default_clubs_enabled": False,
    "announcement_image_count": 4,
    "result_image_count": 4,
    "announcement_text_count": 3,
    "result_text_count": 3,
}


@dataclass(frozen=True, slots=True)
class EffectiveFeature:
    enabled: bool
    value: dict[str, Any]
    source: str


def effective_feature(db: Session, club_id: str, key: str) -> EffectiveFeature:
    """Resolve a master flag plus an optional club override.

    Missing or disabled platform flags always fail closed.  A club flag can
    narrow access but cannot bypass the platform master switch.
    """

    global_flag = db.scalar(
        select(FeatureFlag).where(FeatureFlag.club_id.is_(None), FeatureFlag.key == key)
    )
    if global_flag is None or not global_flag.enabled:
        return EffectiveFeature(False, {}, "platform_disabled")
    club_flag = db.scalar(
        select(FeatureFlag).where(FeatureFlag.club_id == club_id, FeatureFlag.key == key)
    )
    defaults = (
        DEFAULT_CREATIVE_SETTINGS
        if key == CREATIVE_FLAG
        else DEFAULT_ONBOARDING_SETTINGS
        if key == ONBOARDING_FLAG
        else {}
    )
    merged = dict(defaults)
    merged.update(global_flag.value or {})
    if club_flag is not None:
        merged.update(club_flag.value or {})
        return EffectiveFeature(bool(club_flag.enabled), merged, "club_override")
    return EffectiveFeature(
        bool(merged.get("default_clubs_enabled", False)),
        merged,
        "platform_default",
    )


def creative_feature(db: Session, club_id: str) -> EffectiveFeature:
    return effective_feature(db, club_id, CREATIVE_FLAG)


def onboarding_feature(db: Session, club_id: str) -> EffectiveFeature:
    return effective_feature(db, club_id, ONBOARDING_FLAG)


def learning_enabled(db: Session, club_id: str) -> bool:
    feature = creative_feature(db, club_id)
    return feature.enabled and bool(feature.value.get("learning_enabled", True))


def application_enabled(db: Session, club_id: str) -> bool:
    feature = creative_feature(db, club_id)
    return feature.enabled and bool(feature.value.get("application_enabled", True))
