from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.meta.security import TokenCipher
from app.models import (
    FeatureFlag,
    MatchFeedbackContact,
    MatchFeedbackEndpoint,
    MatchFeedbackLinkToken,
    SocialChannelConnection,
)


class TelegramApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: int | None = None,
        permanent: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.permanent = permanent


@dataclass(frozen=True)
class TelegramBotIdentity:
    bot_id: str
    username: str
    display_name: str


class TelegramBotClient:
    """Minimaler offizieller Bot-API-Client ohne Token in Fehlern oder Logs."""

    def __init__(self, settings, token: str):
        self.settings = settings
        self.token = token.strip()
        if not self.token or ":" not in self.token:
            raise TelegramApiError("Das Bot-Token besitzt kein gültiges Format")

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> dict:
        url = f"{self.settings.telegram_bot_api_base_url.rstrip('/')}/bot{self.token}/{method}"
        try:
            response = httpx.post(
                url,
                json=payload or {},
                timeout=self.settings.telegram_http_timeout_seconds,
            )
        except httpx.HTTPError:
            # httpx exceptions can include the complete request URL. Telegram
            # places the bot token in that URL, so never retain the provider
            # exception as the public/loggable exception cause.
            raise TelegramApiError("Telegram ist vorübergehend nicht erreichbar") from None
        retry_after = None
        try:
            data = response.json()
            retry_after = int((data.get("parameters") or {}).get("retry_after") or 0) or None
        except (ValueError, TypeError):
            data = {}
        if response.status_code >= 400 or not data.get("ok"):
            description = str(data.get("description") or "Telegram hat die Anfrage abgelehnt")
            error_code = int(data.get("error_code") or response.status_code or 0)
            permanent = error_code in {400, 401, 403, 404}
            raise TelegramApiError(
                description[:400],
                status_code=error_code or response.status_code,
                retry_after=retry_after,
                permanent=permanent,
            )
        return data.get("result") or {}

    def get_me(self) -> TelegramBotIdentity:
        item = self._call("getMe")
        username = str(item.get("username") or "").strip()
        if not username:
            raise TelegramApiError("Telegram hat keinen Bot-Benutzernamen zurückgegeben")
        return TelegramBotIdentity(
            bot_id=str(item.get("id") or ""),
            username=username,
            display_name=str(item.get("first_name") or username)[:160],
        )

    def set_webhook(self, *, url: str, secret_token: str) -> None:
        self._call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": False,
            },
        )

    def get_webhook_info(self) -> dict:
        return self._call("getWebhookInfo")

    def delete_webhook(self) -> None:
        self._call("deleteWebhook", {"drop_pending_updates": False})

    def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        reply_markup: dict | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4096]}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._call("sendMessage", payload)

    def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        self._call(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text[:200]},
        )


def feedback_provider_enabled(db: Session, club_id: str, provider: str) -> bool:
    key = f"match_feedback.{provider}"
    platform_flag = db.scalar(
        select(FeatureFlag).where(FeatureFlag.club_id.is_(None), FeatureFlag.key == key)
    )
    if platform_flag is not None and not platform_flag.enabled:
        return False
    club_flag = db.scalar(
        select(FeatureFlag).where(
            FeatureFlag.club_id == club_id,
            FeatureFlag.key == key,
        )
    )
    if club_flag is not None:
        return bool(club_flag.enabled)
    if platform_flag is not None:
        return bool(platform_flag.enabled)
    return provider == "whatsapp"


def decrypt_bot_token(connection: SocialChannelConnection, settings) -> str:
    return TokenCipher(settings.meta_token_encryption_key).decrypt(connection.encrypted_token)


def connection_webhook_secret(connection: SocialChannelConnection, settings) -> str:
    encrypted = str((connection.settings or {}).get("encrypted_webhook_secret") or "")
    return TokenCipher(settings.meta_token_encryption_key).decrypt(encrypted)


def webhook_identifier(connection: SocialChannelConnection) -> str:
    return str((connection.settings or {}).get("webhook_identifier") or "")


def create_contact_link(
    db: Session,
    *,
    contact: MatchFeedbackContact,
    connection: SocialChannelConnection,
    created_by: str,
    settings,
) -> str:
    now = datetime.now(timezone.utc)
    # A newly issued link supersedes still-valid links for this contact and bot.
    for existing in db.scalars(
        select(MatchFeedbackLinkToken).where(
            MatchFeedbackLinkToken.club_id == contact.club_id,
            MatchFeedbackLinkToken.contact_id == contact.id,
            MatchFeedbackLinkToken.connection_id == connection.id,
            MatchFeedbackLinkToken.used_at.is_(None),
            MatchFeedbackLinkToken.expires_at > now,
        )
    ):
        existing.used_at = now
    raw = secrets.token_urlsafe(32)
    db.add(
        MatchFeedbackLinkToken(
            club_id=contact.club_id,
            contact_id=contact.id,
            connection_id=connection.id,
            token_digest=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=now + timedelta(minutes=settings.telegram_link_ttl_minutes),
            created_by=created_by,
        )
    )
    return f"https://t.me/{connection.username}?start={raw}"


def consume_contact_link(
    db: Session,
    *,
    connection: SocialChannelConnection,
    raw_token: str,
    external_user_id: str,
    external_chat_id: str,
    external_username: str | None,
) -> MatchFeedbackEndpoint | None:
    digest = hashlib.sha256(raw_token.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    token = db.scalar(
        select(MatchFeedbackLinkToken)
        .where(
            MatchFeedbackLinkToken.club_id == connection.club_id,
            MatchFeedbackLinkToken.connection_id == connection.id,
            MatchFeedbackLinkToken.token_digest == digest,
            MatchFeedbackLinkToken.used_at.is_(None),
            MatchFeedbackLinkToken.expires_at > now,
        )
        .with_for_update()
    )
    if token is None:
        return None
    contact = db.scalar(
        select(MatchFeedbackContact).where(
            MatchFeedbackContact.id == token.contact_id,
            MatchFeedbackContact.club_id == connection.club_id,
            MatchFeedbackContact.active.is_(True),
        )
    )
    if contact is None:
        return None
    duplicate = db.scalar(
        select(MatchFeedbackEndpoint).where(
            MatchFeedbackEndpoint.club_id == connection.club_id,
            MatchFeedbackEndpoint.connection_id == connection.id,
            MatchFeedbackEndpoint.provider == "telegram",
            MatchFeedbackEndpoint.external_chat_id == external_chat_id,
            MatchFeedbackEndpoint.contact_id != contact.id,
            MatchFeedbackEndpoint.status == "connected",
        )
    )
    if duplicate is not None:
        return None
    endpoint = db.scalar(
        select(MatchFeedbackEndpoint).where(
            MatchFeedbackEndpoint.club_id == connection.club_id,
            MatchFeedbackEndpoint.contact_id == contact.id,
            MatchFeedbackEndpoint.provider == "telegram",
        )
    )
    if endpoint is None:
        endpoint = MatchFeedbackEndpoint(
            club_id=connection.club_id,
            contact_id=contact.id,
            provider="telegram",
            connection_id=connection.id,
        )
        db.add(endpoint)
    endpoint.external_user_id = external_user_id
    endpoint.external_chat_id = external_chat_id
    endpoint.external_username = (external_username or "")[:160] or None
    endpoint.status = "connected"
    endpoint.is_primary = contact.preferred_provider == "telegram"
    endpoint.linked_at = now
    endpoint.disabled_at = None
    token.used_at = now
    return endpoint


def safe_payload_metadata(message: dict) -> dict:
    return {
        "has_photo": bool(message.get("photo")),
        "has_document": bool(message.get("document")),
        "has_video": bool(message.get("video")),
        "has_voice": bool(message.get("voice")),
        "has_audio": bool(message.get("audio")),
        "has_video_note": bool(message.get("video_note")),
        "has_sticker": bool(message.get("sticker")),
        "has_location": bool(message.get("location")),
        "has_caption": bool(message.get("caption")),
    }


def payload_digest(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def secret_matches(
    connection: SocialChannelConnection,
    supplied: str,
    settings,
) -> bool:
    try:
        expected = connection_webhook_secret(connection, settings)
    except Exception:
        return False
    return hmac.compare_digest(expected, supplied)
