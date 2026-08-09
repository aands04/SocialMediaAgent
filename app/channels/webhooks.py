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
from app.models import (
    AuditLog,
    ChannelDeliveryAttempt,
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
    for entry in payload.get("entry", []):
        waba_id = str(entry.get("id") or "") or None
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_id = str(metadata.get("phone_number_id") or "") or None
            if waba_id or phone_id:
                return waba_id, phone_id
    return None, None


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


def _process_whatsapp_payload(db: Session, payload: dict, connection_id: str) -> None:
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
            for message in value.get("messages", []):
                sender = str(message.get("from") or "")
                text = str((message.get("text") or {}).get("body") or "").strip().casefold()
                if text not in {"stop", "stopp", "abmelden", "unsubscribe"}:
                    continue
                recipient = db.scalar(
                    select(WhatsAppRecipient).where(
                        WhatsAppRecipient.channel_connection_id == connection_id,
                        WhatsAppRecipient.normalized_phone.in_({sender, f"+{sender}"}),
                    )
                )
                if recipient:
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
        connection = matches[0]
        club_id = connection.club_id
        connection_id = connection.id
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
