from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.api import (
    FACEBOOK_REQUIRED_SCOPES,
    WHATSAPP_REQUIRED_SCOPES,
    ChannelApiError,
    MetaGraphClient,
)
from app.channels.capabilities import CHANNEL_CAPABILITIES
from app.config import Settings
from app.meta.security import MetaSecretError, TokenCipher, random_oauth_state, secret_hash
from app.models import (
    AuditLog,
    SocialChannelConnection,
    SocialChannelOAuthState,
    User,
    WhatsAppMessageTemplate,
)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def assert_channel_enabled(settings: Settings, channel_type: str) -> None:
    if settings.environment not in {"production", "meta-test"}:
        raise ChannelApiError("Meta-Verbindungen sind in dieser Umgebung deaktiviert")
    if not settings.meta_production_enabled and settings.environment == "production":
        raise ChannelApiError("Meta-Integration ist nicht freigegeben")
    if channel_type == "facebook" and not settings.facebook_channel_enabled:
        raise ChannelApiError("Facebook ist plattformweit vorübergehend pausiert")
    if channel_type == "whatsapp" and not settings.whatsapp_channel_enabled:
        raise ChannelApiError("WhatsApp ist plattformweit vorübergehend pausiert")


def whatsapp_phone_is_registered(connection: SocialChannelConnection) -> bool:
    """Return the locally recorded result of Meta's registration endpoint."""

    return bool((connection.settings or {}).get("phone_registered"))


def _whatsapp_app_is_subscribed(items: list[dict], app_id: str) -> bool:
    for item in items:
        if str(item.get("id") or "") == app_id:
            return True
        api_data = item.get("whatsapp_business_api_data")
        if isinstance(api_data, dict) and str(api_data.get("id") or "") == app_id:
            return True
    return False


def ensure_whatsapp_webhook_subscription(
    settings: Settings,
    *,
    connection: SocialChannelConnection,
    access_token: str,
    api: MetaGraphClient,
) -> bool:
    """Verify and, if necessary, repair the app subscription for a WABA.

    App-level webhook tests can succeed even while real messages are not
    delivered. Meta additionally requires the concrete WhatsApp Business
    Account to be subscribed to the current app.
    """

    waba_id = connection.parent_business_id or connection.external_account_id
    app_id = (settings.meta_facebook_app_id or "").strip()
    if not waba_id:
        raise ChannelApiError("Die WhatsApp Business Account ID fehlt")
    if not app_id:
        raise ChannelApiError("Die Meta-App-ID für WhatsApp fehlt")

    subscribed = api.whatsapp_subscribed_apps(waba_id=waba_id, access_token=access_token)
    repaired = not _whatsapp_app_is_subscribed(subscribed, app_id)
    if repaired:
        api.subscribe_whatsapp_app(waba_id=waba_id, access_token=access_token)
        subscribed = api.whatsapp_subscribed_apps(
            waba_id=waba_id,
            access_token=access_token,
        )
        if not _whatsapp_app_is_subscribed(subscribed, app_id):
            raise ChannelApiError(
                "Das WhatsApp-Webhook-Abonnement konnte nicht bestätigt werden"
            )

    connection.settings = {
        **(connection.settings or {}),
        "webhook_subscription_confirmed": True,
        "webhook_subscription_checked_at": datetime.now(timezone.utc).isoformat(),
    }
    return repaired


def _validated_whatsapp_registration_pin(value: str) -> str:
    pin = value.strip()
    if not re.fullmatch(r"[0-9]{6}", pin):
        raise ChannelApiError("Die WhatsApp-Aktivierungs-PIN muss genau 6 Ziffern haben")
    return pin


def _mark_whatsapp_phone_registered(connection: SocialChannelConnection) -> None:
    connection.settings = {
        **(connection.settings or {}),
        "phone_registered": True,
        "phone_registered_at": datetime.now(timezone.utc).isoformat(),
    }


def start_channel_oauth(
    db: Session,
    settings: Settings,
    *,
    channel_type: str,
    user: User,
    api: MetaGraphClient,
) -> str:
    if channel_type not in {"facebook", "whatsapp"}:
        raise ChannelApiError("Unbekannter Meta-Kanal")
    assert_channel_enabled(settings, channel_type)
    redirect_uri = settings.meta_facebook_oauth_redirect_uri
    if not redirect_uri:
        raise ChannelApiError("META_FACEBOOK_OAUTH_REDIRECT_URI fehlt")
    state = random_oauth_state()
    db.add(
        SocialChannelOAuthState(
            channel_type=channel_type,
            state_hash=secret_hash(state),
            user_id=user.id,
            redirect_uri=redirect_uri,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=settings.meta_oauth_state_ttl_seconds),
        )
    )
    db.add(
        AuditLog(
            user_id=user.id,
            action=f"channel.{channel_type}.oauth_started",
            entity_type="social_channel_connection",
            details={"channel_type": channel_type},
        )
    )
    db.commit()
    return api.authorization_url(
        state=state,
        redirect_uri=redirect_uri,
        channel_type=channel_type,
    )


def load_oauth_state(db: Session, raw_state: str, *, lock: bool = False):
    statement = select(SocialChannelOAuthState).where(
        SocialChannelOAuthState.state_hash == secret_hash(raw_state)
    )
    if lock:
        statement = statement.with_for_update()
    item = db.scalar(statement)
    if (
        item is None
        or item.used_at is not None
        or _utc(item.expires_at) <= datetime.now(timezone.utc)
    ):
        raise ChannelApiError("Verbindungsvorgang fehlt, ist abgelaufen oder bereits verwendet")
    return item


def prepare_facebook_selection(
    db: Session,
    settings: Settings,
    *,
    raw_state: str,
    code: str,
    api: MetaGraphClient,
) -> tuple[SocialChannelOAuthState, list[dict]]:
    state = load_oauth_state(db, raw_state, lock=True)
    if state.channel_type != "facebook":
        raise ChannelApiError("Verbindungsvorgang gehört nicht zu Facebook")
    token = api.exchange_code(code=code, redirect_uri=state.redirect_uri)
    granted_scopes = api.granted_permissions(token.access_token)
    missing = FACEBOOK_REQUIRED_SCOPES - granted_scopes
    if missing:
        raise ChannelApiError("Für Facebook fehlt noch mindestens eine erforderliche Berechtigung")
    pages = api.managed_pages(token.access_token)
    if not pages:
        raise ChannelApiError("Es wurde keine verwaltbare Facebook-Seite gefunden")
    state.encrypted_selection_payload = TokenCipher(settings.meta_token_encryption_key).encrypt(
        json.dumps(
            {
                "pages": pages,
                "scopes": sorted(granted_scopes),
                "expires_in": token.expires_in,
            },
            separators=(",", ":"),
        )
    )
    db.commit()
    return state, [
        {key: value for key, value in page.items() if key != "access_token"} for page in pages
    ]


def complete_facebook_selection(
    db: Session,
    settings: Settings,
    *,
    raw_state: str,
    page_id: str,
    api: MetaGraphClient,
) -> SocialChannelConnection:
    state = load_oauth_state(db, raw_state, lock=True)
    if state.channel_type != "facebook" or not state.encrypted_selection_payload:
        raise ChannelApiError("Facebook-Kontoauswahl ist nicht vorbereitet")
    payload = json.loads(
        TokenCipher(settings.meta_token_encryption_key).decrypt(state.encrypted_selection_payload)
    )
    pages = payload.get("pages", []) if isinstance(payload, dict) else payload
    granted_scopes = set(payload.get("scopes", [])) if isinstance(payload, dict) else set()
    selected = next((page for page in pages if str(page.get("id")) == page_id), None)
    if not selected or not selected.get("can_publish"):
        raise ChannelApiError("Für diese Facebook-Seite fehlt das Recht zum Veröffentlichen")
    page_token = str(selected.get("access_token") or "")
    profile = api.page_profile(page_id=page_id, access_token=page_token)
    item = db.scalar(
        select(SocialChannelConnection).where(
            SocialChannelConnection.channel_type == "facebook",
            SocialChannelConnection.external_account_id == page_id,
        )
    )
    if item is None:
        item = SocialChannelConnection(
            channel_type="facebook",
            internal_name=str(profile.get("name") or selected["name"]),
            display_name=str(profile.get("name") or selected["name"]),
        )
        db.add(item)
    item.external_account_id = page_id
    item.status = "connected"
    item.capabilities = [cap.key for cap in CHANNEL_CAPABILITIES["facebook"]]
    item.scopes = sorted(granted_scopes or FACEBOOK_REQUIRED_SCOPES)
    item.encrypted_token = TokenCipher(settings.meta_token_encryption_key).encrypt(page_token)
    item.token_key_version = settings.meta_token_key_version
    expires_in = payload.get("expires_in") if isinstance(payload, dict) else None
    item.token_expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=int(expires_in)) if expires_in else None
    )
    item.api_version = settings.meta_graph_version
    item.active = True
    item.publishing_enabled = False
    item.automatic_delivery_enabled = False
    item.last_check_at = datetime.now(timezone.utc)
    item.last_success_at = item.last_check_at
    item.last_error = None
    state.used_at = datetime.now(timezone.utc)
    state.encrypted_selection_payload = None
    # SQLAlchemy applies the UUID default during the flush.  The audit entry
    # must never be written with a missing entity reference for a newly
    # connected page.
    db.flush()
    db.add(
        AuditLog(
            user_id=state.user_id,
            action="channel.facebook.connected",
            entity_type="social_channel_connection",
            entity_id=item.id,
            details={"display_name": item.display_name},
        )
    )
    db.commit()
    return item


def complete_whatsapp_onboarding(
    db: Session,
    settings: Settings,
    *,
    user: User,
    code: str,
    waba_id: str,
    phone_number_id: str,
    registration_pin: str,
    api: MetaGraphClient,
) -> SocialChannelConnection:
    assert_channel_enabled(settings, "whatsapp")
    if not re.fullmatch(r"[0-9]{5,40}", waba_id) or not re.fullmatch(
        r"[0-9]{5,40}", phone_number_id
    ):
        raise ChannelApiError("WhatsApp-Konto oder Telefonnummer ist ungültig")
    token = api.exchange_code(code=code)
    granted_scopes = api.granted_permissions(token.access_token)
    if WHATSAPP_REQUIRED_SCOPES - granted_scopes:
        raise ChannelApiError("Für WhatsApp fehlt noch mindestens eine erforderliche Berechtigung")
    pin = _validated_whatsapp_registration_pin(registration_pin)
    phone = api.whatsapp_phone(
        phone_number_id=phone_number_id,
        access_token=token.access_token,
    )
    api.register_whatsapp_phone(
        phone_number_id=phone_number_id,
        access_token=token.access_token,
        pin=pin,
    )
    api.subscribe_whatsapp_app(waba_id=waba_id, access_token=token.access_token)
    item = db.scalar(
        select(SocialChannelConnection).where(
            SocialChannelConnection.channel_type == "whatsapp",
            SocialChannelConnection.phone_number_id == phone_number_id,
        )
    )
    if item is None:
        item = SocialChannelConnection(
            channel_type="whatsapp",
            internal_name=str(phone.get("verified_name") or "WhatsApp Vereinsnews"),
            display_name=str(phone.get("verified_name") or "WhatsApp Vereinsnews"),
        )
        db.add(item)
    item.external_account_id = waba_id
    item.parent_business_id = waba_id
    item.phone_number_id = phone_number_id
    item.display_phone_number = str(phone.get("display_phone_number") or "")
    item.status = "connected"
    _mark_whatsapp_phone_registered(item)
    item.capabilities = [cap.key for cap in CHANNEL_CAPABILITIES["whatsapp"]]
    item.scopes = sorted(granted_scopes)
    item.encrypted_token = TokenCipher(settings.meta_token_encryption_key).encrypt(
        token.access_token
    )
    item.token_key_version = settings.meta_token_key_version
    item.token_expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=token.expires_in)
        if token.expires_in
        else None
    )
    item.api_version = settings.meta_graph_version
    item.active = True
    item.publishing_enabled = False
    item.automatic_delivery_enabled = False
    item.last_check_at = datetime.now(timezone.utc)
    item.last_success_at = item.last_check_at
    item.last_error = None
    db.flush()
    templates = api.whatsapp_templates(waba_id=waba_id, access_token=token.access_token)
    for value in templates:
        provider_id = str(value.get("id") or "")
        if not provider_id:
            continue
        existing = db.scalar(
            select(WhatsAppMessageTemplate).where(
                WhatsAppMessageTemplate.channel_connection_id == item.id,
                WhatsAppMessageTemplate.provider_template_id == provider_id,
            )
        )
        target = existing or WhatsAppMessageTemplate(
            channel_connection_id=item.id,
            provider_template_id=provider_id,
            name=str(value.get("name") or provider_id),
            message_type="general",
        )
        db.add(target)
        target.language = str(value.get("language") or "de")
        target.category = str(value.get("category") or "utility").lower()
        target.status = str(value.get("status") or "draft").lower()
        target.components = list(value.get("components") or [])
        target.last_synced_at = datetime.now(timezone.utc)
    db.add(
        AuditLog(
            user_id=user.id,
            action="channel.whatsapp.connected",
            entity_type="social_channel_connection",
            entity_id=item.id,
            details={"display_phone_number": item.display_phone_number},
        )
    )
    db.commit()
    return item


def activate_existing_whatsapp_phone(
    db: Session,
    settings: Settings,
    *,
    user: User,
    connection: SocialChannelConnection,
    registration_pin: str,
    api: MetaGraphClient,
) -> SocialChannelConnection:
    """Finish Cloud API registration for a connection created by older code."""

    assert_channel_enabled(settings, "whatsapp")
    if connection.channel_type != "whatsapp":
        raise ChannelApiError("Diese Verbindung ist kein WhatsApp-Kanal")
    if not connection.phone_number_id or not connection.parent_business_id:
        raise ChannelApiError("WhatsApp-Konto oder Telefonnummer ist unvollständig")
    if not connection.encrypted_token:
        raise ChannelApiError("Die WhatsApp-Verbindung muss zuerst erneuert werden")

    pin = _validated_whatsapp_registration_pin(registration_pin)
    try:
        token = TokenCipher(settings.meta_token_encryption_key).decrypt(connection.encrypted_token)
    except (MetaSecretError, ValueError) as exc:
        raise ChannelApiError("Die WhatsApp-Verbindung muss zuerst erneuert werden") from exc

    api.register_whatsapp_phone(
        phone_number_id=connection.phone_number_id,
        access_token=token,
        pin=pin,
    )
    phone = api.whatsapp_phone(
        phone_number_id=connection.phone_number_id,
        access_token=token,
    )
    api.subscribe_whatsapp_app(
        waba_id=connection.parent_business_id,
        access_token=token,
    )

    connection.display_phone_number = str(
        phone.get("display_phone_number") or connection.display_phone_number or ""
    )
    _mark_whatsapp_phone_registered(connection)
    connection.status = "connected"
    connection.active = True
    connection.last_check_at = datetime.now(timezone.utc)
    connection.last_success_at = connection.last_check_at
    connection.last_error = None
    db.add(
        AuditLog(
            user_id=user.id,
            action="channel.whatsapp.phone_registered",
            entity_type="social_channel_connection",
            entity_id=connection.id,
            details={"result": "registered"},
        )
    )
    db.commit()
    return connection
