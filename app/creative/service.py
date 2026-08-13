from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.creative.flags import (
    CREATIVE_FLAG,
    DEFAULT_CREATIVE_SETTINGS,
    creative_feature,
)
from app.creative.learner import rebuild_profile
from app.creative.types import normalize_content_type, normalize_modality
from app.models import (
    AuditLog,
    CreativeFeedbackEvent,
    CreativePreferenceProfile,
    FeatureFlag,
    User,
    uid,
)
from app.tenancy.context import TenantContext

CONTENT_LABELS = {
    "announcement": "Spielankündigung",
    "result": "Ergebnismeldung",
    "reminder": "Erinnerung",
    "goal": "Tor-Meldung",
    "live": "Live-Inhalt",
}
MODALITY_LABELS = {"image": "Bild", "text": "Text"}


def confidence_label(
    confidence: float,
    sample_count: int,
    *,
    minimum_samples: int,
    thresholds: dict | None = None,
) -> str:
    if sample_count < minimum_samples:
        return "Noch zu wenig Daten"
    values = thresholds or {}
    if confidence < float(values.get("well_trained", 0.55)):
        return "Erste Tendenzen erkannt"
    if confidence < float(values.get("high_confidence", 0.8)):
        return "Gut eingelernt"
    return "Hohe Sicherheit"


def _feature_row(db: Session, club_id: str) -> FeatureFlag | None:
    return db.scalar(
        select(FeatureFlag).where(
            FeatureFlag.club_id == club_id,
            FeatureFlag.key == CREATIVE_FLAG,
        )
    )


def update_club_settings(
    db: Session,
    context: TenantContext,
    actor: User,
    *,
    learning: bool,
    application: bool,
) -> FeatureFlag:
    context.assert_club(actor.club_id)
    row = _feature_row(db, context.club_id)
    value = dict(DEFAULT_CREATIVE_SETTINGS)
    if row is not None:
        value.update(row.value or {})
    value.update({"learning_enabled": learning, "application_enabled": application})
    if row is None:
        row = FeatureFlag(
            id=uid(),
            club_id=context.club_id,
            key=CREATIVE_FLAG,
            enabled=True,
            value=value,
            updated_by=actor.id,
        )
        db.add(row)
    else:
        row.enabled = True
        row.value = value
        row.updated_by = actor.id
        row.version += 1
    db.add(
        AuditLog(
            club_id=context.club_id,
            scope="club",
            user_id=actor.id,
            action="creative.settings_changed",
            entity_type="feature_flag",
            entity_id=row.id,
            details={"learning_enabled": learning, "application_enabled": application},
        )
    )
    db.flush()
    return row


def profile_cards(db: Session, context: TenantContext) -> list[dict]:
    feature = creative_feature(db, context.club_id)
    minimum_samples = max(1, int(feature.value.get("minimum_samples", 5)))
    thresholds = feature.value.get("confidence_thresholds") or {}
    rows = list(
        db.scalars(
            select(CreativePreferenceProfile)
            .where(
                CreativePreferenceProfile.club_id == context.club_id,
                CreativePreferenceProfile.status == "active",
            )
            .order_by(
                CreativePreferenceProfile.modality,
                CreativePreferenceProfile.content_type,
            )
        )
    )
    result: list[dict] = []
    for row in rows:
        score = float(row.confidence)
        result.append(
            {
                "id": row.id,
                "modality": row.modality,
                "modality_label": MODALITY_LABELS.get(row.modality, row.modality),
                "content_type": row.content_type,
                "content_label": CONTENT_LABELS.get(row.content_type, row.content_type),
                "version": row.profile_version,
                "confidence": round(score * 100),
                "confidence_label": confidence_label(
                    score,
                    row.sample_count,
                    minimum_samples=minimum_samples,
                    thresholds=thresholds,
                ),
                "sample_count": row.sample_count,
                "preferences": list((row.preferences or {}).get("traits") or [])[:8],
                "avoidances": list((row.avoidances or {}).get("traits") or [])[:8],
                "source_summary": row.source_summary or {},
                "last_feedback_at": row.last_feedback_at,
            }
        )
    return result


def profile_status(db: Session, context: TenantContext) -> dict:
    feature = creative_feature(db, context.club_id)
    feedback_count = int(
        db.scalar(
            select(func.count()).select_from(CreativeFeedbackEvent).where(
                CreativeFeedbackEvent.club_id == context.club_id
            )
        )
        or 0
    )
    cards = profile_cards(db, context)
    return {
        "feature_enabled": feature.enabled,
        "learning_enabled": feature.enabled
        and bool(feature.value.get("learning_enabled", True)),
        "application_enabled": feature.enabled
        and bool(feature.value.get("application_enabled", True)),
        "feedback_count": feedback_count,
        "profiles": cards,
        "last_update": max(
            (item["last_feedback_at"] for item in cards if item["last_feedback_at"]),
            default=None,
        ),
    }


def rebuild_all_profiles(
    db: Session, context: TenantContext, *, force: bool = True
) -> list[CreativePreferenceProfile]:
    scopes = list(
        db.execute(
            select(
                CreativeFeedbackEvent.modality,
                CreativeFeedbackEvent.content_type,
            )
            .where(CreativeFeedbackEvent.club_id == context.club_id)
            .distinct()
        )
    )
    built: list[CreativePreferenceProfile] = []
    for modality, content_type in scopes:
        item = rebuild_profile(
            db,
            context,
            modality=modality,
            content_type=content_type,
            reason="manual_rebuild",
            force=force,
        )
        if item is not None:
            built.append(item)
    return built


def reset_profiles(db: Session, context: TenantContext, actor: User) -> int:
    current = datetime.now(timezone.utc)
    active = list(
        db.scalars(
            select(CreativePreferenceProfile).where(
                CreativePreferenceProfile.club_id == context.club_id,
                CreativePreferenceProfile.status == "active",
            )
        )
    )
    for row in active:
        row.status = "archived"
        row.superseded_at = current
        next_version = int(
            db.scalar(
                select(
                    func.coalesce(func.max(CreativePreferenceProfile.profile_version), 0)
                ).where(
                    CreativePreferenceProfile.club_id == context.club_id,
                    CreativePreferenceProfile.modality == row.modality,
                    CreativePreferenceProfile.content_type == row.content_type,
                )
            )
            or 0
        ) + 1
        db.add(
            CreativePreferenceProfile(
                id=uid(),
                club_id=context.club_id,
                modality=row.modality,
                content_type=row.content_type,
                profile_version=next_version,
                status="active",
                preferences={"traits": []},
                avoidances={"traits": []},
                confidence=0,
                sample_count=0,
                source_summary={"reset": 1},
                learner_version="deterministic-v1",
                generated_by="club_admin_reset",
                build_reason="reset",
                activated_at=current,
            )
        )
    db.add(
        AuditLog(
            club_id=context.club_id,
            scope="club",
            user_id=actor.id,
            action="creative.profiles_reset",
            entity_type="creative_preference_profile",
            details={"archived_profiles": len(active)},
        )
    )
    db.flush()
    return len(active)


def restore_profile_version(
    db: Session,
    *,
    context: TenantContext,
    source_profile_id: str,
    actor_user_id: str,
) -> CreativePreferenceProfile:
    source = db.scalar(
        select(CreativePreferenceProfile).where(
            CreativePreferenceProfile.id == source_profile_id,
            CreativePreferenceProfile.club_id == context.club_id,
        )
    )
    if source is None:
        raise ValueError("Profilversion wurde nicht gefunden")
    modality = normalize_modality(source.modality)
    content_type = normalize_content_type(modality, source.content_type)
    current = datetime.now(timezone.utc)
    for active in db.scalars(
        select(CreativePreferenceProfile).where(
            CreativePreferenceProfile.club_id == context.club_id,
            CreativePreferenceProfile.modality == modality,
            CreativePreferenceProfile.content_type == content_type,
            CreativePreferenceProfile.status == "active",
        )
    ):
        active.status = "superseded"
        active.superseded_at = current
    next_version = int(
        db.scalar(
            select(func.coalesce(func.max(CreativePreferenceProfile.profile_version), 0)).where(
                CreativePreferenceProfile.club_id == context.club_id,
                CreativePreferenceProfile.modality == modality,
                CreativePreferenceProfile.content_type == content_type,
            )
        )
        or 0
    ) + 1
    restored = CreativePreferenceProfile(
        id=uid(),
        club_id=context.club_id,
        modality=modality,
        content_type=content_type,
        profile_version=next_version,
        status="active",
        preferences=source.preferences or {},
        avoidances=source.avoidances or {},
        confidence=source.confidence,
        sample_count=source.sample_count,
        source_summary=source.source_summary or {},
        learner_version=source.learner_version,
        generated_by="platform_admin_restore",
        build_reason="restore",
        last_feedback_at=source.last_feedback_at,
        activated_at=current,
    )
    db.add(restored)
    db.flush()
    return restored
