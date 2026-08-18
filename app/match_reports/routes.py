from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.match_reports.context import build_match_content_context
from app.match_reports.feedback import request_match_feedback
from app.match_reports.fupa import validate_fupa_url
from app.match_reports.generator import MatchReportGenerationError
from app.match_reports.service import (
    MatchReportServiceError,
    add_manual_note,
    approve_report,
    create_edited_version,
    current_version,
    generate_report_version,
    get_or_create_report,
    prepare_fupa_publication,
    refresh_fupa_snapshot,
    refresh_report_sources,
)
from app.models import (
    ClubWritingExample,
    FupaMatchSnapshot,
    Game,
    MatchFeedbackContact,
    MatchFeedbackRequest,
    MatchFeedbackResponse,
    MatchManualNote,
    MatchReport,
    MatchReportPublication,
    MatchReportVersion,
    Role,
    Team,
    User,
    WhatsAppRecipient,
)
from app.web import check_csrf, csrf_token, current_user, require, require_admin

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

STATUS_LABELS = {
    "waiting_for_sources": "Wartet auf Quellen",
    "waiting_for_feedback": "Wartet auf Rückmeldungen",
    "ready_to_generate": "Bereit zur Erstellung",
    "conflict_requires_review": "Quellen müssen geprüft werden",
    "generating": "Bericht wird erstellt",
    "draft": "Entwurf",
    "review_required": "Prüfung erforderlich",
    "approved": "Freigegeben",
    "publishing": "Übertragung läuft",
    "published": "Übertragen",
    "failed": "Fehlgeschlagen",
    "cancelled": "Abgebrochen",
}


def _redirect(game_id: str, notice: str) -> RedirectResponse:
    return RedirectResponse(
        f"/games/{game_id}/match-report?notice={quote_plus(notice)}",
        status_code=303,
    )


def _game(db: Session, game_id: str, current: User, permission: str = "view") -> Game:
    if not current.club_id:
        raise HTTPException(403, "Ein eindeutiger Vereinskontext ist erforderlich")
    game = db.scalar(
        select(Game).where(Game.id == game_id, Game.club_id == current.club_id)
    )
    if not game:
        raise HTTPException(404, "Spiel nicht gefunden")
    require(current, db, permission, game.team_id)
    return game


def _report(db: Session, game: Game) -> MatchReport | None:
    return db.scalar(
        select(MatchReport).where(
            MatchReport.club_id == game.club_id,
            MatchReport.game_id == game.id,
            MatchReport.report_type == "match_report",
        )
    )


def _safe_action(db: Session, game_id: str, action, success: str) -> RedirectResponse:
    try:
        action()
        db.commit()
        return _redirect(game_id, success)
    except (MatchReportServiceError, MatchReportGenerationError, ValueError) as exc:
        db.rollback()
        return _redirect(game_id, f"Fehler: {exc}")


@router.get("/games/{game_id}/match-report", response_class=HTMLResponse)
def match_report_page(
    game_id: str,
    request: Request,
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    game = _game(db, game_id, current)
    team = db.scalar(
        select(Team).where(Team.id == game.team_id, Team.club_id == game.club_id)
    )
    report = _report(db, game)
    version = current_version(db, report) if report else None
    versions = (
        list(
            db.scalars(
                select(MatchReportVersion)
                .where(
                    MatchReportVersion.club_id == game.club_id,
                    MatchReportVersion.report_id == report.id,
                )
                .order_by(desc(MatchReportVersion.version_number))
            )
        )
        if report
        else []
    )
    snapshot = db.scalar(
        select(FupaMatchSnapshot)
        .where(FupaMatchSnapshot.club_id == game.club_id, FupaMatchSnapshot.game_id == game.id)
        .order_by(desc(FupaMatchSnapshot.fetched_at))
    )
    notes = list(
        db.scalars(
            select(MatchManualNote)
            .where(MatchManualNote.club_id == game.club_id, MatchManualNote.game_id == game.id)
            .order_by(desc(MatchManualNote.created_at))
        )
    )
    requests = list(
        db.scalars(
            select(MatchFeedbackRequest)
            .where(
                MatchFeedbackRequest.club_id == game.club_id,
                MatchFeedbackRequest.game_id == game.id,
            )
            .order_by(desc(MatchFeedbackRequest.created_at))
        )
    )
    responses_by_request = {
        item.request_id: item
        for item in db.scalars(
            select(MatchFeedbackResponse).where(
                MatchFeedbackResponse.club_id == game.club_id,
                MatchFeedbackResponse.request_id.in_([item.id for item in requests] or [""]),
            )
        )
    }
    contacts = list(
        db.scalars(
            select(MatchFeedbackContact)
            .where(
                MatchFeedbackContact.club_id == game.club_id,
                MatchFeedbackContact.team_id == game.team_id,
            )
            .order_by(MatchFeedbackContact.priority, MatchFeedbackContact.display_name)
        )
    )
    recipients = list(
        db.scalars(
            select(WhatsAppRecipient)
            .where(
                WhatsAppRecipient.club_id == game.club_id,
                WhatsAppRecipient.active.is_(True),
                WhatsAppRecipient.opt_in_status == "confirmed",
            )
            .order_by(WhatsAppRecipient.display_name, WhatsAppRecipient.normalized_phone)
        )
    )
    publications = (
        list(
            db.scalars(
                select(MatchReportPublication)
                .where(
                    MatchReportPublication.club_id == game.club_id,
                    MatchReportPublication.report_id == report.id,
                )
                .order_by(desc(MatchReportPublication.created_at))
            )
        )
        if report
        else []
    )
    context = None
    if report:
        try:
            context = build_match_content_context(db, game.id)
        except Exception:
            context = None
    return templates.TemplateResponse(
        request,
        "match_reports/detail.html",
        {
            "user": current,
            "csrf": csrf_token(request),
            "game": game,
            "team": team,
            "report": report,
            "version": version,
            "versions": versions,
            "snapshot": snapshot,
            "notes": notes,
            "feedback_requests": requests,
            "responses_by_request": responses_by_request,
            "contacts": contacts,
            "recipients": recipients,
            "publications": publications,
            "content_context": context,
            "status_labels": STATUS_LABELS,
            "automatic_enabled": get_settings().fupa_report_automatic_generation_enabled,
            "can_admin": current.role == Role.ADMIN,
        },
    )


@router.post("/games/{game_id}/match-report/source")
def save_source(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    fupa_url: str = Form(default=""),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    game = _game(db, game_id, current, "edit_post")

    def action():
        game.fupa_url = validate_fupa_url(fupa_url) if fupa_url.strip() else None

    return _safe_action(db, game.id, action, "FuPa-Quelle gespeichert")


@router.post("/games/{game_id}/match-report/refresh")
def refresh_sources(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    game = _game(db, game_id, current, "generate")

    def action():
        refresh_fupa_snapshot(db, game, get_settings())
        refresh_report_sources(db, get_or_create_report(db, game))

    return _safe_action(db, game.id, action, "FuPa-Quellen wurden aktualisiert")


@router.post("/games/{game_id}/match-report/generate")
def generate_report(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    desired_length: str = Form(default="medium"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    game = _game(db, game_id, current, "generate")
    if desired_length not in {"short", "medium", "long"}:
        raise HTTPException(400, "Ungültige Berichtslänge")

    def action():
        report = get_or_create_report(db, game)
        report.desired_length = desired_length
        generate_report_version(db, report, get_settings(), user_id=current.id)

    return _safe_action(db, game.id, action, "Neue Berichtsfassung wurde erstellt")


@router.post("/games/{game_id}/match-report/note")
def add_note(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    body: str = Form(),
    confirmed_facts: bool = Form(default=False),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    game = _game(db, game_id, current, "edit_post")
    return _safe_action(
        db,
        game.id,
        lambda: add_manual_note(
            db, game, body=body, confirmed_facts=confirmed_facts, user_id=current.id
        ),
        "Redaktionelle Notiz gespeichert",
    )


@router.post("/games/{game_id}/match-report/edit")
def edit_report(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    headline: str = Form(),
    teaser: str = Form(default=""),
    body: str = Form(),
    change_reason: str = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    game = _game(db, game_id, current, "edit_post")

    def action():
        report = _report(db, game)
        if not report:
            raise MatchReportServiceError("Es existiert noch kein Bericht")
        create_edited_version(
            db,
            report,
            headline=headline,
            teaser=teaser,
            body=body,
            change_reason=change_reason,
            user_id=current.id,
        )

    return _safe_action(db, game.id, action, "Neue redaktionelle Version gespeichert")


@router.post("/games/{game_id}/match-report/approve")
def approve(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    game = _game(db, game_id, current, "approve")

    def action():
        report = _report(db, game)
        if not report:
            raise MatchReportServiceError("Es existiert noch kein Bericht")
        approve_report(db, report, user_id=current.id)

    return _safe_action(db, game.id, action, "Spielbericht freigegeben")


@router.post("/games/{game_id}/match-report/publish")
def publish(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    game = _game(db, game_id, current, "publish_retry")

    def action():
        report = _report(db, game)
        if not report:
            raise MatchReportServiceError("Es existiert noch kein Bericht")
        prepare_fupa_publication(db, report, user_id=current.id)

    return _safe_action(
        db,
        game.id,
        action,
        "Manuelle FuPa-Übertragung wurde vorbereitet; es erfolgt keine automatische Veröffentlichung",
    )


@router.post("/games/{game_id}/match-report/feedback")
def request_feedback(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    game = _game(db, game_id, current, "generate")
    return _safe_action(
        db,
        game.id,
        lambda: request_match_feedback(db, game, get_settings()),
        "Rückfragen wurden an verfügbare Kontakte versendet",
    )


@router.post("/games/{game_id}/match-report/contact")
def save_contact(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    recipient_id: str = Form(),
    role_label: str = Form(default=""),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    game = _game(db, game_id, current, "edit_post")

    def action():
        recipient = db.scalar(
            select(WhatsAppRecipient).where(
                WhatsAppRecipient.id == recipient_id,
                WhatsAppRecipient.club_id == game.club_id,
                WhatsAppRecipient.active.is_(True),
                WhatsAppRecipient.opt_in_status == "confirmed",
            )
        )
        if not recipient:
            raise MatchReportServiceError("Der ausgewählte WhatsApp-Kontakt ist nicht zulässig")
        existing = db.scalar(
            select(MatchFeedbackContact).where(
                MatchFeedbackContact.club_id == game.club_id,
                MatchFeedbackContact.team_id == game.team_id,
                MatchFeedbackContact.recipient_id == recipient.id,
            )
        )
        if existing:
            existing.active = True
            existing.request_match_reports = True
            existing.role_label = role_label.strip()[:120] or None
        else:
            db.add(
                MatchFeedbackContact(
                    club_id=game.club_id,
                    team_id=game.team_id,
                    recipient_id=recipient.id,
                    normalized_phone=recipient.normalized_phone,
                    display_name=recipient.display_name or recipient.normalized_phone,
                    role_label=role_label.strip()[:120] or None,
                )
            )

    return _safe_action(db, game.id, action, "Rückfragekontakt gespeichert")


@router.post("/games/{game_id}/match-report/writing-example")
def save_writing_example(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    category: str = Form(default="general"),
    title: str = Form(default=""),
    body: str = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    game = _game(db, game_id, current, "edit_post")
    allowed = {"general", "win", "loss", "draw", "derby", "cup", "friendly"}

    def action():
        if category not in allowed or not body.strip():
            raise MatchReportServiceError("Kategorie und Beispieltext müssen gültig sein")
        db.add(
            ClubWritingExample(
                club_id=game.club_id,
                team_id=game.team_id,
                category=category,
                title=title.strip()[:240] or None,
                body=body.strip()[:12000],
                created_by=current.id,
            )
        )

    return _safe_action(db, game.id, action, "Schreibbeispiel gespeichert")
