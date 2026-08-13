from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import UsageLedgerEntry, UsageStatus, uid
from app.tenancy.context import TenantContext
from app.usage.service import billing_period

INTERNAL_USAGE_TYPES = {
    "creative_director",
    "preference_learning",
    "visual_trait_analysis",
    "onboarding_calibration",
}


def record_internal_usage(
    db: Session,
    context: TenantContext,
    *,
    usage_type: str,
    idempotency_key: str,
    model: str,
    quantity: int = 1,
    provider: str = "internal",
    provider_cost: Decimal | float | None = None,
    generation_job_id: str | None = None,
    post_id: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> UsageLedgerEntry:
    """Record non-quota Creative Intelligence work idempotently.

    These entries remain separate from billable image/text quotas.  Provider
    cost is only stored when an actual provider reports one; no estimates are
    presented as real cost.
    """

    context.assert_club(context.club_id)
    if usage_type not in INTERNAL_USAGE_TYPES:
        raise ValueError("Unbekannter Creative-Intelligence-Verbrauchstyp")
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 255:
        raise ValueError("Idempotenzschlüssel fehlt oder ist zu lang")
    existing = db.scalar(
        select(UsageLedgerEntry).where(
            UsageLedgerEntry.club_id == context.club_id,
            UsageLedgerEntry.idempotency_key == key,
        )
    )
    if existing is not None:
        return existing
    start, end = billing_period()
    actor_user_id = str(context.actor_user_id or "").strip()
    entry = UsageLedgerEntry(
        id=uid(),
        club_id=context.club_id,
        user_id=(
            None
            if actor_user_id == "system" or actor_user_id.startswith("system:")
            else actor_user_id
        ),
        generation_job_id=generation_job_id,
        post_id=post_id,
        generation_type=usage_type,
        provider=str(provider or "internal")[:80],
        model=str(model or "unknown")[:120],
        period_start=start,
        period_end=end,
        status=UsageStatus.COMPLETED_NOT_BILLABLE,
        reserved_quantity=0,
        actual_quantity=max(0, int(quantity)),
        provider_cost=provider_cost,
        billable=False,
        platform_test=True,
        idempotency_key=key,
        details=dict(details or {}),
    )
    db.add(entry)
    db.flush()
    return entry
