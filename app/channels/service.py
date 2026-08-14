from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.capabilities import CHANNEL_CAPABILITIES, status_label
from app.models import (
    InstagramConnection,
    InstagramPage,
    SocialChannelConnection,
    TeamChannelAssignment,
    WhatsAppMessageTemplate,
    WhatsAppRecipient,
)


def sync_instagram_channel(db: Session, page: InstagramPage) -> SocialChannelConnection:
    """Spiegelt den bestehenden Instagram-Datensatz ohne Token-Duplikation."""
    legacy = db.scalar(
        select(InstagramConnection).where(InstagramConnection.instagram_page_id == page.id)
    )
    item = db.scalar(
        select(SocialChannelConnection).where(
            SocialChannelConnection.legacy_instagram_page_id == page.id
        )
    )
    if item is None:
        item = SocialChannelConnection(
            channel_type="instagram",
            internal_name=page.internal_name,
            display_name=page.display_name,
            legacy_instagram_page_id=page.id,
        )
        db.add(item)
    item.username = page.username
    item.external_account_id = legacy.instagram_user_id if legacy else page.account_id
    item.status = legacy.status if legacy else page.connection_status
    item.capabilities = [cap.key for cap in CHANNEL_CAPABILITIES["instagram"]]
    item.scopes = list(legacy.scopes or []) if legacy else []
    item.settings = {
        **(item.settings or {}),
        "feed_enabled": bool((page.allowed_types or {}).get("feed", True)),
        "story_enabled": bool((page.allowed_types or {}).get("story", True)),
    }
    item.token_expires_at = legacy.token_expires_at if legacy else None
    item.token_key_version = legacy.token_key_version if legacy else None
    item.api_version = legacy.api_version if legacy else None
    item.active = page.active
    item.publishing_enabled = page.publishing_enabled
    item.automatic_delivery_enabled = page.automatic_publishing_enabled
    item.last_check_at = legacy.last_check_at if legacy else page.last_check_at
    item.last_success_at = legacy.last_success_at if legacy else None
    item.last_error = legacy.last_error if legacy else page.last_error
    item.disconnected_at = legacy.disconnected_at if legacy else None
    return item


def ensure_instagram_channels(db: Session) -> list[SocialChannelConnection]:
    channels = []
    for page in db.scalars(
        select(InstagramPage)
        .where(InstagramPage.archived_at.is_(None))
        .order_by(InstagramPage.display_name)
    ):
        channels.append(sync_instagram_channel(db, page))
    db.flush()
    return channels


def channel_cards(db: Session) -> dict[str, list[dict]]:
    ensure_instagram_channels(db)
    connections = list(
        db.scalars(
            select(SocialChannelConnection).order_by(
                SocialChannelConnection.channel_type,
                SocialChannelConnection.display_name,
            )
        )
    )
    result: dict[str, list[dict]] = {"instagram": [], "facebook": [], "whatsapp": []}
    for item in connections:
        capabilities = {
            capability.key: capability.label
            for capability in CHANNEL_CAPABILITIES.get(item.channel_type, ())
        }
        active_capabilities = [
            capabilities[key] for key in item.capabilities or [] if key in capabilities
        ]
        progress = None
        registration_required = False
        display_status = item.status
        if item.channel_type == "whatsapp":
            registration_required = not bool((item.settings or {}).get("phone_registered"))
            if registration_required and item.status == "connected":
                display_status = "setup_required"
            approved_template = db.scalar(
                select(WhatsAppMessageTemplate.id).where(
                    WhatsAppMessageTemplate.channel_connection_id == item.id,
                    WhatsAppMessageTemplate.status == "approved",
                )
            )
            opted_in_recipient = db.scalar(
                select(WhatsAppRecipient.id).where(
                    WhatsAppRecipient.channel_connection_id == item.id,
                    WhatsAppRecipient.active.is_(True),
                    WhatsAppRecipient.opt_in_status == "confirmed",
                )
            )
            completed = sum(
                [
                    bool(
                        item.external_account_id
                        and item.phone_number_id
                        and not registration_required
                    ),
                    bool(item.display_phone_number),
                    bool(approved_template),
                    bool(opted_in_recipient),
                ]
            )
            progress = {"completed": completed, "total": 4}
        result.setdefault(item.channel_type, []).append(
            {
                "connection": item,
                "display_status": display_status,
                "status_label": status_label(display_status),
                "capability_labels": active_capabilities,
                "progress": progress,
                "registration_required": registration_required,
                "webhook_subscription_confirmed": bool(
                    (item.settings or {}).get("webhook_subscription_confirmed")
                ),
            }
        )
    return result


def assignment_map(db: Session) -> dict[tuple[str, str], TeamChannelAssignment]:
    return {
        (item.team_id, item.channel_connection_id): item
        for item in db.scalars(select(TeamChannelAssignment))
    }


def mark_connection_error(connection: SocialChannelConnection, message: str) -> None:
    connection.status = "disrupted"
    connection.last_error = message[:500]
    connection.last_check_at = datetime.now(timezone.utc)
