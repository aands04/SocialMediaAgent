from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import PlanProfile, RegistrationIntent


class RegistrationDisabled(PermissionError):
    pass


def begin_registration(
    db: Session,
    settings: Settings,
    *,
    email: str,
    club_name: str,
    plan_profile_id: str | None,
) -> tuple[RegistrationIntent, str]:
    if not settings.self_registration_enabled:
        raise RegistrationDisabled("Die öffentliche Vereinsregistrierung ist deaktiviert")
    if settings.billing_enabled:
        raise RegistrationDisabled(
            "Zahlungsabwicklung ist in dieser Ausbaustufe nicht freigeschaltet"
        )
    clean_email = email.strip().casefold()
    clean_name = club_name.strip()
    if "@" not in clean_email or not clean_name:
        raise ValueError("E-Mail-Adresse und Vereinsname sind erforderlich")
    if plan_profile_id and db.get(PlanProfile, plan_profile_id) is None:
        raise ValueError("Tarifprofil ist nicht vorhanden")
    raw_token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    intent = RegistrationIntent(
        email=clean_email,
        club_name=clean_name,
        requested_plan_profile_id=plan_profile_id,
        status="email_confirmation_pending",
        email_token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        email_token_expires_at=now + timedelta(hours=24),
        expires_at=now + timedelta(days=7),
        registration_metadata={"billing_required": False},
    )
    db.add(intent)
    db.flush()
    return intent, raw_token
