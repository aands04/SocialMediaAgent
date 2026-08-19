from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.match_reports.feedback import consume_feedback_response
from app.match_reports.telegram import (
    TelegramApiError,
    TelegramBotClient,
    consume_contact_link,
    decrypt_bot_token,
    feedback_provider_enabled,
    payload_digest,
    safe_payload_metadata,
    secret_matches,
    webhook_identifier,
)
from app.models import (
    AuditLog,
    MatchFeedbackEndpoint,
    SocialChannelConnection,
    TelegramWebhookUpdate,
)
from app.tenancy.state import system_scope, tenant_scope

router = APIRouter()
settings = get_settings()
_MAX_WEBHOOK_BYTES = 256 * 1024


def _connection_for_identifier(db: Session, identifier: str) -> SocialChannelConnection | None:
    """Resolve the tenant before inspecting any contact or message content."""

    with system_scope("Telegram-Webhook eindeutig einem Vereinsbot zuordnen"):
        connections = list(
            db.scalars(
                select(SocialChannelConnection).where(
                    SocialChannelConnection.channel_type == "telegram"
                )
            )
        )
        matches = [item for item in connections if webhook_identifier(item) == identifier]
        return matches[0] if len(matches) == 1 else None


def _message_kind(message: dict[str, Any]) -> str:
    for kind in (
        "voice",
        "photo",
        "document",
        "video",
        "video_note",
        "audio",
        "sticker",
        "location",
    ):
        if message.get(kind):
            return kind
    return "text"


def _private_message(update: dict[str, Any]) -> dict[str, Any] | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        return None
    return message


def _linked_endpoint(
    db: Session, connection: SocialChannelConnection, chat_id: str
) -> MatchFeedbackEndpoint | None:
    return db.scalar(
        select(MatchFeedbackEndpoint).where(
            MatchFeedbackEndpoint.club_id == connection.club_id,
            MatchFeedbackEndpoint.connection_id == connection.id,
            MatchFeedbackEndpoint.provider == "telegram",
            MatchFeedbackEndpoint.external_chat_id == chat_id,
            MatchFeedbackEndpoint.status == "connected",
        )
    )


def _safe_send(client: TelegramBotClient | None, *, chat_id: str, text: str) -> None:
    if client is None:
        return
    try:
        client.send_message(chat_id=chat_id, text=text)
    except TelegramApiError:
        # Die fachliche Verarbeitung wurde bereits persistiert. Eine nicht
        # erreichbare Bestätigung darf deshalb keine erneute Zustellung auslösen.
        return


def _safe_answer_callback(
    client: TelegramBotClient | None, *, callback_query_id: str, text: str
) -> None:
    if client is None:
        return
    try:
        client.answer_callback_query(callback_query_id, text)
    except TelegramApiError:
        # Die fachliche Verarbeitung wurde bereits persistiert. Eine nicht
        # erreichbare Bestätigung darf deshalb keine erneute Zustellung auslösen.
        return


def _audit(
    db: Session,
    *,
    connection: SocialChannelConnection,
    action: str,
    details: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            club_id=connection.club_id,
            user_id=None,
            action=action,
            entity_type="social_channel_connection",
            entity_id=connection.id,
            details=details or {},
        )
    )


@router.post("/webhooks/telegram/{identifier}")
async def telegram_webhook(
    identifier: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    raw = await request.body()
    if len(raw) > _MAX_WEBHOOK_BYTES:
        raise HTTPException(413, "Telegram-Update ist zu groß")
    try:
        update = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "Telegram-Update ist ungültig") from exc
    if not isinstance(update, dict) or update.get("update_id") is None:
        raise HTTPException(400, "Telegram-Update besitzt keine Update-ID")

    connection = _connection_for_identifier(db, identifier)
    if connection is None:
        raise HTTPException(404, "Telegram-Webhook ist nicht bekannt")
    supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not supplied_secret or not secret_matches(connection, supplied_secret, settings):
        raise HTTPException(403, "Telegram-Webhook-Secret ist ungültig")

    update_id = str(update["update_id"])
    with tenant_scope(connection.club_id, "system:telegram-webhook"):
        existing = db.scalar(
            select(TelegramWebhookUpdate).where(
                TelegramWebhookUpdate.connection_id == connection.id,
                TelegramWebhookUpdate.update_id == update_id,
            )
        )
        if existing is not None:
            return {"ok": True}

        update_type = (
            "callback_query"
            if isinstance(update.get("callback_query"), dict)
            else "message"
            if isinstance(update.get("message"), dict)
            else "unknown"
        )
        ledger = TelegramWebhookUpdate(
            club_id=connection.club_id,
            connection_id=connection.id,
            update_id=update_id,
            update_type=update_type,
            payload_digest=payload_digest(update),
            status="received",
        )
        db.add(ledger)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            return {"ok": True}

        provider_enabled = feedback_provider_enabled(db, connection.club_id, "telegram")
        if not provider_enabled:
            _audit(
                db,
                connection=connection,
                action="match_report.telegram_webhook_received_disabled",
                details={"reason": "provider_disabled", "update_type": update_type},
            )
            # Deaktivierte Provider dürfen Updates weiterhin quittieren und
            # protokollieren, aber weder Kontakte verknüpfen noch fachliche
            # Feedback-Daten verändern oder Bot-Antworten auslösen.
            ledger.status = "ignored_disabled"
            ledger.processed_at = datetime.now(timezone.utc)
            db.commit()
            return {"ok": True}

        if not connection.active or connection.status != "connected":
            _audit(
                db,
                connection=connection,
                action="match_report.telegram_webhook_received_inactive",
                details={"reason": "connection_inactive", "update_type": update_type},
            )
            ledger.status = "ignored_inactive"
            ledger.processed_at = datetime.now(timezone.utc)
            db.commit()
            return {"ok": True}

        client = None
        try:
            client = TelegramBotClient(settings, decrypt_bot_token(connection, settings))
        except Exception:
            _audit(
                db,
                connection=connection,
                action="match_report.telegram_webhook_client_unavailable",
            )
            # Ohne entschlüsselbaren Bot-Zugriff ist die Verbindung technisch
            # unvollständig. Das Update wird sicher quittiert und protokolliert,
            # darf aber keine Kontakte oder fachlichen Rückmeldungen verändern.
            ledger.status = "ignored_unavailable"
            ledger.processed_at = datetime.now(timezone.utc)
            db.commit()
            return {"ok": True}
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            callback_message = callback.get("message") or {}
            chat = callback_message.get("chat") or {}
            callback_id = str(callback.get("id") or "")
            callback_data = str(callback.get("data") or "")
            parts = callback_data.split(":", 2)
            handled = False
            if (
                chat.get("type") == "private"
                and len(parts) == 3
                and parts[:2] == ["match_feedback", "none"]
            ):
                sender = str((callback.get("from") or {}).get("id") or "")
                chat_id = str(chat.get("id") or "")
                handled = consume_feedback_response(
                    db,
                    connection=connection,
                    provider="telegram",
                    sender=sender,
                    provider_message_id=f"callback:{callback_id}",
                    body="",
                    external_chat_id=chat_id,
                    request_id=parts[2],
                    payload_type="callback",
                    payload_metadata={"action": "no_additional_feedback"},
                    no_additional_feedback=True,
                )
                if handled:
                    _audit(
                        db,
                        connection=connection,
                        action="match_report.feedback_no_additions",
                        details={"provider": "telegram"},
                    )
            if callback_id and client is not None:
                background_tasks.add_task(
                    _safe_answer_callback,
                    client,
                    callback_query_id=callback_id,
                    text=("Keine Ergänzungen gespeichert" if handled else "Nicht zuordenbar"),
                )
            ledger.status = "processed" if handled else "ignored"

        else:
            message = _private_message(update)
            if message is None:
                ledger.status = "ignored_non_private"
            else:
                chat = message.get("chat") or {}
                sender_data = message.get("from") or {}
                chat_id = str(chat.get("id") or "")
                sender_id = str(sender_data.get("id") or "")
                message_id = str(message.get("message_id") or "")
                text = str(message.get("text") or message.get("caption") or "").strip()
                command, _, argument = text.partition(" ")
                endpoint = _linked_endpoint(db, connection, chat_id)

                if command == "/start" and argument.strip():
                    endpoint = consume_contact_link(
                        db,
                        connection=connection,
                        raw_token=argument.strip(),
                        external_user_id=sender_id,
                        external_chat_id=chat_id,
                        external_username=str(sender_data.get("username") or "") or None,
                    )
                    if endpoint is None:
                        background_tasks.add_task(
                            _safe_send,
                            client,
                            chat_id=chat_id,
                            text="Dieser Verknüpfungslink ist ungültig oder abgelaufen.",
                        )
                        ledger.status = "link_rejected"
                    else:
                        _audit(
                            db,
                            connection=connection,
                            action="match_report.telegram_contact_linked",
                            details={"contact_id": endpoint.contact_id},
                        )
                        background_tasks.add_task(
                            _safe_send,
                            client,
                            chat_id=chat_id,
                            text=(
                                "Verbindung erfolgreich. Nach Spielen können dir hier "
                                "Rückfragen für den Spielbericht gestellt werden."
                            ),
                        )
                        ledger.status = "linked"
                elif command in {"/help", "/start"}:
                    background_tasks.add_task(
                        _safe_send,
                        client,
                        chat_id=chat_id,
                        text=(
                            "Dieser Bot sammelt bestätigte Ergänzungen für Vereins-"
                            "Spielberichte. Antworte direkt auf eine Rückfrage."
                        ),
                    )
                    ledger.status = "help"
                elif command == "/status":
                    background_tasks.add_task(
                        _safe_send,
                        client,
                        chat_id=chat_id,
                        text=(
                            "Telegram ist mit deinem Rückfragekontakt verbunden."
                            if endpoint
                            else "Telegram ist noch nicht mit einem Kontakt verbunden."
                        ),
                    )
                    ledger.status = "status"
                elif endpoint is None:
                    ledger.status = "ignored_unlinked"
                else:
                    kind = _message_kind(message)
                    reply_id = (
                        str(((message.get("reply_to_message") or {}).get("message_id") or ""))
                        or None
                    )
                    metadata = safe_payload_metadata(message)
                    metadata["unsupported_for_transcription"] = kind != "text"
                    handled = consume_feedback_response(
                        db,
                        connection=connection,
                        provider="telegram",
                        sender=sender_id,
                        provider_message_id=message_id,
                        body=text if kind == "text" else "",
                        external_chat_id=chat_id,
                        reply_to_message_id=reply_id,
                        payload_type=kind,
                        payload_metadata=metadata,
                    )
                    ledger.status = "processed" if handled else "ambiguous"
                    if handled:
                        _audit(
                            db,
                            connection=connection,
                            action="match_report.telegram_feedback_received",
                            details={"payload_type": kind},
                        )
                    elif kind == "text":
                        background_tasks.add_task(
                            _safe_send,
                            client,
                            chat_id=chat_id,
                            text=(
                                "Die Antwort konnte keiner offenen Rückfrage eindeutig "
                                "zugeordnet werden. Bitte antworte direkt auf die Bot-Nachricht."
                            ),
                        )

        ledger.processed_at = datetime.now(timezone.utc)
        db.commit()
    return {"ok": True}
