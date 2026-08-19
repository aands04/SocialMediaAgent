from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.match_reports.feedback import expire_feedback_requests, request_match_feedback
from app.match_reports.service import (
    generate_report_version,
    get_or_create_report,
    refresh_fupa_snapshot,
    refresh_report_sources,
)
from app.models import Club, ClubStatus, FupaMatchSnapshot, Game, MatchFeedbackRequest
from app.tenancy.state import system_scope, tenant_scope


@dataclass(frozen=True)
class MatchReportCycleResult:
    checked: int = 0
    snapshots: int = 0
    feedback_requests: int = 0
    generated: int = 0
    conflicts: int = 0
    failed: int = 0


def _final_result_available(snapshot: FupaMatchSnapshot, game: Game) -> bool:
    structured = snapshot.structured_data or {}
    score = structured.get("home_score") is not None and structured.get("away_score") is not None
    status = str(structured.get("status") or "").casefold()
    ticker_finished = any(
        str(item.get("event_type") or "") == "fulltime" for item in snapshot.ticker_data or []
    )
    # A clock-based assumption must never turn an interim score into a final
    # result.  FuPa has to mark the match as finished, or a club user has to
    # confirm the result explicitly.
    return bool(
        game.result_confirmed
        or (score and (ticker_finished or "finished" in status or "beendet" in status))
    )


def _open_feedback(db: Session, club_id: str, game_id: str, now: datetime) -> bool:
    return bool(
        db.scalar(
            select(MatchFeedbackRequest.id).where(
                MatchFeedbackRequest.club_id == club_id,
                MatchFeedbackRequest.game_id == game_id,
                MatchFeedbackRequest.status.in_(["pending", "sent"]),
                MatchFeedbackRequest.deadline_at > now,
            )
        )
    )


def run_match_report_cycle(db: Session, settings) -> MatchReportCycleResult:
    if not settings.fupa_reports_enabled:
        return MatchReportCycleResult()
    now = datetime.now(timezone.utc)
    with system_scope("FuPa-Spielberichte planen"):
        club_ids = list(
            db.scalars(
                select(Club.id).where(Club.status.in_([ClubStatus.ACTIVE, ClubStatus.TRIAL]))
            )
        )
    counters = {name: 0 for name in MatchReportCycleResult.__dataclass_fields__}
    for club_id in club_ids:
        with tenant_scope(club_id, "system:fupa-match-reports"):
            expire_feedback_requests(db, at=now)
            games = list(
                db.scalars(
                    select(Game)
                    .where(
                        Game.fupa_url.is_not(None),
                        Game.kickoff <= now - timedelta(minutes=settings.fupa_report_first_check_minutes),
                        Game.kickoff >= now - timedelta(hours=settings.fupa_report_max_poll_hours),
                        Game.status.not_in(["cancelled", "postponed"]),
                    )
                    .order_by(Game.kickoff)
                    .limit(settings.fupa_report_batch_size)
                )
            )
            for game in games:
                latest = db.scalar(
                    select(FupaMatchSnapshot)
                    .where(
                        FupaMatchSnapshot.club_id == club_id,
                        FupaMatchSnapshot.game_id == game.id,
                    )
                    .order_by(desc(FupaMatchSnapshot.fetched_at))
                )
                if latest and latest.next_check_at and latest.next_check_at > now:
                    continue
                counters["checked"] += 1
                try:
                    with db.begin_nested():
                        snapshot = refresh_fupa_snapshot(db, game, settings)
                        snapshot.next_check_at = now + timedelta(
                            seconds=settings.fupa_report_poll_interval_seconds
                        )
                        counters["snapshots"] += 1
                        if not _final_result_available(snapshot, game):
                            continue
                        report = get_or_create_report(db, game)
                        counters["feedback_requests"] += request_match_feedback(
                            db, game, settings
                        )
                        if _open_feedback(db, club_id, game.id, now):
                            report.status = "waiting_for_feedback"
                            continue
                        context = refresh_report_sources(db, report)
                        if context.has_blocking_conflicts:
                            counters["conflicts"] += 1
                            continue
                        if (
                            settings.fupa_report_automatic_generation_enabled
                            and report.current_version_number is None
                        ):
                            generate_report_version(db, report, settings, user_id=None)
                            counters["generated"] += 1
                except Exception as exc:
                    counters["failed"] += 1
                    try:
                        with db.begin_nested():
                            report = get_or_create_report(db, game)
                            report.status = "failed"
                            report.last_error_category = type(exc).__name__
                            report.last_error = str(exc)[:1000]
                    except Exception:
                        # A single corrupt game must not stop other tenants or
                        # games. The outer transaction remains usable because
                        # both operations are isolated by savepoints.
                        pass
            db.commit()
    return MatchReportCycleResult(**counters)
