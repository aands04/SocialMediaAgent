from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.meta.api import MetaApiClient
from app.meta.oauth import check_connection
from app.meta.publishing import assert_automatic_scheduler_environment
from app.models import Club, ClubStatus, InstagramConnection, InstagramPage
from app.tenancy.state import system_scope, tenant_scope

log = structlog.get_logger()


@dataclass
class AutomaticConnectionCheckCycle:
    claimed: int = 0
    checked: int = 0
    succeeded: int = 0
    failed: int = 0


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _claim_due_connections(
    db: Session,
    settings: Settings,
    now: datetime,
) -> list[tuple[str, str]]:
    interval = settings.meta_connection_check_interval_seconds
    if interval <= 0:
        raise ValueError("META_CONNECTION_CHECK_INTERVAL_SECONDS muss positiv sein")

    due_before = now - timedelta(seconds=interval)
    # InstagramPage.last_check_at doubles as a short claim lease.  The
    # connection timestamp is updated only after Meta actually answered, so a
    # worker crash cannot make a stale connection appear freshly verified.
    claim_before = now - timedelta(minutes=5)
    query = (
        select(InstagramConnection, InstagramPage)
        .join(
            InstagramPage,
            InstagramPage.id == InstagramConnection.instagram_page_id,
        )
        .join(Club, Club.id == InstagramConnection.club_id)
        .where(
            Club.status.in_([ClubStatus.ACTIVE, ClubStatus.TRIAL]),
            InstagramPage.active.is_(True),
            InstagramConnection.encrypted_token.is_not(None),
            InstagramConnection.disconnected_at.is_(None),
            or_(
                InstagramConnection.last_check_at.is_(None),
                InstagramConnection.last_check_at <= due_before,
            ),
            or_(
                InstagramPage.last_check_at.is_(None),
                InstagramPage.last_check_at <= claim_before,
            ),
        )
        .order_by(InstagramConnection.last_check_at.asc())
        .limit(settings.meta_scheduler_batch_size)
    )
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True, of=InstagramConnection)

    rows = list(db.execute(query).all())
    claimed = []
    for connection, page in rows:
        page.last_check_at = now
        claimed.append((connection.id, connection.club_id))
    if claimed:
        db.commit()
    return claimed


def run_automatic_connection_check_cycle(
    db: Session,
    settings: Settings,
    *,
    api: MetaApiClient | None = None,
    now: datetime | None = None,
) -> AutomaticConnectionCheckCycle:
    """Revalidate active Instagram connections at most once per interval.

    This performs the same read-only profile validation as the protected
    dashboard action.  It never creates a media container or publishes media.
    """

    assert_automatic_scheduler_environment(settings)
    now = _utc(now or datetime.now(timezone.utc))
    with system_scope("Fällige Instagram-Verbindungsprüfungen global beanspruchen"):
        candidates = _claim_due_connections(db, settings, now)

    result = AutomaticConnectionCheckCycle(claimed=len(candidates))
    if not candidates:
        return result
    api = api or MetaApiClient(settings)

    for connection_id, club_id in candidates:
        with tenant_scope(club_id, "system:meta-connection-check"):
            connection = db.get(InstagramConnection, connection_id)
            if connection is None:
                continue
            result.checked += 1
            try:
                check_connection(db, settings, connection, None, api)
                result.succeeded += 1
            except Exception as exc:
                # check_connection persisted a sanitized error and an audit
                # record before raising. Continue with other tenant accounts.
                db.rollback()
                result.failed += 1
                log.warning(
                    "automatic_meta_connection_check_failed",
                    connection_id=connection_id,
                    error_type=type(exc).__name__,
                )
    return result
