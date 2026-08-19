from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.channels.api import ChannelApiError, MetaGraphClient
from app.match_reports.telegram import (
    TelegramApiError,
    TelegramBotClient,
    decrypt_bot_token,
)
from app.meta.security import TokenCipher
from app.models import (
    Club,
    Game,
    MatchFeedbackContact,
    MatchFeedbackEndpoint,
    SocialChannelConnection,
    WhatsAppMessageTemplate,
    WhatsAppRecipient,
)


def _localized_kickoff(db: Session, game: Game, settings) -> str:
    """Render the fixture time in the owning club's configured timezone."""

    club = db.scalar(select(Club).where(Club.id == game.club_id))
    timezone_name = str(
        (club.timezone if club is not None else None)
        or getattr(settings, "timezone", None)
        or "UTC"
    )
    try:
        target_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        target_timezone = timezone.utc
    return game.kickoff.astimezone(target_timezone).strftime("%d.%m.%Y %H:%M Uhr")


@dataclass(frozen=True)
class FeedbackTarget:
    contact: MatchFeedbackContact
    endpoint: MatchFeedbackEndpoint
    connection: SocialChannelConnection


@dataclass(frozen=True)
class FeedbackSendResult:
    external_message_id: str
    external_chat_id: str
    template_id: str | None = None


class FeedbackProvider(Protocol):
    name: str

    def send(
        self,
        db: Session,
        *,
        target: FeedbackTarget,
        game: Game,
        request_id: str,
        settings,
    ) -> FeedbackSendResult: ...


class WhatsAppFeedbackProvider:
    name = "whatsapp"

    def send(self, db: Session, *, target, game, request_id, settings):
        recipient = db.scalar(
            select(WhatsAppRecipient).where(
                WhatsAppRecipient.id == target.contact.recipient_id,
                WhatsAppRecipient.club_id == game.club_id,
                WhatsAppRecipient.channel_connection_id == target.connection.id,
                WhatsAppRecipient.active.is_(True),
                WhatsAppRecipient.opt_in_status == "confirmed",
            )
        )
        if recipient is None:
            raise ValueError("Für WhatsApp liegt keine gültige Einwilligung vor")
        template = db.scalar(
            select(WhatsAppMessageTemplate)
            .where(
                WhatsAppMessageTemplate.club_id == game.club_id,
                WhatsAppMessageTemplate.channel_connection_id == target.connection.id,
                WhatsAppMessageTemplate.status == "approved",
                WhatsAppMessageTemplate.name == settings.fupa_report_feedback_template_name,
                WhatsAppMessageTemplate.language == settings.fupa_report_feedback_template_language,
            )
            .order_by(desc(WhatsAppMessageTemplate.updated_at))
        )
        if template is None:
            raise ValueError("Die WhatsApp-Rückfragevorlage ist nicht genehmigt")
        api = MetaGraphClient(settings)
        token = TokenCipher(settings.meta_token_encryption_key).decrypt(
            target.connection.encrypted_token
        )
        payload = api.send_whatsapp_template(
            phone_number_id=target.connection.phone_number_id or "",
            access_token=token,
            to=recipient.normalized_phone,
            template_name=template.name,
            language=template.language,
            components=[
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": game.home_team[:120]},
                        {"type": "text", "text": game.away_team[:120]},
                        {
                            "type": "text",
                            "text": _localized_kickoff(db, game, settings),
                        },
                    ],
                }
            ],
        )
        messages = payload.get("messages") or []
        message_id = (
            str(messages[0].get("id") or "").strip()
            if messages and isinstance(messages[0], dict)
            else ""
        )
        return FeedbackSendResult(
            external_message_id=message_id,
            external_chat_id=recipient.normalized_phone,
            template_id=template.id,
        )


class TelegramFeedbackProvider:
    name = "telegram"

    def send(self, db: Session, *, target, game, request_id, settings):
        chat_id = str(target.endpoint.external_chat_id or "").strip()
        if not chat_id:
            raise ValueError("Der Telegram-Kontakt ist noch nicht verknüpft")
        text = (
            "Ergänzungen zum Spielbericht gesucht\n\n"
            f"{game.home_team} – {game.away_team}\n"
            f"{_localized_kickoff(db, game, settings)}\n\n"
            "Antworte bitte direkt auf diese Nachricht. "
            "Teile nur bestätigte Beobachtungen und keine sensiblen Daten mit."
        )
        result = TelegramBotClient(
            settings,
            decrypt_bot_token(target.connection, settings),
        ).send_message(
            chat_id=chat_id,
            text=text,
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "Keine Ergänzungen",
                            "callback_data": f"match_feedback:none:{request_id}",
                        }
                    ]
                ]
            },
        )
        return FeedbackSendResult(
            external_message_id=str(result.get("message_id") or ""),
            external_chat_id=chat_id,
        )


PROVIDERS: dict[str, FeedbackProvider] = {
    "whatsapp": WhatsAppFeedbackProvider(),
    "telegram": TelegramFeedbackProvider(),
}
PROVIDER_ERRORS = (ChannelApiError, TelegramApiError, ValueError)
