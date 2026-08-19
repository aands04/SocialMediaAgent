from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.match_reports.context import build_match_content_context
from app.match_reports.fupa import FupaReader
from app.match_reports.generator import build_match_report_generator
from app.match_reports.publisher import ManualFupaPublisher
from app.models import (
    AuditLog,
    FupaMatchSnapshot,
    Game,
    MatchManualNote,
    MatchReport,
    MatchReportPublication,
    MatchReportVersion,
)


class MatchReportServiceError(RuntimeError):
    pass


def get_or_create_report(db: Session, game: Game) -> MatchReport:
    report = db.scalar(
        select(MatchReport).where(
            MatchReport.club_id == game.club_id,
            MatchReport.game_id == game.id,
            MatchReport.report_type == "match_report",
        )
    )
    if report:
        return report
    report = MatchReport(club_id=game.club_id, game_id=game.id, team_id=game.team_id)
    db.add(report)
    db.flush()
    return report


def refresh_fupa_snapshot(db: Session, game: Game, settings) -> FupaMatchSnapshot:
    source_url = game.fupa_url
    if not source_url:
        raise MatchReportServiceError("Für dieses Spiel ist noch keine FuPa-Spiel-URL hinterlegt")
    try:
        result = FupaReader(timeout_seconds=settings.fupa_http_timeout_seconds).fetch(source_url)
    except Exception as exc:
        digest = hashlib.sha256(f"{source_url}:{type(exc).__name__}:{exc}".encode()).hexdigest()
        existing = db.scalar(
            select(FupaMatchSnapshot).where(
                FupaMatchSnapshot.club_id == game.club_id,
                FupaMatchSnapshot.game_id == game.id,
                FupaMatchSnapshot.content_digest == digest,
            )
        )
        if existing:
            existing.attempt_count += 1
            existing.fetched_at = datetime.now(timezone.utc)
            return existing
        snapshot = FupaMatchSnapshot(
            club_id=game.club_id,
            game_id=game.id,
            source_url=source_url,
            fetch_status="failed",
            structured_data={},
            ticker_data=[],
            source_metadata={},
            content_digest=digest,
            last_error_category=type(exc).__name__,
            last_error=str(exc)[:1000],
        )
        db.add(snapshot)
        db.flush()
        return snapshot
    existing = db.scalar(
        select(FupaMatchSnapshot).where(
            FupaMatchSnapshot.club_id == game.club_id,
            FupaMatchSnapshot.game_id == game.id,
            FupaMatchSnapshot.content_digest == result.content_digest,
        )
    )
    if existing:
        return existing
    snapshot = FupaMatchSnapshot(
        club_id=game.club_id,
        game_id=game.id,
        source_url=result.source_url,
        fetch_status=result.fetch_status,
        structured_data=result.structured_data,
        ticker_data=result.ticker_json(),
        source_metadata=result.metadata,
        content_digest=result.content_digest,
        last_error_category=result.error_category,
        last_error=result.error,
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def refresh_report_sources(db: Session, report: MatchReport):
    context = build_match_content_context(db, report.game_id)
    report.source_summary = {
        "facts": context.facts,
        "event_count": len(context.events),
        "feedback_count": len(context.feedback),
        "manual_note_count": len(context.manual_notes),
        "provenance": context.provenance,
    }
    report.source_conflicts = [item.__dict__ for item in context.conflicts]
    report.status = (
        "conflict_requires_review" if context.has_blocking_conflicts else "ready_to_generate"
    )
    return context


def generate_report_version(
    db: Session,
    report: MatchReport,
    settings,
    *,
    user_id: str | None,
    change_reason: str | None = None,
) -> MatchReportVersion:
    context = refresh_report_sources(db, report)
    if context.has_blocking_conflicts:
        raise MatchReportServiceError("Die Quellen müssen vor der Generierung geprüft werden")
    report.status = "generating"
    result = build_match_report_generator(settings).generate(
        context,
        desired_length=report.desired_length,
    )
    next_number = (
        int(
            db.scalar(
                select(func.coalesce(func.max(MatchReportVersion.version_number), 0)).where(
                    MatchReportVersion.club_id == report.club_id,
                    MatchReportVersion.report_id == report.id,
                )
            )
            or 0
        )
        + 1
    )
    version = MatchReportVersion(
        club_id=report.club_id,
        report_id=report.id,
        version_number=next_number,
        origin="generated",
        headline=result.headline,
        teaser=result.teaser,
        body=result.body,
        used_sources=list(result.used_sources),
        omitted_sources=list(result.omitted_sources),
        source_snapshot=context.as_dict(),
        model=result.model,
        prompt_template_id=result.prompt_template_id,
        prompt_version=result.prompt_version,
        change_reason=change_reason,
        created_by=user_id,
    )
    db.add(version)
    db.flush()
    report.current_version_number = next_number
    report.status = "review_required"
    report.approved_by = None
    report.approved_at = None
    db.add(
        AuditLog(
            club_id=report.club_id,
            user_id=user_id,
            action="match_report.version_generated",
            entity_type="match_report",
            entity_id=report.id,
            details={"version": next_number, "source_count": len(result.used_sources)},
        )
    )
    return version


def create_edited_version(
    db: Session,
    report: MatchReport,
    *,
    headline: str,
    teaser: str | None,
    body: str,
    user_id: str,
    change_reason: str,
) -> MatchReportVersion:
    current = current_version(db, report)
    if not current:
        raise MatchReportServiceError("Es existiert noch keine bearbeitbare Berichtsfassung")
    if not headline.strip() or not body.strip() or not change_reason.strip():
        raise MatchReportServiceError("Überschrift, Text und Änderungsgrund sind erforderlich")
    number = (report.current_version_number or 0) + 1
    version = MatchReportVersion(
        club_id=report.club_id,
        report_id=report.id,
        version_number=number,
        origin="edited",
        headline=headline.strip()[:300],
        teaser=teaser.strip() if teaser else None,
        body=body.strip(),
        used_sources=current.used_sources,
        omitted_sources=current.omitted_sources,
        source_snapshot=current.source_snapshot,
        model=None,
        prompt_template_id=current.prompt_template_id,
        prompt_version=current.prompt_version,
        change_reason=change_reason.strip(),
        created_by=user_id,
    )
    db.add(version)
    report.current_version_number = number
    report.status = "review_required"
    report.approved_by = None
    report.approved_at = None
    return version


def current_version(db: Session, report: MatchReport) -> MatchReportVersion | None:
    if report.current_version_number is None:
        return None
    return db.scalar(
        select(MatchReportVersion).where(
            MatchReportVersion.club_id == report.club_id,
            MatchReportVersion.report_id == report.id,
            MatchReportVersion.version_number == report.current_version_number,
        )
    )


def delete_unpublished_report(
    db: Session,
    report: MatchReport,
    *,
    user_id: str,
) -> None:
    """Delete an unpublished report while retaining its independent sources and audit trail."""

    publication_statuses = set(
        db.scalars(
            select(MatchReportPublication.status).where(
                MatchReportPublication.club_id == report.club_id,
                MatchReportPublication.report_id == report.id,
            )
        )
    )
    if (
        report.status in {"publishing", "published"}
        or report.published_at is not None
        or publication_statuses.intersection({"publishing", "published"})
    ):
        raise MatchReportServiceError(
            "Ein bereits übertragener oder aktuell übertragener Spielbericht kann nicht gelöscht werden"
        )

    version_count = int(
        db.scalar(
            select(func.count(MatchReportVersion.id)).where(
                MatchReportVersion.club_id == report.club_id,
                MatchReportVersion.report_id == report.id,
            )
        )
        or 0
    )
    publication_count = int(
        db.scalar(
            select(func.count(MatchReportPublication.id)).where(
                MatchReportPublication.club_id == report.club_id,
                MatchReportPublication.report_id == report.id,
            )
        )
        or 0
    )
    report_id = report.id
    club_id = report.club_id
    game_id = report.game_id
    previous_status = report.status

    # Delete dependants explicitly so this operation behaves identically on
    # PostgreSQL and SQLite despite the publication-to-version RESTRICT key.
    db.execute(
        delete(MatchReportPublication).where(
            MatchReportPublication.club_id == club_id,
            MatchReportPublication.report_id == report_id,
        )
    )
    db.execute(
        delete(MatchReportVersion).where(
            MatchReportVersion.club_id == club_id,
            MatchReportVersion.report_id == report_id,
        )
    )
    db.execute(
        delete(MatchReport).where(
            MatchReport.club_id == club_id,
            MatchReport.id == report_id,
        )
    )
    db.add(
        AuditLog(
            club_id=club_id,
            user_id=user_id,
            action="match_report.deleted",
            entity_type="match_report",
            entity_id=report_id,
            details={
                "game_id": game_id,
                "previous_status": previous_status,
                "deleted_versions": version_count,
                "deleted_publication_preparations": publication_count,
                "sources_retained": True,
            },
        )
    )


def approve_report(db: Session, report: MatchReport, *, user_id: str) -> MatchReportVersion:
    context = refresh_report_sources(db, report)
    version = current_version(db, report)
    if context.has_blocking_conflicts or version is None:
        raise MatchReportServiceError(
            "Der Bericht ist wegen offener Quellenkonflikte nicht freigabefähig"
        )
    report.status = "approved"
    report.approved_by = user_id
    report.approved_at = datetime.now(timezone.utc)
    db.add(
        AuditLog(
            club_id=report.club_id,
            user_id=user_id,
            action="match_report.approved",
            entity_type="match_report",
            entity_id=report.id,
            details={"version": version.version_number},
        )
    )
    return version


def prepare_fupa_publication(db: Session, report: MatchReport, *, user_id: str):
    if report.status != "approved":
        raise MatchReportServiceError("Nur freigegebene Berichte können an FuPa übertragen werden")
    version = current_version(db, report)
    if not version:
        raise MatchReportServiceError("Freigegebene Berichtsfassung fehlt")
    key = hashlib.sha256(f"{report.id}:{version.id}:fupa".encode()).hexdigest()
    publication = db.scalar(
        select(MatchReportPublication).where(
            MatchReportPublication.club_id == report.club_id,
            MatchReportPublication.idempotency_key == key,
        )
    )
    if publication:
        return publication
    context = build_match_content_context(db, report.game_id)
    result = ManualFupaPublisher().publish(
        context=context,
        version=version,
        idempotency_key=key,
    )
    publication = MatchReportPublication(
        club_id=report.club_id,
        report_id=report.id,
        version_id=version.id,
        status=result.status,
        idempotency_key=key,
        external_url=result.external_url,
        last_error=result.message,
    )
    db.add(publication)
    db.add(
        AuditLog(
            club_id=report.club_id,
            user_id=user_id,
            action="match_report.fupa_transfer_prepared",
            entity_type="match_report",
            entity_id=report.id,
            details={"version": version.version_number, "status": result.status},
        )
    )
    return publication


def add_manual_note(
    db: Session,
    game: Game,
    *,
    body: str,
    confirmed_facts: bool,
    user_id: str,
):
    if not body.strip():
        raise MatchReportServiceError("Die Notiz darf nicht leer sein")
    note = MatchManualNote(
        club_id=game.club_id,
        game_id=game.id,
        body=body.strip()[:5000],
        confirmed_facts=confirmed_facts,
        created_by=user_id,
    )
    db.add(note)
    return note
