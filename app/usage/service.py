from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.limits.service import effective_limits
from app.models import UsageLedgerEntry, UsageStatus, uid


class QuotaExceeded(ValueError):
    pass


ACTIVE_RESERVATIONS = {UsageStatus.RESERVED, UsageStatus.PROVIDER_PROCESSING}
BILLABLE_RESULTS = {UsageStatus.COMPLETED_BILLABLE, UsageStatus.REJECTED_BY_USER}


@dataclass(frozen=True, slots=True)
class UsageSummary:
    limit: int
    completed: int
    reserved: int
    remaining: int
    period_start: datetime
    period_end: datetime


def billing_period(value: datetime | None = None) -> tuple[datetime, datetime]:
    current = (value or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = datetime(current.year, current.month, 1, tzinfo=timezone.utc)
    days = monthrange(current.year, current.month)[1]
    end = datetime(current.year, current.month, days, 23, 59, 59, 999999, tzinfo=timezone.utc)
    return start, end


def usage_summary(
    db: Session,
    club_id: str,
    generation_type: str,
    *,
    now: datetime | None = None,
    lock: bool = False,
) -> UsageSummary:
    if generation_type not in {"text", "image"}:
        raise ValueError("Unbekannter KI-Verbrauchstyp")
    start, end = billing_period(now)
    key = "ai_texts" if generation_type == "text" else "ai_images"
    limit = effective_limits(db, club_id, now=now, lock=lock)[key].value
    completed = int(
        db.scalar(
            select(func.coalesce(func.sum(UsageLedgerEntry.actual_quantity), 0)).where(
                UsageLedgerEntry.club_id == club_id,
                UsageLedgerEntry.generation_type == generation_type,
                UsageLedgerEntry.period_start == start,
                UsageLedgerEntry.status.in_(BILLABLE_RESULTS),
                UsageLedgerEntry.billable.is_(True),
            )
        )
        or 0
    )
    reserved = int(
        db.scalar(
            select(func.coalesce(func.sum(UsageLedgerEntry.reserved_quantity), 0)).where(
                UsageLedgerEntry.club_id == club_id,
                UsageLedgerEntry.generation_type == generation_type,
                UsageLedgerEntry.period_start == start,
                UsageLedgerEntry.status.in_(ACTIVE_RESERVATIONS),
            )
        )
        or 0
    )
    return UsageSummary(
        limit=limit,
        completed=completed,
        reserved=reserved,
        remaining=max(0, limit - completed - reserved),
        period_start=start,
        period_end=end,
    )


def reserve_usage(
    db: Session,
    *,
    club_id: str,
    generation_type: str,
    quantity: int,
    idempotency_key: str,
    provider: str,
    model: str,
    user_id: str | None = None,
    generation_job_id: str | None = None,
    post_id: str | None = None,
    platform_test: bool = False,
    prompt_template_id: str | None = None,
    prompt_version: int | None = None,
) -> UsageLedgerEntry:
    existing = db.scalar(
        select(UsageLedgerEntry).where(
            UsageLedgerEntry.club_id == club_id,
            UsageLedgerEntry.idempotency_key == idempotency_key,
        )
    )
    if existing:
        return existing
    if quantity <= 0:
        raise ValueError("Reservierte Menge muss positiv sein")
    start, end = billing_period()
    if not platform_test:
        summary = usage_summary(db, club_id, generation_type, lock=True)
        if quantity > summary.remaining:
            raise QuotaExceeded(
                f"Monatliches KI-{generation_type}-Kontingent erreicht: "
                f"{summary.completed} verbraucht, {summary.reserved} reserviert, "
                f"Limit {summary.limit}"
            )
    entry = UsageLedgerEntry(
        id=uid(),
        club_id=club_id,
        user_id=user_id,
        generation_job_id=generation_job_id,
        post_id=post_id,
        generation_type=generation_type,
        provider=provider,
        model=model,
        prompt_template_id=prompt_template_id,
        prompt_version=prompt_version,
        period_start=start,
        period_end=end,
        status=UsageStatus.RESERVED,
        reserved_quantity=quantity,
        actual_quantity=0,
        billable=False,
        platform_test=platform_test,
        idempotency_key=idempotency_key,
        details={},
    )
    db.add(entry)
    db.flush()
    return entry


def complete_usage(
    db: Session,
    entry: UsageLedgerEntry,
    *,
    actual_quantity: int | None = None,
    provider_cost: Decimal | float | None = None,
    post_id: str | None = None,
) -> None:
    if entry.status in BILLABLE_RESULTS:
        return
    quantity = entry.reserved_quantity if actual_quantity is None else actual_quantity
    if quantity < 0 or quantity > entry.reserved_quantity:
        raise ValueError("Tatsächlicher Verbrauch liegt außerhalb der Reservierung")
    entry.status = (
        UsageStatus.COMPLETED_NOT_BILLABLE
        if entry.platform_test
        else UsageStatus.COMPLETED_BILLABLE
    )
    entry.actual_quantity = quantity
    entry.reserved_quantity = 0
    entry.billable = not entry.platform_test
    entry.provider_cost = provider_cost
    entry.post_id = post_id or entry.post_id


def release_usage(
    entry: UsageLedgerEntry,
    *,
    technical: bool = True,
    provider_cost: Decimal | float | None = None,
    details: dict | None = None,
) -> None:
    if entry.status not in ACTIVE_RESERVATIONS:
        return
    entry.status = UsageStatus.FAILED_TECHNICAL if technical else UsageStatus.CANCELLED
    entry.actual_quantity = 0
    entry.reserved_quantity = 0
    entry.billable = False
    entry.provider_cost = provider_cost
    entry.details = {**(entry.details or {}), **(details or {})}


def mark_post_rejected(db: Session, post_id: str) -> int:
    entries = list(
        db.scalars(
            select(UsageLedgerEntry).where(
                UsageLedgerEntry.post_id == post_id,
                UsageLedgerEntry.status == UsageStatus.COMPLETED_BILLABLE,
            )
        )
    )
    for entry in entries:
        entry.status = UsageStatus.REJECTED_BY_USER
        entry.billable = True
    return len(entries)
