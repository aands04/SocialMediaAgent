from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.creative.feedback import record_feedback
from app.creative.flags import onboarding_feature
from app.creative.learner import rebuild_profile
from app.creative.types import normalize_traits
from app.creative.usage import record_internal_usage
from app.models import ClubOnboardingSession, OnboardingCalibrationSample, uid
from app.tenancy.context import TenantContext

STEPS = (
    (1, "Willkommen", "Wir übernehmen vorhandene Vereinsdaten und ändern nichts ungefragt."),
    (2, "Vereinsdaten", "Prüfe Name, Kurzname und Zeitzone."),
    (3, "Mannschaften", "Prüfe die Mannschaften für künftige Inhalte."),
    (4, "Vereinsbranding", "Prüfe Logo, Farben und Schriften."),
    (5, "Medien", "Prüfe die Medienbibliothek und Bildauswahlregeln."),
    (6, "Social-Media-Kanäle", "Prüfe die verbundenen Ausgabekanäle."),
    (7, "Automatische Beiträge", "Prüfe Zeitpunkte, Freigaben und Automatikregeln."),
    (8, "Bildstil", "Wähle Wirkung, Spielerfokus, Hintergrund und Textmenge."),
    (9, "Textstil", "Bestimme Ton, Länge, Emojis und Ansprache."),
    (10, "Stil-Kalibrierung", "Bewerte sichere Testvarianten für ein erstes Profil."),
    (11, "Zusammenfassung", "Prüfe die Einrichtung und schließe sie ab."),
)

CALIBRATION_RECIPES = {
    "announcement": (
        {"graphic_style": "modern", "dynamics": "dynamisch", "image_text_amount": "wenig"},
        {"graphic_style": "klassisch", "dynamics": "ausgewogen", "image_text_amount": "normal"},
        {"graphic_style": "minimalistisch", "dynamics": "ruhig", "image_text_amount": "wenig"},
        {"graphic_style": "emotional", "dynamics": "dynamisch", "player_focus": "hoch"},
        {"graphic_style": "stadion", "background_style": "flutlicht", "contrast": "hoch"},
        {"graphic_style": "modern", "background_style": "hell", "player_focus": "mittel"},
    ),
    "result": (
        {"graphic_style": "modern", "contrast": "hoch", "image_text_amount": "wenig"},
        {"graphic_style": "klassisch", "contrast": "mittel", "image_text_amount": "normal"},
        {"graphic_style": "minimalistisch", "contrast": "hoch", "player_focus": "mittel"},
        {"graphic_style": "emotional", "contrast": "hoch", "player_focus": "hoch"},
        {"graphic_style": "stadion", "contrast": "hoch", "image_text_amount": "normal"},
        {"graphic_style": "modern", "contrast": "mittel", "background_style": "hell"},
    ),
}

EXPLICIT_STEP_SCOPES = {
    8: ("image", ("announcement", "result")),
    9: ("text", ("announcement", "result")),
}


def get_or_create_session(
    db: Session, context: TenantContext
) -> ClubOnboardingSession:
    item = db.scalar(
        select(ClubOnboardingSession).where(
            ClubOnboardingSession.club_id == context.club_id
        )
    )
    if item is None:
        item = ClubOnboardingSession(
            id=uid(),
            club_id=context.club_id,
            status="not_started",
            current_step=1,
            completed_steps=[],
            answers={},
            created_by_user_id=context.actor_user_id,
            last_actor_user_id=context.actor_user_id,
        )
        db.add(item)
        db.flush()
    return item


def save_step(
    db: Session,
    context: TenantContext,
    *,
    step: int,
    values: dict,
    expected_version: int | None = None,
) -> ClubOnboardingSession:
    if step < 1 or step > 11:
        raise ValueError("Ungültiger Einrichtungsschritt")
    item = get_or_create_session(db, context)
    if expected_version is not None and item.version != expected_version:
        raise ValueError(
            "Die Einrichtung wurde zwischenzeitlich geändert. Bitte lade die Seite neu."
        )
    normalized = normalize_traits(values)
    answers = dict(item.answers or {})
    feedback_refs = dict(answers.get("_explicit_feedback") or {})
    answers[f"step_{step}"] = normalized
    completed = list(dict.fromkeys([*(item.completed_steps or []), step]))
    item.answers = answers
    item.completed_steps = completed
    item.current_step = min(11, step + 1)
    item.status = "in_progress" if step < 10 else "calibration_pending"
    item.started_at = item.started_at or datetime.now(timezone.utc)
    item.last_actor_user_id = context.actor_user_id
    item.version += 1
    db.flush()
    scope = EXPLICIT_STEP_SCOPES.get(step)
    if scope is not None:
        modality, content_types = scope
        for content_type in content_types:
            for key, value in normalized.items():
                if isinstance(value, str) and value in {
                    "keine",
                    "keine_praeferenz",
                    "deaktiviert",
                }:
                    continue
                reference_key = f"{step}:{content_type}:{key}"
                previous_event_id = feedback_refs.get(reference_key)
                event = record_feedback(
                    db,
                    context,
                    modality=modality,
                    content_type=content_type,
                    action="selected",
                    source="onboarding_explicit",
                    sentiment="positive",
                    traits={key: value},
                    correction_of_id=previous_event_id,
                    idempotency_key=(
                        f"onboarding-explicit:{item.id}:{item.version}:"
                        f"{content_type}:{key}"
                    ),
                    metadata={"onboarding_step": step, "answer_key": key},
                    force=True,
                )
                if event is not None:
                    feedback_refs[reference_key] = event.id
        answers = dict(item.answers or {})
        answers["_explicit_feedback"] = feedback_refs
        item.answers = answers
        db.flush()
    return item


def seed_calibration(
    db: Session, context: TenantContext, session: ClubOnboardingSession
) -> list[OnboardingCalibrationSample]:
    feature = onboarding_feature(db, context.club_id)
    if not feature.enabled:
        return []
    existing = list(
        db.scalars(
            select(OnboardingCalibrationSample).where(
                OnboardingCalibrationSample.club_id == context.club_id,
                OnboardingCalibrationSample.session_id == session.id,
            )
        )
    )
    if existing:
        return existing
    created: list[OnboardingCalibrationSample] = []
    for content_type, recipes in CALIBRATION_RECIPES.items():
        image_count = max(
            2,
            min(6, int(feature.value.get(f"{content_type}_image_count", 4))),
        )
        for index, traits in enumerate(recipes[:image_count], 1):
            item = OnboardingCalibrationSample(
                id=uid(),
                club_id=context.club_id,
                session_id=session.id,
                modality="image",
                content_type=content_type,
                recipe_key=f"{content_type}-image-{index}",
                sample_index=index,
                preview_payload={"traits": traits, "fixture": True},
                status="ready",
                publishing_blocked=True,
            )
            db.add(item)
            created.append(item)
        text_count = max(
            2,
            min(6, int(feature.value.get(f"{content_type}_text_count", 3))),
        )
        for index in range(1, text_count + 1):
            tones = ("sachlich", "emotional", "motivierend", "locker", "professionell", "traditionsbewusst")
            tone = tones[index - 1]
            item = OnboardingCalibrationSample(
                id=uid(),
                club_id=context.club_id,
                session_id=session.id,
                modality="text",
                content_type=content_type,
                recipe_key=f"{content_type}-text-{index}",
                sample_index=index,
                rendered_text=f"Kalibrierungsbeispiel ({tone}) – ausschließlich zur Stilbewertung.",
                preview_payload={"traits": {"tone": tone, "text_length": "mittel"}, "fixture": True},
                status="ready",
                publishing_blocked=True,
            )
            db.add(item)
            created.append(item)
    db.flush()
    for item in created:
        usage = record_internal_usage(
            db,
            context,
            usage_type="onboarding_calibration",
            idempotency_key=f"creative:onboarding-sample:{item.id}",
            model="fixture-calibration-v1",
            details={
                "session_id": session.id,
                "sample_id": item.id,
                "modality": item.modality,
                "content_type": item.content_type,
                "fixture": True,
                "publishing_blocked": True,
            },
        )
        item.usage_ledger_entry_id = usage.id
    db.flush()
    return created


def rate_sample(
    db: Session,
    context: TenantContext,
    *,
    sample_id: str,
    rating: str,
    reason_codes: list[str] | None = None,
) -> OnboardingCalibrationSample:
    item = db.scalar(
        select(OnboardingCalibrationSample).where(
            OnboardingCalibrationSample.id == sample_id,
            OnboardingCalibrationSample.club_id == context.club_id,
        )
    )
    if item is None:
        raise ValueError("Kalibrierungsbeispiel nicht gefunden")
    if rating not in {"favorite", "second", "unsuitable", "neutral"}:
        raise ValueError("Unbekannte Bewertung")
    previous_feedback = dict(item.feedback or {})
    previous_event_id = previous_feedback.get("feedback_event_id")
    item.feedback = {"rating": rating, "reason_codes": reason_codes or []}
    item.ranking = {"favorite": 1, "second": 2}.get(rating)
    item.status = "rated"
    sentiment = "positive" if rating in {"favorite", "second"} else "negative" if rating == "unsuitable" else "neutral"
    event = record_feedback(
        db,
        context,
        modality=item.modality,
        content_type=item.content_type,
        action="selected" if sentiment == "positive" else "rejected" if sentiment == "negative" else "skipped",
        source="onboarding_calibration",
        sentiment=sentiment,
        reason_codes=reason_codes,
        traits=(item.preview_payload or {}).get("traits") or {},
        idempotency_key=f"onboarding:{item.id}:v{item.version + 1}:{rating}",
        metadata={"calibration_sample_id": item.id, "publishing_blocked": True},
        correction_of_id=previous_event_id,
        force=True,
    )
    if event is not None:
        item.feedback = {**item.feedback, "feedback_event_id": event.id}
    item.version += 1
    db.flush()
    return item


def complete_onboarding(
    db: Session, context: TenantContext, session: ClubOnboardingSession
) -> None:
    all_samples = list(
        db.scalars(
            select(OnboardingCalibrationSample).where(
                OnboardingCalibrationSample.club_id == context.club_id,
                OnboardingCalibrationSample.session_id == session.id,
            )
        )
    )
    rated_samples = [item for item in all_samples if item.status == "rated"]
    if all_samples:
        favorites = {
            (item.modality, item.content_type)
            for item in rated_samples
            if (item.feedback or {}).get("rating") == "favorite"
        }
        required = {
            (item.modality, item.content_type)
            for item in all_samples
            if item.status in {"ready", "rated"}
        }
        if not required.issubset(favorites):
            raise ValueError(
                "Bitte wähle für Bilder und Texte jeder Kalibrierungsgruppe "
                "mindestens einen Favoriten."
            )
    for modality in ("image", "text"):
        for content_type in ("announcement", "result"):
            rebuild_profile(
                db,
                context,
                modality=modality,
                content_type=content_type,
                reason="onboarding",
                force=True,
            )
    session.status = "completed"
    session.current_step = 11
    session.completed_steps = list(range(1, 12))
    session.completed_at = datetime.now(timezone.utc)
    session.last_actor_user_id = context.actor_user_id
    session.version += 1


def skip_calibration(session: ClubOnboardingSession, actor_user_id: str) -> None:
    session.status = "skipped"
    session.current_step = 11
    session.skipped_calibration_at = datetime.now(timezone.utc)
    session.last_actor_user_id = actor_user_id
    session.version += 1


def restart_calibration(
    db: Session, context: TenantContext, session: ClubOnboardingSession
) -> int:
    """Discard only disposable samples; immutable feedback/profile history remains."""

    rows = list(
        db.scalars(
            select(OnboardingCalibrationSample).where(
                OnboardingCalibrationSample.club_id == context.club_id,
                OnboardingCalibrationSample.session_id == session.id,
            )
        )
    )
    for row in rows:
        db.delete(row)
    session.status = "calibration_pending"
    session.current_step = 10
    session.completed_at = None
    session.skipped_calibration_at = None
    session.last_actor_user_id = context.actor_user_id
    session.version += 1
    db.flush()
    return len(rows)
