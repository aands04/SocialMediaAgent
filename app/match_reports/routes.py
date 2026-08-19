from __future__ import annotations

from datetime import datetime, timezone
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
from app.match_reports.telegram import create_contact_link, feedback_provider_enabled
from app.models import (
    AuditLog,
    Club,
    ClubWritingExample,
    FupaMatchSnapshot,
    Game,
    MatchFeedbackContact,
    MatchFeedbackEndpoint,
    MatchFeedbackRequest,
    MatchFeedbackResponse,
    MatchManualNote,
    MatchReport,
    MatchReportPublication,
    MatchReportVersion,
    Role,
    SocialChannelConnection,
    Team,
    User,
    WhatsAppRecipient,
)
from app.web import berlin_datetime, check_csrf, csrf_token, current_user, require, require_admin

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["berlin"] = berlin_datetime

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
    game = db.scalar(select(Game).where(Game.id == game_id, Game.club_id == current.club_id))
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
    team = db.scalar(select(Team).where(Team.id == game.team_id, Team.club_id == game.club_id))
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
    endpoints = list(
        db.scalars(
            select(MatchFeedbackEndpoint).where(
                MatchFeedbackEndpoint.club_id == game.club_id,
                MatchFeedbackEndpoint.contact_id.in_([item.id for item in contacts] or [""]),
            )
        )
    )
    endpoints_by_contact = {(item.contact_id, item.provider): item for item in endpoints}
    telegram_connection = db.scalar(
        select(SocialChannelConnection).where(
            SocialChannelConnection.club_id == game.club_id,
            SocialChannelConnection.channel_type == "telegram",
            SocialChannelConnection.active.is_(True),
            SocialChannelConnection.status == "connected",
        )
    )
    club = db.scalar(select(Club).where(Club.id == game.club_id))
    team_messenger_defaults = (team.rules or {}).get("match_feedback_messenger") or {}
    club_messenger_defaults = (
        ((club.technical_settings or {}).get("match_feedback_messenger") or {}) if club else {}
    )
    whatsapp_available = feedback_provider_enabled(db, game.club_id, "whatsapp")
    telegram_available = feedback_provider_enabled(db, game.club_id, "telegram")
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
            "endpoints_by_contact": endpoints_by_contact,
            "recipients": recipients,
            "telegram_connection": telegram_connection,
            "whatsapp_available": whatsapp_available,
            "telegram_available": telegram_available,
            "team_messenger_defaults": team_messenger_defaults,
            "club_messenger_defaults": club_messenger_defaults,
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
    if not feedback_provider_enabled(db, game.club_id, "whatsapp"):
        raise HTTPException(403, "WhatsApp ist für diesen Verein nicht freigeschaltet")

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
            existing.preferred_provider = existing.preferred_provider or "whatsapp"
        else:
            db.add(
                MatchFeedbackContact(
                    club_id=game.club_id,
                    team_id=game.team_id,
                    recipient_id=recipient.id,
                    normalized_phone=recipient.normalized_phone,
                    display_name=recipient.display_name or recipient.normalized_phone,
                    role_label=role_label.strip()[:120] or None,
                    preferred_provider="whatsapp",
                )
            )

    return _safe_action(db, game.id, action, "Rückfragekontakt gespeichert")


@router.post("/games/{game_id}/match-report/contact/telegram", response_class=HTMLResponse)
def create_telegram_contact(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    contact_id: str = Form(default=""),
    display_name: str = Form(default=""),
    role_label: str = Form(default=""),
    fallback_provider: str = Form(default=""),
    make_preferred: bool = Form(default=False),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    game = _game(db, game_id, current, "edit_post")
    if not feedback_provider_enabled(db, game.club_id, "telegram"):
        raise HTTPException(403, "Telegram ist für diesen Verein nicht freigeschaltet")
    fallback = fallback_provider if fallback_provider in {"", "whatsapp"} else ""
    if fallback and not feedback_provider_enabled(db, game.club_id, fallback):
        raise HTTPException(403, "Der gewählte Ersatz-Messenger ist nicht freigeschaltet")
    connection = db.scalar(
        select(SocialChannelConnection).where(
            SocialChannelConnection.club_id == game.club_id,
            SocialChannelConnection.channel_type == "telegram",
            SocialChannelConnection.active.is_(True),
            SocialChannelConnection.status == "connected",
        )
    )
    if connection is None:
        raise HTTPException(409, "Telegram muss zuerst unter Social-Media-Kanäle verbunden werden")

    reused_existing = bool(contact_id.strip())
    if reused_existing:
        contact = db.scalar(
            select(MatchFeedbackContact).where(
                MatchFeedbackContact.id == contact_id.strip(),
                MatchFeedbackContact.club_id == game.club_id,
                MatchFeedbackContact.team_id == game.team_id,
                MatchFeedbackContact.active.is_(True),
            )
        )
        if contact is None:
            raise HTTPException(404, "Der gewählte Rückfragekontakt wurde nicht gefunden")
        existing_endpoint = db.scalar(
            select(MatchFeedbackEndpoint).where(
                MatchFeedbackEndpoint.club_id == game.club_id,
                MatchFeedbackEndpoint.contact_id == contact.id,
                MatchFeedbackEndpoint.provider == "telegram",
                MatchFeedbackEndpoint.status == "connected",
            )
        )
        if existing_endpoint is not None:
            raise HTTPException(409, "Dieser Kontakt ist bereits mit Telegram verbunden")
        if role_label.strip():
            contact.role_label = role_label.strip()[:120]
        if make_preferred:
            previous = contact.preferred_provider
            contact.preferred_provider = "telegram"
            contact.fallback_provider = (
                previous
                if previous in {"whatsapp"}
                and feedback_provider_enabled(db, game.club_id, previous)
                else fallback or None
            )
    else:
        name = display_name.strip()[:160]
        if len(name) < 2:
            raise HTTPException(422, "Bitte einen Anzeigenamen angeben")
        contact = MatchFeedbackContact(
            club_id=game.club_id,
            team_id=game.team_id,
            recipient_id=None,
            normalized_phone=None,
            display_name=name,
            role_label=role_label.strip()[:120] or None,
            preferred_provider="telegram",
            fallback_provider=fallback or None,
        )
        db.add(contact)
        db.flush()
    deep_link = create_contact_link(
        db,
        contact=contact,
        connection=connection,
        created_by=current.id,
        settings=get_settings(),
    )
    db.add(
        AuditLog(
            club_id=game.club_id,
            user_id=current.id,
            action="match_feedback.telegram_link_created",
            entity_type="match_feedback_contact",
            entity_id=contact.id,
            details={
                "game_id": game.id,
                "team_id": game.team_id,
                "reused_existing_contact": reused_existing,
            },
        )
    )
    db.commit()
    response = templates.TemplateResponse(
        request,
        "match_reports/telegram_contact_link.html",
        {
            "user": current,
            "game": game,
            "contact": contact,
            "deep_link": deep_link,
            "ttl_minutes": get_settings().telegram_link_ttl_minutes,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


@router.post("/games/{game_id}/match-report/contact/{contact_id}/preferences")
def save_contact_preferences(
    game_id: str,
    contact_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    preferred_provider: str = Form(),
    fallback_provider: str = Form(default=""),
    priority: int = Form(default=100),
    request_match_reports: bool = Form(default=False),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    game = _game(db, game_id, current, "edit_post")
    allowed = {"whatsapp", "telegram"}
    if preferred_provider not in allowed:
        raise HTTPException(422, "Bevorzugter Messenger ist ungültig")
    if fallback_provider and fallback_provider not in allowed:
        raise HTTPException(422, "Ersatz-Messenger ist ungültig")
    if fallback_provider == preferred_provider:
        raise HTTPException(422, "Bevorzugter und Ersatz-Messenger müssen verschieden sein")
    selected_providers = {preferred_provider, fallback_provider} - {""}
    unavailable = [
        provider
        for provider in selected_providers
        if not feedback_provider_enabled(db, game.club_id, provider)
    ]
    if unavailable:
        raise HTTPException(
            403,
            "Mindestens ein ausgewählter Messenger ist für diesen Verein nicht freigeschaltet",
        )
    contact = db.scalar(
        select(MatchFeedbackContact).where(
            MatchFeedbackContact.id == contact_id,
            MatchFeedbackContact.club_id == game.club_id,
            MatchFeedbackContact.team_id == game.team_id,
        )
    )
    if contact is None:
        raise HTTPException(404, "Rückfragekontakt nicht gefunden")
    contact.preferred_provider = preferred_provider
    contact.fallback_provider = fallback_provider or None
    contact.priority = max(0, min(priority, 10000))
    contact.request_match_reports = request_match_reports
    db.add(
        AuditLog(
            club_id=game.club_id,
            user_id=current.id,
            action="match_feedback.contact_preferences_changed",
            entity_type="match_feedback_contact",
            entity_id=contact.id,
            details={
                "preferred_provider": preferred_provider,
                "fallback_provider": fallback_provider or None,
                "priority": contact.priority,
                "request_match_reports": request_match_reports,
            },
        )
    )
    db.commit()
    return _redirect(game.id, "Messenger-Einstellungen gespeichert")


@router.post("/games/{game_id}/match-report/contact/{contact_id}/telegram/disconnect")
def disconnect_telegram_contact(
    game_id: str,
    contact_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    game = _game(db, game_id, current, "edit_post")
    contact = db.scalar(
        select(MatchFeedbackContact).where(
            MatchFeedbackContact.id == contact_id,
            MatchFeedbackContact.club_id == game.club_id,
            MatchFeedbackContact.team_id == game.team_id,
        )
    )
    if contact is None:
        raise HTTPException(404, "Rückfragekontakt nicht gefunden")
    endpoint = db.scalar(
        select(MatchFeedbackEndpoint).where(
            MatchFeedbackEndpoint.club_id == game.club_id,
            MatchFeedbackEndpoint.contact_id == contact.id,
            MatchFeedbackEndpoint.provider == "telegram",
        )
    )
    if endpoint is None:
        raise HTTPException(404, "Telegram-Verknüpfung nicht gefunden")
    endpoint.status = "disabled"
    endpoint.is_primary = False
    endpoint.disabled_at = datetime.now(timezone.utc)
    if contact.preferred_provider == "telegram":
        contact.preferred_provider = (
            contact.fallback_provider
            if contact.fallback_provider
            and feedback_provider_enabled(db, game.club_id, contact.fallback_provider)
            else None
        )
    if contact.fallback_provider == "telegram":
        contact.fallback_provider = None
    db.add(
        AuditLog(
            club_id=game.club_id,
            user_id=current.id,
            action="match_feedback.telegram_contact_disconnected",
            entity_type="match_feedback_contact",
            entity_id=contact.id,
            details={"game_id": game.id, "team_id": game.team_id},
        )
    )
    db.commit()
    return _redirect(game.id, "Telegram-Verknüpfung getrennt")


@router.post("/games/{game_id}/match-report/messenger-defaults")
def save_messenger_defaults(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    scope: str = Form(),
    preferred_provider: str = Form(default=""),
    fallback_provider: str = Form(default=""),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    game = _game(db, game_id, current, "edit_post")
    if scope not in {"team", "club"}:
        raise HTTPException(422, "Der Geltungsbereich ist ungültig")
    allowed = {"", "whatsapp", "telegram"}
    if preferred_provider not in allowed or fallback_provider not in allowed:
        raise HTTPException(422, "Messenger-Auswahl ist ungültig")
    if fallback_provider and not preferred_provider:
        raise HTTPException(422, "Ein Ersatz-Messenger benötigt einen bevorzugten Messenger")
    if preferred_provider and fallback_provider == preferred_provider:
        raise HTTPException(422, "Bevorzugter und Ersatz-Messenger müssen verschieden sein")
    selected = {preferred_provider, fallback_provider} - {""}
    if any(not feedback_provider_enabled(db, game.club_id, provider) for provider in selected):
        raise HTTPException(403, "Der ausgewählte Messenger ist nicht freigeschaltet")
    value = (
        {
            "preferred_provider": preferred_provider,
            "fallback_provider": fallback_provider or None,
        }
        if preferred_provider
        else None
    )
    if scope == "team":
        team = db.scalar(select(Team).where(Team.id == game.team_id, Team.club_id == game.club_id))
        if team is None:
            raise HTTPException(404, "Mannschaft nicht gefunden")
        settings = dict(team.rules or {})
        if value:
            settings["match_feedback_messenger"] = value
        else:
            settings.pop("match_feedback_messenger", None)
        team.rules = settings
        entity_type, entity_id = "team", team.id
    else:
        club = db.scalar(select(Club).where(Club.id == game.club_id))
        if club is None:
            raise HTTPException(404, "Verein nicht gefunden")
        settings = dict(club.technical_settings or {})
        if value:
            settings["match_feedback_messenger"] = value
        else:
            settings.pop("match_feedback_messenger", None)
        club.technical_settings = settings
        entity_type, entity_id = "club", club.id
    db.add(
        AuditLog(
            club_id=game.club_id,
            user_id=current.id,
            action="match_feedback.messenger_defaults_changed",
            entity_type=entity_type,
            entity_id=entity_id,
            details={
                "scope": scope,
                "preferred_provider": preferred_provider or None,
                "fallback_provider": fallback_provider or None,
            },
        )
    )
    db.commit()
    return _redirect(game.id, "Messenger-Standard gespeichert")


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
