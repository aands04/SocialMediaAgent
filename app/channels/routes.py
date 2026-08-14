from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.api import ChannelApiError, MetaGraphClient
from app.channels.capabilities import CHANNEL_LABELS
from app.channels.oauth import (
    activate_existing_whatsapp_phone,
    complete_facebook_selection,
    complete_whatsapp_onboarding,
    ensure_whatsapp_webhook_subscription,
    prepare_facebook_selection,
    start_channel_oauth,
    whatsapp_phone_is_registered,
)
from app.channels.service import assignment_map, channel_cards
from app.config import get_settings
from app.db import get_db
from app.limits.service import LimitExceeded, assert_resource_capacity
from app.meta.api import MetaApiClient, MetaApiError
from app.meta.oauth import disconnect as disconnect_instagram
from app.meta.oauth import start_oauth as start_instagram_oauth
from app.meta.security import MetaSecretError, TokenCipher
from app.models import (
    AuditLog,
    Club,
    InstagramConnection,
    InstagramPage,
    Role,
    SocialChannelConnection,
    SystemSetting,
    Team,
    TeamChannelAssignment,
    User,
    WhatsAppMessageTemplate,
    WhatsAppRecipient,
)
from app.tenancy.state import system_scope, tenant_scope
from app.web import berlin_datetime, check_csrf, csrf_token, current_user, require, require_admin

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["berlin"] = berlin_datetime
settings = get_settings()
templates.env.globals["environment"] = settings.environment


def _redirect(path: str, notice: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?notice={notice}", 303)


def _connection(db: Session, connection_id: str, channel_type: str | None = None):
    item = db.get(SocialChannelConnection, connection_id)
    if item is None or (channel_type and item.channel_type != channel_type):
        raise HTTPException(404, "Kanalverbindung nicht gefunden")
    return item


@router.get("/channels", response_class=HTMLResponse)
def channels_dashboard(
    request: Request,
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    require(current, db, "view")
    cards = channel_cards(db)
    db.commit()
    teams = list(
        db.scalars(select(Team).where(Team.archived_at.is_(None)).order_by(Team.display_name))
    )
    assignments = assignment_map(db)
    whatsapp_connections = [item["connection"] for item in cards["whatsapp"]]
    whatsapp_ids = [item.id for item in whatsapp_connections]
    recipients = (
        list(
            db.scalars(
                select(WhatsAppRecipient)
                .where(WhatsAppRecipient.channel_connection_id.in_(whatsapp_ids))
                .order_by(WhatsAppRecipient.display_name, WhatsAppRecipient.normalized_phone)
            )
        )
        if whatsapp_ids
        else []
    )
    message_templates = (
        list(
            db.scalars(
                select(WhatsAppMessageTemplate)
                .where(WhatsAppMessageTemplate.channel_connection_id.in_(whatsapp_ids))
                .order_by(WhatsAppMessageTemplate.name)
            )
        )
        if whatsapp_ids
        else []
    )
    emergency = db.get(SystemSetting, "emergency_stop")
    paused = bool(emergency and emergency.value.get("enabled")) or not bool(
        settings.global_publish_enabled
    )
    return templates.TemplateResponse(
        request,
        "channels.html",
        {
            "user": current,
            "csrf": csrf_token(request),
            "cards": cards,
            "teams": teams,
            "assignments": assignments,
            "recipients": recipients,
            "message_templates": message_templates,
            "paused": paused,
            "is_admin": current.role == Role.ADMIN,
            "facebook_available": settings.facebook_channel_enabled,
            "whatsapp_available": settings.whatsapp_channel_enabled,
            "title": "Social-Media-Kanäle",
        },
    )


@router.get("/channels/{channel_type}/setup", response_class=HTMLResponse)
def channel_setup(
    channel_type: str,
    request: Request,
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    require_admin(current)
    if channel_type not in {"instagram", "facebook", "whatsapp"}:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request,
        "channel_setup.html",
        {
            "user": current,
            "csrf": csrf_token(request),
            "channel_type": channel_type,
            "channel_label": CHANNEL_LABELS[channel_type],
            "meta_app_id": settings.meta_facebook_app_id,
            "whatsapp_configuration_id": settings.meta_whatsapp_configuration_id,
            "meta_graph_version": settings.meta_graph_version,
            "facebook_available": settings.facebook_channel_enabled,
            "whatsapp_available": settings.whatsapp_channel_enabled,
            "title": f"{CHANNEL_LABELS[channel_type]} einrichten",
        },
    )


@router.post("/channels/instagram/connect")
def instagram_connect(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    display_name: str = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Startet Instagram Login ohne technische Konto-ID im Vereinsformular."""
    check_csrf(request, csrf_token_value)
    require_admin(current)
    label = " ".join(display_name.split()).strip()
    if not 2 <= len(label) <= 120:
        raise HTTPException(422, "Bitte eine Bezeichnung mit 2 bis 120 Zeichen angeben")
    try:
        assert_resource_capacity(db, current.club_id, "instagram_pages")
    except LimitExceeded as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    club = db.get(Club, current.club_id)
    if club is None:
        raise HTTPException(409, "Der Verein ist nicht eindeutig zugeordnet")
    marker = uuid4().hex[:12]
    page = InstagramPage(
        internal_name=label,
        display_name=label,
        username=f"auswahl-{marker}",
        club=club.name,
        active=False,
        publishing_enabled=False,
        connection_status="unconfigured",
        defaults={"guided_setup": True},
    )
    db.add(page)
    db.flush()
    db.add(
        AuditLog(
            user_id=current.id,
            action="channel.instagram.guided_setup_started",
            entity_type="instagram_page",
            entity_id=page.id,
            details={"display_name": label},
        )
    )
    try:
        url = start_instagram_oauth(db, settings, page, current, MetaApiClient(settings))
    except MetaApiError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(url, 303)


@router.post("/channels/facebook/connect")
def facebook_connect(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    try:
        url = start_channel_oauth(
            db,
            settings,
            channel_type="facebook",
            user=current,
            api=MetaGraphClient(settings),
        )
    except ChannelApiError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(url, 303)


@router.get("/public/meta/channels/oauth/callback", response_class=HTMLResponse)
def channel_oauth_callback(
    request: Request,
    state: str = "",
    code: str = "",
    error: str = "",
    db: Session = Depends(get_db),
):
    if error or not state or not code:
        return templates.TemplateResponse(
            request,
            "channel_oauth_result.html",
            {
                "ok": False,
                "message": "Die Meta-Anmeldung wurde abgebrochen oder ist unvollständig.",
            },
            status_code=400,
        )
    try:
        from app.meta.security import secret_hash
        from app.models import SocialChannelOAuthState

        with system_scope("Öffentlichen Meta-OAuth-State einem Verein zuordnen"):
            record = db.scalar(
                select(SocialChannelOAuthState).where(
                    SocialChannelOAuthState.state_hash == secret_hash(state)
                )
            )
            club_id = record.club_id if record else None
        if not club_id:
            raise ChannelApiError("Verbindungsvorgang ist ungültig oder abgelaufen")
        with tenant_scope(club_id, "system:meta-oauth-callback"):
            record, pages = prepare_facebook_selection(
                db,
                settings,
                raw_state=state,
                code=code,
                api=MetaGraphClient(settings),
            )
    except (ChannelApiError, ValueError) as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "channel_oauth_result.html",
            {"ok": False, "message": str(exc)},
            status_code=400,
        )
    return templates.TemplateResponse(
        request,
        "channel_oauth_select.html",
        {"state": state, "pages": pages, "title": "Facebook-Seite auswählen"},
    )


@router.post("/public/meta/channels/oauth/select", response_class=HTMLResponse)
def channel_oauth_select(
    request: Request,
    state: str = Form(),
    page_id: str = Form(),
    db: Session = Depends(get_db),
):
    try:
        from app.meta.security import secret_hash
        from app.models import SocialChannelOAuthState

        with system_scope("Öffentliche Meta-Kontoauswahl einem Verein zuordnen"):
            record = db.scalar(
                select(SocialChannelOAuthState).where(
                    SocialChannelOAuthState.state_hash == secret_hash(state)
                )
            )
            club_id = record.club_id if record else None
        if not club_id:
            raise ChannelApiError("Verbindungsvorgang ist ungültig oder abgelaufen")
        with tenant_scope(club_id, "system:meta-oauth-selection"):
            item = complete_facebook_selection(
                db,
                settings,
                raw_state=state,
                page_id=page_id,
                api=MetaGraphClient(settings),
            )
    except (ChannelApiError, ValueError) as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "channel_oauth_result.html",
            {"ok": False, "message": str(exc)},
            status_code=400,
        )
    return templates.TemplateResponse(
        request,
        "channel_oauth_result.html",
        {
            "ok": True,
            "message": f"Facebook-Seite „{item.display_name}“ wurde verbunden.",
        },
    )


@router.post("/channels/whatsapp/complete")
def whatsapp_complete(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    code: str = Form(),
    waba_id: str = Form(),
    phone_number_id: str = Form(),
    registration_pin: str = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    try:
        complete_whatsapp_onboarding(
            db,
            settings,
            user=current,
            code=code,
            waba_id=waba_id,
            phone_number_id=phone_number_id,
            registration_pin=registration_pin,
            api=MetaGraphClient(settings),
        )
    except ChannelApiError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _redirect("/channels", "WhatsApp wurde verbunden")


@router.post("/channels/{connection_id}/whatsapp/register")
def register_existing_whatsapp_phone(
    connection_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    registration_pin: str = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Finish the one-time Cloud API registration for an older connection."""

    check_csrf(request, csrf_token_value)
    require_admin(current)
    item = _connection(db, connection_id, "whatsapp")
    try:
        activate_existing_whatsapp_phone(
            db,
            settings,
            user=current,
            connection=item,
            registration_pin=registration_pin,
            api=MetaGraphClient(settings),
        )
    except ChannelApiError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return _redirect("/channels", "WhatsApp-Telefonnummer wurde aktiviert")


@router.post("/channels/{connection_id}/check")
def check_channel_connection(
    connection_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    item = _connection(db, connection_id)
    if item.channel_type == "instagram":
        legacy_page = item.legacy_instagram_page_id
        if not legacy_page:
            raise HTTPException(409, "Instagram-Verbindung ist unvollständig")
        return RedirectResponse(f"/instagram/{legacy_page}/meta/check", 307)
    if item.channel_type == "whatsapp" and not whatsapp_phone_is_registered(item):
        item.status = "setup_required"
        item.last_error = (
            "Die WhatsApp-Telefonnummer muss einmalig für die Cloud API aktiviert werden"
        )
        item.last_check_at = datetime.now(timezone.utc)
        db.commit()
        raise HTTPException(409, item.last_error)
    webhook_repaired = False
    try:
        token = TokenCipher(settings.meta_token_encryption_key).decrypt(item.encrypted_token)
        api = MetaGraphClient(settings)
        if item.channel_type == "facebook":
            api.page_profile(page_id=item.external_account_id or "", access_token=token)
        elif item.channel_type == "whatsapp":
            api.whatsapp_phone(phone_number_id=item.phone_number_id or "", access_token=token)
            webhook_repaired = ensure_whatsapp_webhook_subscription(
                settings,
                connection=item,
                access_token=token,
                api=api,
            )
        item.status = "connected"
        item.last_check_at = datetime.now(timezone.utc)
        item.last_success_at = item.last_check_at
        item.last_error = None
    except (ChannelApiError, MetaSecretError, ValueError) as exc:
        item.status = "disrupted"
        item.last_error = str(exc)[:500]
        item.last_check_at = datetime.now(timezone.utc)
        db.commit()
        raise HTTPException(409, "Die Verbindung konnte nicht bestätigt werden") from exc
    db.add(
        AuditLog(
            user_id=current.id,
            action=f"channel.{item.channel_type}.checked",
            entity_type="social_channel_connection",
            entity_id=item.id,
            details={
                "result": "connected",
                "webhook_subscription_repaired": webhook_repaired,
            },
        )
    )
    db.commit()
    notice = f"{CHANNEL_LABELS[item.channel_type]} ist bereit"
    if item.channel_type == "whatsapp" and webhook_repaired:
        notice = "WhatsApp ist bereit; die Webhook-Verbindung wurde repariert"
    return _redirect("/channels", notice)


@router.post("/channels/{connection_id}/settings")
def save_channel_settings(
    connection_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    publishing_enabled: bool = Form(default=False),
    automatic_delivery_enabled: bool = Form(default=False),
    automatic_confirmation: str = Form(default=""),
    announcements: bool = Form(default=False),
    results: bool = Form(default=False),
    stories: bool = Form(default=False),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    item = _connection(db, connection_id)
    if (
        item.channel_type == "whatsapp"
        and not whatsapp_phone_is_registered(item)
        and (publishing_enabled or automatic_delivery_enabled)
    ):
        raise HTTPException(
            409,
            "Die WhatsApp-Telefonnummer muss zuerst für die Cloud API aktiviert werden",
        )
    if item.status != "connected" and (publishing_enabled or automatic_delivery_enabled):
        raise HTTPException(409, "Die Verbindung muss zuerst erfolgreich geprüft werden")
    if automatic_delivery_enabled and not publishing_enabled:
        raise HTTPException(409, "Aktiviere zuerst den Kanal")
    if settings.environment == "meta-test" and automatic_delivery_enabled:
        raise HTTPException(409, "Automatische Auslieferung ist im Meta-Test gesperrt")
    if settings.environment == "production" and automatic_delivery_enabled:
        if automatic_confirmation != "AUTOMATISCH VERÖFFENTLICHEN":
            raise HTTPException(
                409,
                "Bestätigung für die automatische Veröffentlichung oder den Versand fehlt",
            )
        if not item.last_check_at:
            raise HTTPException(409, "Die Verbindung wurde noch nicht aktuell geprüft")

    if item.channel_type == "instagram":
        page = db.get(InstagramPage, item.legacy_instagram_page_id)
        legacy = db.scalar(
            select(InstagramConnection).where(
                InstagramConnection.instagram_page_id == item.legacy_instagram_page_id
            )
        )
        if page is None or legacy is None:
            raise HTTPException(409, "Instagram-Verbindung ist unvollständig")
        required_scopes = {
            "instagram_business_basic",
            "instagram_business_content_publish",
        }
        if publishing_enabled and legacy.account_type != "BUSINESS":
            raise HTTPException(409, "Dieses Instagram-Konto unterstützt Publishing nicht")
        if publishing_enabled and not required_scopes.issubset(set(legacy.scopes or [])):
            raise HTTPException(409, "Benötigte Instagram-Berechtigungen fehlen")
        page.publishing_enabled = publishing_enabled
        page.allowed_types = {"feed": publishing_enabled, "story": stories}
        page.automatic_publishing_enabled = (
            automatic_delivery_enabled if settings.environment == "production" else False
        )
        if page.automatic_publishing_enabled:
            page.automatic_publishing_confirmed_by = current.id
            page.automatic_publishing_confirmed_at = datetime.now(timezone.utc)
        else:
            page.automatic_publishing_confirmed_by = None
            page.automatic_publishing_confirmed_at = None
        page.active = True
    item.publishing_enabled = publishing_enabled
    item.automatic_delivery_enabled = automatic_delivery_enabled
    item.settings = {
        **(item.settings or {}),
        "announcements_enabled": announcements,
        "results_enabled": results,
        "feed_enabled": publishing_enabled,
        "story_enabled": stories if item.channel_type == "instagram" else False,
    }
    db.add(
        AuditLog(
            user_id=current.id,
            action=f"channel.{item.channel_type}.settings_changed",
            entity_type="social_channel_connection",
            entity_id=item.id,
            details={
                "publishing_enabled": publishing_enabled,
                "automatic_delivery_enabled": automatic_delivery_enabled,
                "announcements": announcements,
                "results": results,
                "stories": stories if item.channel_type == "instagram" else False,
            },
        )
    )
    db.commit()
    return _redirect("/channels", "Kanaleinstellungen gespeichert")


@router.post("/channels/{connection_id}/disconnect")
def disconnect_channel(
    connection_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    confirmation: str = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    item = _connection(db, connection_id)
    if confirmation != "VERBINDUNG TRENNEN":
        raise HTTPException(422, "Bestätigung stimmt nicht überein")
    if item.channel_type == "instagram":
        page = db.get(InstagramPage, item.legacy_instagram_page_id)
        legacy = db.scalar(
            select(InstagramConnection).where(
                InstagramConnection.instagram_page_id == item.legacy_instagram_page_id
            )
        )
        if page is None or legacy is None:
            raise HTTPException(409, "Instagram-Verbindung ist unvollständig")
        disconnect_instagram(db, legacy, page, current)

    item.status = "disconnected"
    item.active = False
    item.publishing_enabled = False
    item.automatic_delivery_enabled = False
    item.encrypted_token = None
    item.disconnected_at = datetime.now(timezone.utc)
    for assignment in db.scalars(
        select(TeamChannelAssignment).where(TeamChannelAssignment.channel_connection_id == item.id)
    ):
        assignment.enabled = False
    db.add(
        AuditLog(
            user_id=current.id,
            action=f"channel.{item.channel_type}.disconnected",
            entity_type="social_channel_connection",
            entity_id=item.id,
            details={},
        )
    )
    db.commit()
    return _redirect("/channels", "Verbindung getrennt")


@router.post("/channels/{connection_id}/teams/{team_id}")
def save_team_channel_assignment(
    connection_id: str,
    team_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    enabled: bool = Form(default=False),
    announcement_enabled: bool = Form(default=False),
    result_enabled: bool = Form(default=False),
    story_enabled: bool = Form(default=False),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    connection = _connection(db, connection_id)
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "Mannschaft nicht gefunden")
    item = db.scalar(
        select(TeamChannelAssignment).where(
            TeamChannelAssignment.team_id == team.id,
            TeamChannelAssignment.channel_connection_id == connection.id,
        )
    )
    if item is None:
        item = TeamChannelAssignment(
            team_id=team.id,
            channel_connection_id=connection.id,
        )
        db.add(item)
    item.enabled = enabled
    item.announcement_enabled = enabled and announcement_enabled
    item.result_enabled = enabled and result_enabled
    item.story_enabled = enabled and story_enabled and connection.channel_type == "instagram"
    db.add(
        AuditLog(
            user_id=current.id,
            team_id=team.id,
            action="channel.team_assignment_changed",
            entity_type="team_channel_assignment",
            entity_id=item.id,
            details={"channel_type": connection.channel_type, "enabled": enabled},
        )
    )
    db.commit()
    return _redirect("/channels", "Mannschaftszuordnung gespeichert")


def _normalize_phone(value: str) -> str:
    normalized = re.sub(r"[^0-9+]", "", value.strip())
    if normalized.startswith("00"):
        normalized = "+" + normalized[2:]
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", normalized):
        raise HTTPException(422, "Telefonnummer muss im internationalen Format vorliegen")
    return normalized


@router.post("/channels/{connection_id}/whatsapp/recipients")
def add_whatsapp_recipient(
    connection_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    phone: str = Form(),
    display_name: str = Form(default=""),
    opt_in_source: str = Form(),
    consent_confirmed: bool = Form(default=False),
    announcements: bool = Form(default=False),
    results: bool = Form(default=False),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    connection = _connection(db, connection_id, "whatsapp")
    if not consent_confirmed or not opt_in_source.strip():
        raise HTTPException(422, "Ein nachvollziehbarer Opt-in-Nachweis ist erforderlich")
    normalized = _normalize_phone(phone)
    item = db.scalar(
        select(WhatsAppRecipient).where(
            WhatsAppRecipient.channel_connection_id == connection.id,
            WhatsAppRecipient.normalized_phone == normalized,
        )
    )
    if item is None:
        item = WhatsAppRecipient(
            channel_connection_id=connection.id,
            normalized_phone=normalized,
        )
        db.add(item)
    item.display_name = display_name.strip() or None
    item.opt_in_status = "confirmed"
    item.opt_in_at = datetime.now(timezone.utc)
    item.opt_in_source = opt_in_source.strip()[:160]
    item.opt_out_at = None
    item.active = True
    item.preferred_message_types = [
        value
        for value, selected in (("announcement", announcements), ("result", results))
        if selected
    ]
    db.add(
        AuditLog(
            user_id=current.id,
            action="whatsapp.recipient.opted_in",
            entity_type="whatsapp_recipient",
            entity_id=item.id,
            details={"source": item.opt_in_source},
        )
    )
    db.commit()
    return _redirect("/channels", "WhatsApp-Empfänger gespeichert")


@router.post("/channels/whatsapp/recipients/{recipient_id}/opt-out")
def opt_out_whatsapp_recipient(
    recipient_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    item = db.get(WhatsAppRecipient, recipient_id)
    if item is None:
        raise HTTPException(404)
    item.opt_in_status = "revoked"
    item.opt_out_at = datetime.now(timezone.utc)
    item.active = False
    db.add(
        AuditLog(
            user_id=current.id,
            action="whatsapp.recipient.opted_out",
            entity_type="whatsapp_recipient",
            entity_id=item.id,
            details={},
        )
    )
    db.commit()
    return _redirect("/channels", "Empfänger wurde abgemeldet")


@router.post("/channels/whatsapp/templates/{template_id}")
def configure_whatsapp_template(
    template_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    message_type: str = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    if message_type not in {"general", "announcement", "result", "live_event"}:
        raise HTTPException(422, "Unbekannter Nachrichtentyp")
    item = db.get(WhatsAppMessageTemplate, template_id)
    if item is None:
        raise HTTPException(404, "WhatsApp-Vorlage nicht gefunden")
    item.message_type = message_type
    db.add(
        AuditLog(
            user_id=current.id,
            action="whatsapp.template.assignment_changed",
            entity_type="whatsapp_message_template",
            entity_id=item.id,
            details={"message_type": message_type},
        )
    )
    db.commit()
    return _redirect("/channels", "WhatsApp-Vorlage zugeordnet")
