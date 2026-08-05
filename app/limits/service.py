from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Club,
    ClubAdditionalAllowance,
    ClubStatus,
    FontAsset,
    InstagramPage,
    PlanProfile,
    Team,
)


class LimitExceeded(ValueError):
    pass


LIMIT_FIELDS = {
    "teams": "max_teams",
    "storage_bytes": "max_storage_bytes",
    "ai_texts": "monthly_ai_texts",
    "ai_images": "monthly_ai_images",
    "fonts": "max_fonts",
    "instagram_pages": "max_instagram_pages",
}


@dataclass(frozen=True, slots=True)
class EffectiveLimit:
    key: str
    value: int
    profile_value: int
    override_value: int | None
    additional_value: int
    source: tuple[str, ...]


def _locked_club(db: Session, club_id: str) -> Club:
    statement = select(Club).where(Club.id == club_id)
    if db.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    club = db.scalar(statement)
    if club is None:
        raise LimitExceeded("Verein ist nicht vorhanden")
    if club.status not in {ClubStatus.ACTIVE, ClubStatus.TRIAL, ClubStatus.SETUP_PENDING}:
        raise LimitExceeded(f"Verein ist gesperrt (Status: {club.status.value})")
    return club


def effective_limits(
    db: Session,
    club_id: str,
    *,
    now: datetime | None = None,
    lock: bool = False,
) -> dict[str, EffectiveLimit]:
    now = now or datetime.now(timezone.utc)
    club = _locked_club(db, club_id) if lock else db.get(Club, club_id)
    if club is None:
        raise LimitExceeded("Verein ist nicht vorhanden")
    profile = db.get(PlanProfile, club.plan_profile_id)
    if profile is None:
        raise LimitExceeded("Dem Verein ist kein gültiges Limitprofil zugeordnet")
    additions = list(
        db.scalars(
            select(ClubAdditionalAllowance).where(
                ClubAdditionalAllowance.club_id == club_id,
                ClubAdditionalAllowance.starts_at <= now,
                ClubAdditionalAllowance.ends_at > now,
            )
        )
    )
    result: dict[str, EffectiveLimit] = {}
    overrides = club.limit_overrides or {}
    for key, field in LIMIT_FIELDS.items():
        profile_value = int(getattr(profile, field))
        raw_override = overrides.get(key)
        override_value = int(raw_override) if raw_override is not None else None
        base = override_value if override_value is not None else profile_value
        additional = sum(int(item.amount) for item in additions if item.limit_key == key)
        source = ("club_override",) if override_value is not None else ("plan_profile",)
        if additional:
            source = (*source, "temporary_allowance")
        result[key] = EffectiveLimit(
            key=key,
            value=max(0, base + additional),
            profile_value=profile_value,
            override_value=override_value,
            additional_value=additional,
            source=source,
        )
    return result


def current_resource_count(db: Session, club_id: str, key: str) -> int:
    if key == "teams":
        return int(
            db.scalar(
                select(func.count())
                .select_from(Team)
                .where(
                    Team.club_id == club_id,
                    Team.active.is_(True),
                    Team.archived_at.is_(None),
                )
            )
            or 0
        )
    if key == "instagram_pages":
        return int(
            db.scalar(
                select(func.count())
                .select_from(InstagramPage)
                .where(
                    InstagramPage.club_id == club_id,
                    InstagramPage.active.is_(True),
                    InstagramPage.archived_at.is_(None),
                )
            )
            or 0
        )
    if key == "fonts":
        return int(
            db.scalar(
                select(func.count())
                .select_from(FontAsset)
                .where(
                    FontAsset.club_id == club_id,
                    FontAsset.active.is_(True),
                    FontAsset.archived_at.is_(None),
                )
            )
            or 0
        )
    raise ValueError(f"Unbekannte Ressourcenquote: {key}")


def assert_resource_capacity(
    db: Session,
    club_id: str,
    key: str,
    *,
    requested: int = 1,
) -> EffectiveLimit:
    if requested <= 0:
        raise ValueError("Angeforderte Menge muss positiv sein")
    limit = effective_limits(db, club_id, lock=True)[key]
    current = current_resource_count(db, club_id, key)
    if current + requested > limit.value:
        labels = {
            "teams": "Mannschaftslimit",
            "instagram_pages": "Instagram-Seitenlimit",
            "fonts": "Schriftartenlimit",
        }
        raise LimitExceeded(f"{labels.get(key, key)} erreicht: {current} von {limit.value} aktiv")
    return limit
