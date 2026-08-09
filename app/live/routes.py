from __future__ import annotations

import hashlib
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.service import allowed
from app.config import get_settings
from app.db import get_db
from app.live.parser import EVENT_TYPES, ParsedMatchEvent, parse_match_event
from app.live.service import (
    LiveEventError,
    confirm_event,
    create_match_event,
    get_or_create_state,
    normalize_phone,
    reporter_can_access_team,
)
from app.models import (
    AccountType,
    AuditLog,
    Club,
    Game,
    LiveEventDelivery,
    LiveEventRule,
    LiveGameState,
    LiveReporter,
    LiveReporterTeam,
    MatchEvent,
    SocialChannelConnection,
    Team,
    User,
    WhatsAppAudience,
    WhatsAppAudienceRecipient,
    WhatsAppRecipient,
    now,
)
from app.web import berlin_datetime, check_csrf, csrf_token, current_user, require, require_admin

router = APIRouter(prefix="/live")
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["berlin"] = berlin_datetime
settings = get_settings()

EVENT_LABELS = {
    "kickoff": "Anpfiff",
    "goal": "Tor für uns",
    "opponent_goal": "Gegentor",
    "own_goal": "Eigentor",
    "penalty_scored": "Elfmeter verwandelt",
    "penalty_missed": "Elfmeter vergeben",
    "yellow_card": "Gelbe Karte",
    "second_yellow_card": "Gelb-Rote Karte",
    "red_card": "Rote Karte",
    "substitution": "Auswechslung",
    "halftime": "Halbzeit",
    "second_half": "Zweite Halbzeit",
    "fulltime": "Abpfiff",
    "interruption": "Unterbrechung",
    "resume": "Fortsetzung",
    "abandoned": "Spielabbruch",
    "comment": "Kommentar",
    "score_correction": "Spielstand korrigieren",
    "event_correction": "Ereignis korrigieren",
}


def _club_user(current: User) -> None:
    if not settings.live_center_enabled:
        raise HTTPException(404, "Live Center ist nicht aktiviert")
    if current.account_type != AccountType.CLUB_USER or not current.club_id:
        raise HTTPException(403, "Live Center ist nur im Vereinskontext verfügbar")


def _visible_teams(db: Session, current: User) -> list[Team]:
    teams = list(
        db.scalars(select(Team).where(Team.archived_at.is_(None)).order_by(Team.display_name))
    )
    return [team for team in teams if allowed(db, current, "view", team.id)]


def _page_context(
    request: Request,
    current: User,
    db: Session,
    *,
    simulated: ParsedMatchEvent | None = None,
) -> dict:
    teams = _visible_teams(db, current)
    team_ids = [team.id for team in teams]
    current_time = now()
    games = (
        list(
            db.scalars(
                select(Game)
                .where(
                    Game.team_id.in_(team_ids),
                    Game.kickoff.between(
                        current_time - timedelta(days=2), current_time + timedelta(days=7)
                    ),
                    Game.status.not_in({"cancelled", "postponed"}),
                )
                .order_by(Game.kickoff)
            )
        )
        if team_ids
        else []
    )
    states = (
        {
            item.game_id: item
            for item in db.scalars(
                select(LiveGameState).where(LiveGameState.game_id.in_([g.id for g in games]))
            )
        }
        if games
        else {}
    )
    events = (
        list(
            db.scalars(
                select(MatchEvent)
                .where(MatchEvent.game_id.in_([g.id for g in games]))
                .order_by(MatchEvent.occurred_at.desc())
            )
        )
        if games
        else []
    )
    events_by_game: dict[str, list[MatchEvent]] = {game.id: [] for game in games}
    for event in events:
        events_by_game.setdefault(event.game_id, []).append(event)
    deliveries = (
        list(
            db.scalars(
                select(LiveEventDelivery)
                .where(LiveEventDelivery.event_id.in_([event.id for event in events]))
                .order_by(LiveEventDelivery.created_at.desc())
            )
        )
        if events
        else []
    )
    deliveries_by_event: dict[str, list[LiveEventDelivery]] = {}
    for item in deliveries:
        deliveries_by_event.setdefault(item.event_id, []).append(item)
    reporters = list(db.scalars(select(LiveReporter).order_by(LiveReporter.display_name)))
    reporter_teams = list(db.scalars(select(LiveReporterTeam)))
    reporter_team_ids: dict[str, set[str]] = {}
    for item in reporter_teams:
        reporter_team_ids.setdefault(item.reporter_id, set()).add(item.team_id)
    rules = (
        list(
            db.scalars(
                select(LiveEventRule)
                .where(LiveEventRule.team_id.in_(team_ids))
                .order_by(LiveEventRule.team_id, LiveEventRule.event_type)
            )
        )
        if team_ids
        else []
    )
    whatsapp_connections = list(
        db.scalars(
            select(SocialChannelConnection).where(
                SocialChannelConnection.channel_type == "whatsapp",
                SocialChannelConnection.active.is_(True),
            )
        )
    )
    audiences = list(
        db.scalars(
            select(WhatsAppAudience)
            .where(WhatsAppAudience.active.is_(True))
            .order_by(WhatsAppAudience.name)
        )
    )
    whatsapp_recipients = list(
        db.scalars(
            select(WhatsAppRecipient)
            .where(
                WhatsAppRecipient.active.is_(True),
                WhatsAppRecipient.opt_in_status == "confirmed",
            )
            .order_by(WhatsAppRecipient.display_name, WhatsAppRecipient.normalized_phone)
        )
    )
    return {
        "request": request,
        "user": current,
        "csrf": csrf_token(request),
        "title": "Live Center",
        "games": games,
        "teams": teams,
        "teams_by_id": {team.id: team for team in teams},
        "states": states,
        "events_by_game": events_by_game,
        "deliveries_by_event": deliveries_by_event,
        "reporters": reporters,
        "reporter_team_ids": reporter_team_ids,
        "rules": rules,
        "whatsapp_connections": whatsapp_connections,
        "whatsapp_audiences": audiences,
        "whatsapp_audiences_by_id": {item.id: item for item in audiences},
        "whatsapp_recipients": whatsapp_recipients,
        "event_labels": EVENT_LABELS,
        "simulated": simulated,
        "live_paused": bool(
            (db.get(Club, current.club_id).technical_settings or {}).get("live_center_paused")
        ),
    }


def _redirect(message: str) -> RedirectResponse:
    return RedirectResponse(f"/live?notice={message}", status_code=303)


@router.get("", response_class=HTMLResponse)
def live_center(
    request: Request,
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _club_user(current)
    require(current, db, "view")
    return templates.TemplateResponse(request, "live.html", _page_context(request, current, db))


@router.post("/games/{game_id}/events")
def add_event(
    game_id: str,
    request: Request,
    csrf_value: str = Form(alias="csrf_token"),
    event_type: str = Form(),
    minute: int | None = Form(default=None),
    stoppage_minute: int | None = Form(default=None),
    home_score: int | None = Form(default=None),
    away_score: int | None = Form(default=None),
    player_name: str | None = Form(default=None),
    related_player_name: str | None = Form(default=None),
    comment: str | None = Form(default=None),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_value)
    _club_user(current)
    game = db.get(Game, game_id)
    if game is None:
        raise HTTPException(404)
    require(current, db, "generate", game.team_id)
    if event_type not in EVENT_TYPES:
        raise HTTPException(422, "Unbekannter Ereignistyp")
    parsed = ParsedMatchEvent(
        event_type=event_type,
        minute=minute,
        stoppage_minute=stoppage_minute,
        home_score_after=home_score,
        away_score_after=away_score,
        player_name=(player_name or "").strip()[:160] or None,
        related_player_name=(related_player_name or "").strip()[:160] or None,
        comment=(comment or "").strip()[:500] or None,
        parser="dashboard",
    )
    try:
        event = create_match_event(
            db,
            game=game,
            parsed=parsed,
            provider="dashboard",
            idempotency_key=f"dashboard:{game.id}:{current.id}:{hashlib.sha256(str(parsed).encode()).hexdigest()[:24]}:{now().isoformat()}",
            created_by=current.id,
            force_confirmed=allowed(db, current, "approve", game.team_id),
        )
    except LiveEventError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return _redirect(f"{EVENT_LABELS.get(event.event_type, 'Ereignis')} gespeichert")


@router.post("/events/{event_id}/confirm")
def approve_event(
    event_id: str,
    request: Request,
    csrf_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_value)
    _club_user(current)
    event = db.get(MatchEvent, event_id)
    if event is None:
        raise HTTPException(404)
    require(current, db, "approve", event.team_id)
    try:
        confirm_event(db, event, user_id=current.id)
    except LiveEventError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    return _redirect("Live-Ereignis bestätigt")


@router.post("/events/{event_id}/reject")
def reject_event(
    event_id: str,
    request: Request,
    csrf_value: str = Form(alias="csrf_token"),
    reason: str = Form(default=""),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_value)
    _club_user(current)
    event = db.get(MatchEvent, event_id)
    if event is None:
        raise HTTPException(404)
    require(current, db, "approve", event.team_id)
    if event.status != "pending":
        raise HTTPException(409, "Ereignis ist nicht mehr zur Prüfung offen")
    event.status = "rejected"
    event.needs_confirmation = False
    event.metadata_json = {
        **(event.metadata_json or {}),
        "rejection_reason": reason.strip()[:500] or None,
    }
    db.add(
        AuditLog(
            user_id=current.id,
            team_id=event.team_id,
            action="live.event_rejected",
            entity_type="match_event",
            entity_id=event.id,
            details={"reason": reason.strip()[:500] or None},
        )
    )
    db.commit()
    return _redirect("Live-Ereignis abgelehnt")


@router.post("/deliveries/{delivery_id}/decision")
def decide_delivery(
    delivery_id: str,
    request: Request,
    csrf_value: str = Form(alias="csrf_token"),
    decision: str = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_value)
    _club_user(current)
    delivery = db.get(LiveEventDelivery, delivery_id)
    if delivery is None:
        raise HTTPException(404)
    event = db.get(MatchEvent, delivery.event_id)
    if event is None:
        raise HTTPException(404)
    require(current, db, "approve", event.team_id)
    if delivery.status != "awaiting_approval":
        raise HTTPException(409, "Auslieferung ist nicht mehr zur Freigabe offen")
    if decision not in {"approve", "reject"}:
        raise HTTPException(422, "Unbekannte Entscheidung")
    delivery.status = "queued" if decision == "approve" else "cancelled"
    delivery.last_error = None if decision == "approve" else "Manuell abgelehnt"
    db.add(
        AuditLog(
            user_id=current.id,
            team_id=event.team_id,
            action=f"live.delivery_{'approved' if decision == 'approve' else 'rejected'}",
            entity_type="live_event_delivery",
            entity_id=delivery.id,
            details={"channel_type": delivery.channel_type},
        )
    )
    db.commit()
    return _redirect(
        "Live-Auslieferung freigegeben" if decision == "approve" else "Live-Auslieferung abgelehnt"
    )


@router.post("/reporters")
def save_reporter(
    request: Request,
    csrf_value: str = Form(alias="csrf_token"),
    display_name: str = Form(),
    normalized_phone: str = Form(),
    channel_connection_id: str = Form(),
    team_ids: list[str] = Form(default=[]),
    all_teams: bool = Form(default=False),
    trusted_auto_confirm: bool = Form(default=False),
    may_correct: bool = Form(default=False),
    allowed_event_types: list[str] = Form(default=[]),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_value)
    _club_user(current)
    require_admin(current)
    connection = db.get(SocialChannelConnection, channel_connection_id)
    if connection is None or connection.channel_type != "whatsapp":
        raise HTTPException(422, "WhatsApp-Verbindung gehört nicht zum aktuellen Verein")
    try:
        phone = normalize_phone(normalized_phone)
    except LiveEventError as exc:
        raise HTTPException(422, str(exc)) from exc
    reporter = LiveReporter(
        channel_connection_id=connection.id,
        normalized_phone=phone,
        display_name=display_name.strip()[:160],
        all_teams=all_teams,
        trusted_auto_confirm=trusted_auto_confirm,
        may_correct=may_correct,
        allowed_event_types=[
            value for value in dict.fromkeys(allowed_event_types) if value in EVENT_TYPES
        ],
        active=True,
    )
    if not reporter.display_name:
        raise HTTPException(422, "Anzeigename fehlt")
    db.add(reporter)
    db.flush()
    if not all_teams:
        for team_id in set(team_ids):
            if db.get(Team, team_id) is None:
                raise HTTPException(422, "Mannschaft gehört nicht zum aktuellen Verein")
            db.add(LiveReporterTeam(reporter_id=reporter.id, team_id=team_id))
    db.add(
        AuditLog(
            user_id=current.id,
            action="live.reporter_created",
            entity_type="live_reporter",
            entity_id=reporter.id,
            details={"all_teams": all_teams, "team_count": len(set(team_ids))},
        )
    )
    db.commit()
    return _redirect("Reporter angelegt")


@router.post("/reporters/{reporter_id}/active")
def set_reporter_active(
    reporter_id: str,
    request: Request,
    csrf_value: str = Form(alias="csrf_token"),
    active: bool = Form(default=False),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_value)
    _club_user(current)
    require_admin(current)
    reporter = db.get(LiveReporter, reporter_id)
    if reporter is None:
        raise HTTPException(404)
    reporter.active = active
    if not active:
        reporter.active_game_id = None
        reporter.active_game_expires_at = None
    db.add(
        AuditLog(
            user_id=current.id,
            action="live.reporter_status_changed",
            entity_type="live_reporter",
            entity_id=reporter.id,
            details={"active": active},
        )
    )
    db.commit()
    return _redirect("Reporter aktiviert" if active else "Reporter deaktiviert")


@router.post("/reporters/{reporter_id}/active-game")
def assign_active_game(
    reporter_id: str,
    request: Request,
    csrf_value: str = Form(alias="csrf_token"),
    game_id: str = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_value)
    _club_user(current)
    require_admin(current)
    reporter = db.get(LiveReporter, reporter_id)
    game = db.get(Game, game_id)
    if reporter is None or game is None:
        raise HTTPException(404)
    if not reporter_can_access_team(db, reporter, game.team_id):
        raise HTTPException(403, "Reporter besitzt kein Recht für diese Mannschaft")
    reporter.active_game_id = game.id
    reporter.active_game_expires_at = now() + timedelta(
        minutes=settings.live_event_active_game_ttl_minutes
    )
    db.add(
        AuditLog(
            user_id=current.id,
            team_id=game.team_id,
            action="live.reporter_active_game_changed",
            entity_type="live_reporter",
            entity_id=reporter.id,
            details={
                "game_id": game.id,
                "expires_at": reporter.active_game_expires_at.isoformat(),
            },
        )
    )
    db.commit()
    return _redirect("Aktives Reporter-Spiel gespeichert")


@router.post("/games/{game_id}/pause")
def pause_game_live_publishing(
    game_id: str,
    request: Request,
    csrf_value: str = Form(alias="csrf_token"),
    paused: bool = Form(default=False),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_value)
    _club_user(current)
    game = db.get(Game, game_id)
    if game is None:
        raise HTTPException(404)
    require(current, db, "approve", game.team_id)
    state = get_or_create_state(db, game)
    state.live_publishing_paused = paused
    db.add(
        AuditLog(
            user_id=current.id,
            team_id=game.team_id,
            action="live.game_pause_changed",
            entity_type="game",
            entity_id=game.id,
            details={"paused": paused},
        )
    )
    db.commit()
    return _redirect(
        "Live-Veröffentlichungen für das Spiel pausiert"
        if paused
        else "Live-Veröffentlichungen für das Spiel fortgesetzt"
    )


@router.post("/rules")
def save_rule(
    request: Request,
    csrf_value: str = Form(alias="csrf_token"),
    team_id: str = Form(),
    event_type: str = Form(),
    delivery_mode: str = Form(),
    audience_type: str = Form(default="dashboard"),
    whatsapp_audience_id: str = Form(default=""),
    channel_types: list[str] = Form(default=[]),
    enabled: bool = Form(default=False),
    require_confirmation: bool = Form(default=False),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_value)
    _club_user(current)
    require_admin(current)
    if db.get(Team, team_id) is None:
        raise HTTPException(404)
    if event_type not in EVENT_TYPES:
        raise HTTPException(422, "Unbekannter Ereignistyp")
    if delivery_mode not in {"off", "manual", "automatic"}:
        raise HTTPException(422, "Unbekannter Auslieferungsmodus")
    if audience_type not in {"dashboard", "opt_in_recipients", "eligible_group"}:
        raise HTTPException(422, "Unbekannte Zielgruppe")
    safe_channels = [
        value
        for value in dict.fromkeys(channel_types)
        if value in {"dashboard", "instagram", "facebook", "whatsapp"}
    ]
    audience = None
    if "whatsapp" in safe_channels:
        audience = db.get(WhatsAppAudience, whatsapp_audience_id)
        if audience is None or not audience.active:
            raise HTTPException(422, "Für WhatsApp muss eine aktive Zielgruppe gewählt werden")
        audience_type = (
            "eligible_group" if audience.audience_type == "group" else "opt_in_recipients"
        )
    else:
        whatsapp_audience_id = ""
        audience_type = "dashboard"
    rule = db.scalar(
        select(LiveEventRule).where(
            LiveEventRule.team_id == team_id,
            LiveEventRule.event_type == event_type,
        )
    )
    if rule is None:
        rule = LiveEventRule(team_id=team_id, event_type=event_type)
        db.add(rule)
    rule.delivery_mode = delivery_mode
    rule.audience_type = audience_type
    rule.whatsapp_audience_id = audience.id if audience else None
    rule.channel_types = safe_channels or ["dashboard"]
    rule.enabled = enabled
    rule.require_confirmation = require_confirmation
    db.add(
        AuditLog(
            user_id=current.id,
            team_id=team_id,
            action="live.rule_saved",
            entity_type="live_event_rule",
            entity_id=rule.id,
            details={
                "event_type": event_type,
                "delivery_mode": delivery_mode,
                "audience_type": audience_type,
                "whatsapp_audience_id": rule.whatsapp_audience_id,
                "channel_types": rule.channel_types,
            },
        )
    )
    db.commit()
    return _redirect("Live-Regel gespeichert")


@router.post("/whatsapp-audiences")
def create_whatsapp_audience(
    request: Request,
    csrf_value: str = Form(alias="csrf_token"),
    channel_connection_id: str = Form(),
    name: str = Form(),
    audience_type: str = Form(),
    external_group_id: str = Form(default=""),
    recipient_ids: list[str] = Form(default=[]),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_value)
    _club_user(current)
    require_admin(current)
    connection = db.get(SocialChannelConnection, channel_connection_id)
    if connection is None or connection.channel_type != "whatsapp":
        raise HTTPException(422, "WhatsApp-Verbindung gehört nicht zum aktuellen Verein")
    if audience_type not in {"group", "recipient_list"}:
        raise HTTPException(422, "Unbekannter Zielgruppentyp")
    clean_name = name.strip()[:160]
    if not clean_name:
        raise HTTPException(422, "Name der Zielgruppe fehlt")
    if db.scalar(
        select(WhatsAppAudience.id).where(
            WhatsAppAudience.channel_connection_id == connection.id,
            WhatsAppAudience.name == clean_name,
        )
    ):
        raise HTTPException(409, "Eine Zielgruppe mit diesem Namen existiert bereits")
    external_group_id = external_group_id.strip()[:200]
    eligibility = "unknown"
    if audience_type == "group":
        if "groups" not in (connection.capabilities or []) or not external_group_id:
            raise HTTPException(
                422,
                "Eine offizielle Gruppe kann nur bei bestätigter Groups-API-Eignung angelegt werden",
            )
        eligibility = "available"
    audience = WhatsAppAudience(
        channel_connection_id=connection.id,
        name=clean_name,
        audience_type=audience_type,
        external_group_id=external_group_id or None,
        eligibility_status=eligibility,
        active=True,
    )
    db.add(audience)
    db.flush()
    if audience_type == "recipient_list":
        if not recipient_ids:
            raise HTTPException(422, "Empfängerliste benötigt mindestens einen aktiven Opt-in")
        for recipient_id in dict.fromkeys(recipient_ids):
            recipient = db.get(WhatsAppRecipient, recipient_id)
            if (
                recipient is None
                or recipient.channel_connection_id != connection.id
                or not recipient.active
                or recipient.opt_in_status != "confirmed"
            ):
                raise HTTPException(422, "Empfänger ist nicht für diese Verbindung freigegeben")
            db.add(
                WhatsAppAudienceRecipient(
                    audience_id=audience.id,
                    recipient_id=recipient.id,
                )
            )
    db.add(
        AuditLog(
            user_id=current.id,
            action="live.whatsapp_audience_created",
            entity_type="whatsapp_audience",
            entity_id=audience.id,
            details={
                "audience_type": audience_type,
                "recipient_count": len(set(recipient_ids))
                if audience_type == "recipient_list"
                else 0,
            },
        )
    )
    db.commit()
    return _redirect("WhatsApp-Zielgruppe gespeichert")


@router.post("/pause")
def pause_live_center(
    request: Request,
    csrf_value: str = Form(alias="csrf_token"),
    paused: bool = Form(default=False),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_value)
    _club_user(current)
    require_admin(current)
    club = db.get(Club, current.club_id)
    if club is None:
        raise HTTPException(404)
    club.technical_settings = {**(club.technical_settings or {}), "live_center_paused": paused}
    db.add(
        AuditLog(
            user_id=current.id,
            action="live.pause_changed",
            entity_type="club",
            entity_id=club.id,
            details={"paused": paused},
        )
    )
    db.commit()
    return _redirect("Live-Verteilung pausiert" if paused else "Live-Verteilung fortgesetzt")


@router.post("/simulate", response_class=HTMLResponse)
def simulate_event(
    request: Request,
    csrf_value: str = Form(alias="csrf_token"),
    message: str = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_value)
    _club_user(current)
    require(current, db, "generate")
    parsed = parse_match_event(message)
    return templates.TemplateResponse(
        request,
        "live.html",
        _page_context(request, current, db, simulated=parsed),
    )
