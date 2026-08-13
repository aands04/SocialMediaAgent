from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.creative.flags import creative_feature
from app.creative.service import rebuild_all_profiles
from app.models import Club, ClubStatus
from app.tenancy.context import TenantContext
from app.tenancy.state import system_scope, tenant_scope

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class CreativeProfileCycleResult:
    clubs_checked: int = 0
    clubs_skipped: int = 0
    profiles_built: int = 0
    failures: int = 0


def _schedule_is_due(schedule: str, current: datetime) -> bool:
    normalized = str(schedule or "nightly").strip().casefold()
    if normalized == "disabled":
        return False
    if normalized == "hourly":
        return True
    # Der Worker prueft stuendlich. Der Nachtlauf wird in einem stabilen
    # UTC-Fenster ausgefuehrt; der Learner verhindert Doppelversionen.
    return normalized == "nightly" and current.hour == 2


def run_creative_profile_cycle(
    db: Session, *, now: datetime | None = None
) -> CreativeProfileCycleResult:
    """Rebuild due tenant profiles without affecting normal job processing.

    Each club is handled in its own transaction and TenantContext. A failure
    therefore never blocks another club, generation jobs, or publishing.
    """

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with system_scope("Aktive Vereine fuer Creative-Profile ermitteln"):
        club_ids = list(
            db.scalars(
                select(Club.id).where(
                    Club.status.in_((ClubStatus.ACTIVE, ClubStatus.TRIAL))
                )
            )
        )

    checked = skipped = built = failures = 0
    for club_id in club_ids:
        checked += 1
        try:
            with tenant_scope(club_id, "system:creative-profile-rebuild"):
                feature = creative_feature(db, club_id)
                schedule = str(feature.value.get("profile_rebuild_schedule", "nightly"))
                if (
                    not feature.enabled
                    or not bool(feature.value.get("learning_enabled", True))
                    or not _schedule_is_due(schedule, current)
                ):
                    skipped += 1
                    db.rollback()
                    continue
                profiles = rebuild_all_profiles(
                    db,
                    TenantContext(
                        club_id=club_id,
                        actor_user_id="system:creative-profile-rebuild",
                    ),
                    force=False,
                )
                built += len(profiles)
                db.commit()
        except Exception as exc:  # fail-open by domain requirement
            db.rollback()
            failures += 1
            log.warning(
                "creative_profile_cycle_failed",
                club_id=club_id,
                error_type=type(exc).__name__,
            )
    return CreativeProfileCycleResult(
        clubs_checked=checked,
        clubs_skipped=skipped,
        profiles_built=built,
        failures=failures,
    )
