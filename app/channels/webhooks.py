from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.live.service import ingest_whatsapp_message
from app.models import (
    AuditLog,
    ChannelDeliveryAttempt,
    LiveDeliveryAttempt,
    LiveEventDelivery,
    MetaWebhookEvent,
    SocialChannelConnection,
    WhatsAppRecipient,
)
from app.tenancy.state import system_scope, tenant_scope

router = APIRouter()
settings = get_settings()


def _verify_signature(body: bytes, supplied: str) -> None:
    secret = settings.meta_facebook_app_secret
    if not secret or not supplied.startswith("sha256="):
        raise HTTPException(403, "Webhook-Signatur fehlt")
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        raise HTTPException(403, "Webhook-Signatur ist ungültig")


@router.get("/public/meta/webhook", response_class=PlainTextResponse)
def verify_meta_webhook(
    mode: str = Query(default="", alias="hub.mode"),
    verify_token: str = Query(default="", alias="hub.verify_token"),
    challenge: str = Query(default="", alias="hub.challenge"),
):
    if (
        mode != "subscribe"
        or not settings.meta_webhook_verify_token
        or not hmac.compare_digest(verify_token, settings.meta_webhook_verify_token)
    ):
        raise HTTPException(403, "Webhook-Verifizierung abgelehnt")
    return challenge


def _whatsapp_identifiers(payload: dict) -> tuple[str | None, str | None]:
    identifiers: set[tuple[str | None, str | None]] = set()
    for entry in payload.get("entry", []):
        waba_id = str(entry.get("id") or "") or None
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_id = str(metadata.get("phone_number_id") or "") or None
            if waba_id or phone_id:
                identifiers.add((waba_id, phone_id))
    if not identifiers:
        return None, None
    if len(identifiers) != 1:
        raise HTTPException(
            409,
            "Webhook enthält widersprüchliche WhatsApp-Kanalkennungen",
        )
    return identifiers.pop()


def _event_key(payload: dict, digest: str) -> tuple[str, str]:
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            for status in value.get("statuses", []):
                if status.get("id"):
                    return f"status:{status['id']}:{status.get('status', '')}", "message_status"
            for message in value.get("messages", []):
                if message.get("id"):
                    return f"message:{message['id']}", "incoming_message"
    return f"payload:{digest}", "unknown"


def _resolve_whatsapp_connection(
    db: Session,
    *,
    waba_id: str | None,
    phone_id: str | None,
) -> tuple[str, str]:
    """Resolve the tenant from immutable Meta identifiers before reading content."""

    with system_scope("Meta-Webhook eindeutig einem Kanal zuordnen"):
        identifiers = []
        if waba_id and phone_id:
            identifiers.append(
                and_(
                    SocialChannelConnection.parent_business_id == waba_id,
                    SocialChannelConnection.phone_number_id == phone_id,
                )
            )
        elif phone_id:
            identifiers.append(SocialChannelConnection.phone_number_id == phone_id)
        elif waba_id:
            identifiers.append(SocialChannelConnection.parent_business_id == waba_id)
        matches = (
            list(
                db.scalars(
                    select(SocialChannelConnection)
                    .where(
                        SocialChannelConnection.channel_type == "whatsapp",
                        or_(*identifiers),
                    )
                    .limit(2)
                )
            )
            if identifiers
            else []
        )
        if not matches:
            raise HTTPException(404, "Webhook-Kanal ist nicht bekannt")
        if len(matches) != 1:
            raise HTTPException(409, "Webhook-Kanal ist nicht eindeutig")
        return matches[0].club_id, matches[0].id


def _process_whatsapp_payload(
    db: Session,
    payload: dict,
    connection: SocialChannelConnection | str,
) -> None:
    if isinstance(connection, str):
        resolved = db.get(SocialChannelConnection, connection)
        if resolved is None:
            raise ValueError("WhatsApp-Verbindung fehlt")
        connection = resolved
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            for status in value.get("statuses", []):
                message_id = str(status.get("id") or "")
                attempt = db.scalar(
                    select(ChannelDeliveryAttempt).where(
                        ChannelDeliveryAttempt.platform_id == message_id
                    )
                )
                if attempt:
                    attempt.status = str(status.get("status") or attempt.status)
                    if attempt.status in {"sent", "delivered", "read"} and not attempt.sent_at:
                        attempt.sent_at = datetime.now(timezone.utc)
                live_attempt = db.scalar(
                    select(LiveDeliveryAttempt).where(LiveDeliveryAttempt.platform_id == message_id)
                )
                if live_attempt:
                    received_status = str(status.get("status") or live_attempt.status)
                    if received_status in {"sent", "delivered", "read", "failed"}:
                        live_attempt.status = received_status
                    timestamp = datetime.now(timezone.utc)
                    if (
                        live_attempt.status in {"sent", "delivered", "read"}
                        and not live_attempt.sent_at
                    ):
                        live_attempt.sent_at = timestamp
                    if live_attempt.status in {"delivered", "read"}:
                        live_attempt.delivered_at = live_attempt.delivered_at or timestamp
                    if live_attempt.status == "read":
                        live_attempt.read_at = live_attempt.read_at or timestamp
                    if live_attempt.status == "failed":
                        errors = status.get("errors") or []
                        live_attempt.error_category = "provider_delivery_failed"
                        live_attempt.error_message = (
                            str(errors[0].get("title") or "WhatsApp-Zustellung fehlgeschlagen")[
                                :500
                            ]
                            if errors
                            else "WhatsApp-Zustellung fehlgeschlagen"
                        )
                    parent = db.get(LiveEventDelivery, live_attempt.delivery_id)
                    if parent:
                        sibling_statuses = list(
                            db.scalars(
                                select(LiveDeliveryAttempt.status).where(
                                    LiveDeliveryAttempt.delivery_id == parent.id
                                )
                            )
                        )
                        if live_attempt.status == "failed":
                            parent.status = "failed"
                            parent.last_error = live_attempt.error_message
                        elif sibling_statuses and all(
                            item in {"delivered", "read"} for item in sibling_statuses
                        ):
                            parent.status = "delivered"
                            parent.delivered_at = timestamp
            for message in value.get("messages", []):
                sender = str(message.get("from") or "")
                original_text = str((message.get("text") or {}).get("body") or "").strip()
                text = original_text.casefold()
                recipient = db.scalar(
                    select(WhatsAppRecipient).where(
                        WhatsAppRecipient.channel_connection_id == connection.id,
                        WhatsAppRecipient.normalized_phone.in_({sender, f"+{sender}"}),
                    )
                )
                if text in {"stop", "stopp", "abmelden", "unsubscribe"} and recipient:
                    recipient.opt_in_status = "revoked"
                    recipient.opt_out_at = datetime.now(timezone.utc)
                    recipient.active = False
                    db.add(
                        AuditLog(
                            user_id=None,
                            action="channel.whatsapp.recipient_opted_out_by_message",
                            entity_type="whatsapp_recipient",
                            entity_id=recipient.id,
                            details={"source": "verified_meta_webhook"},
                        )
                    )
                    continue
                if not original_text or not message.get("id"):
                    continue
                result = ingest_whatsapp_message(
                    db,
                    connection=connection,
                    provider_message_id=str(message["id"]),
                    sender=sender,
                    text=original_text,
                    settings=settings,
                )
                if result.event is None:
                    db.add(
                        AuditLog(
                            user_id=None,
                            action="live.whatsapp_message_not_applied",
                            entity_type="social_channel_connection",
                            entity_id=connection.id,
                            details={"status": result.status},
                        )
                    )


@router.post("/public/meta/webhook")
async def receive_meta_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    _verify_signature(body, request.headers.get("x-hub-signature-256", ""))
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "Webhook-Nutzdaten sind ungültig") from exc
    digest = hashlib.sha256(body).hexdigest()
    waba_id, phone_id = _whatsapp_identifiers(payload)
    club_id, connection_id = _resolve_whatsapp_connection(
        db,
        waba_id=waba_id,
        phone_id=phone_id,
    )
    event_key, event_type = _event_key(payload, digest)
    with tenant_scope(club_id, "system:meta-webhook"):
        event = MetaWebhookEvent(
            channel_type="whatsapp",
            channel_connection_id=connection_id,
            provider_event_key=event_key,
            event_type=event_type,
            payload_digest=digest,
            status="received",
            received_at=datetime.now(timezone.utc),
        )
        db.add(event)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            return {"status": "duplicate"}
        _process_whatsapp_payload(db, payload, connection_id)
        event.status = "processed"
        event.processed_at = datetime.now(timezone.utc)
        db.commit()
    return {"status": "ok"}
