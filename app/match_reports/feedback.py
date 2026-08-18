from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.channels.api import ChannelApiError, MetaGraphClient
from app.meta.security import TokenCipher
from app.models import (
    AuditLog,
    Game,
    MatchFeedbackContact,
    MatchFeedbackRequest,
    MatchFeedbackResponse,
    SocialChannelConnection,
    WhatsAppMessageTemplate,
    WhatsAppRecipient,
)


def _message_id(payload: dict) -> str | None:
    messages = payload.get("messages") or []
    if not messages or not isinstance(messages[0], dict):
        return None
    value = str(messages[0].get("id") or "").strip()
    return value or None


def expire_feedback_requests(db: Session, *, at: datetime | None = None) -> int:
    at = at or datetime.now(timezone.utc)
    items = list(
        db.scalars(
            select(MatchFeedbackRequest).where(
                MatchFeedbackRequest.status.in_(["pending", "sent"]),
                MatchFeedbackRequest.deadline_at <= at,
            )
        )
    )
    for item in items:
        item.status = "expired"
    return len(items)


def request_match_feedback(db: Session, game: Game, settings) -> int:
    """Ask configured opt-in contacts once; absence of contacts never blocks a report."""

    contacts = list(
        db.scalars(
            select(MatchFeedbackContact)
            .join(
                WhatsAppRecipient,
                (WhatsAppRecipient.id == MatchFeedbackContact.recipient_id)
                & (WhatsAppRecipient.club_id == MatchFeedbackContact.club_id),
            )
            .where(
                MatchFeedbackContact.club_id == game.club_id,
                MatchFeedbackContact.team_id == game.team_id,
                MatchFeedbackContact.active.is_(True),
                MatchFeedbackContact.request_match_reports.is_(True),
                WhatsAppRecipient.active.is_(True),
                WhatsAppRecipient.opt_in_status == "confirmed",
                WhatsAppRecipient.club_id == game.club_id,
            )
            .order_by(MatchFeedbackContact.priority, MatchFeedbackContact.created_at)
        )
    )
    if not contacts:
        return 0

    connection = db.scalar(
        select(SocialChannelConnection).where(
            SocialChannelConnection.club_id == game.club_id,
            SocialChannelConnection.channel_type == "whatsapp",
            SocialChannelConnection.active.is_(True),
            SocialChannelConnection.status == "connected",
            SocialChannelConnection.encrypted_token.is_not(None),
            SocialChannelConnection.phone_number_id.is_not(None),
        )
    )
    if not connection:
        return 0
    template = db.scalar(
        select(WhatsAppMessageTemplate)
        .where(
            WhatsAppMessageTemplate.club_id == game.club_id,
            WhatsAppMessageTemplate.channel_connection_id == connection.id,
            WhatsAppMessageTemplate.status == "approved",
            WhatsAppMessageTemplate.name == settings.fupa_report_feedback_template_name,
            WhatsAppMessageTemplate.language == settings.fupa_report_feedback_template_language,
        )
        .order_by(desc(WhatsAppMessageTemplate.updated_at))
    )
    if not template:
        return 0

    token = TokenCipher(settings.meta_token_encryption_key).decrypt(connection.encrypted_token)
    api = MetaGraphClient(settings)
    sent = 0
    deadline = datetime.now(timezone.utc) + timedelta(
        minutes=settings.fupa_report_whatsapp_wait_minutes
    )
    for contact in contacts:
        key = hashlib.sha256(f"fupa-report:{game.id}:{contact.id}".encode()).hexdigest()
        if db.scalar(
            select(MatchFeedbackRequest.id).where(
                MatchFeedbackRequest.club_id == game.club_id,
                MatchFeedbackRequest.idempotency_key == key
            )
        ):
            continue
        request = MatchFeedbackRequest(
            club_id=game.club_id,
            game_id=game.id,
            team_id=game.team_id,
            contact_id=contact.id,
            channel_connection_id=connection.id,
            template_id=template.id,
            idempotency_key=key,
            status="pending",
            deadline_at=deadline,
        )
        db.add(request)
        db.flush()
        try:
            payload = api.send_whatsapp_template(
                phone_number_id=connection.phone_number_id,
                access_token=token,
                to=contact.normalized_phone,
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
                                "text": game.kickoff.astimezone(timezone.utc).strftime(
                                    "%d.%m.%Y %H:%M UTC"
                                ),
                            },
                        ],
                    }
                ],
            )
        except (ChannelApiError, ValueError) as exc:
            request.status = "failed"
            request.last_error = str(exc)[:500]
            continue
        request.status = "sent"
        request.requested_at = datetime.now(timezone.utc)
        request.provider_message_id = _message_id(payload)
        sent += 1
        db.add(
            AuditLog(
                club_id=game.club_id,
                user_id=None,
                action="match_report.feedback_requested",
                entity_type="match_feedback_request",
                entity_id=request.id,
                team_id=game.team_id,
                details={"game_id": game.id, "contact_id": contact.id},
            )
        )
    return sent


def consume_feedback_response(
    db: Session,
    *,
    connection: SocialChannelConnection,
    sender: str,
    provider_message_id: str,
    body: str,
) -> bool:
    """Assign an inbound reply only when exactly one open request matches."""

    if db.scalar(
        select(MatchFeedbackResponse.id).where(
            MatchFeedbackResponse.club_id == connection.club_id,
            MatchFeedbackResponse.provider_message_id == provider_message_id
        )
    ):
        return True
    phones = {sender, sender.removeprefix("+"), f"+{sender.removeprefix('+')}"}
    now = datetime.now(timezone.utc)
    requests = list(
        db.scalars(
            select(MatchFeedbackRequest)
            .join(
                MatchFeedbackContact,
                (MatchFeedbackContact.id == MatchFeedbackRequest.contact_id)
                & (MatchFeedbackContact.club_id == MatchFeedbackRequest.club_id),
            )
            .where(
                MatchFeedbackRequest.club_id == connection.club_id,
                MatchFeedbackRequest.channel_connection_id == connection.id,
                MatchFeedbackRequest.status.in_(["pending", "sent"]),
                MatchFeedbackRequest.deadline_at > now,
                MatchFeedbackContact.normalized_phone.in_(phones),
            )
            .order_by(desc(MatchFeedbackRequest.requested_at))
            .limit(2)
        )
    )
    if len(requests) != 1 or not body.strip():
        return False
    request = requests[0]
    db.add(
        MatchFeedbackResponse(
            club_id=connection.club_id,
            request_id=request.id,
            provider_message_id=provider_message_id,
            body=body.strip()[:5000],
            received_at=now,
        )
    )
    request.status = "answered"
    request.answered_at = now
    db.add(
        AuditLog(
            club_id=connection.club_id,
            user_id=None,
            action="match_report.feedback_received",
            entity_type="match_feedback_request",
            entity_id=request.id,
            team_id=request.team_id,
            details={"game_id": request.game_id},
        )
    )
    return True
