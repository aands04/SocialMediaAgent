from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChannelCapability:
    key: str
    label: str
    action: str


CHANNEL_CAPABILITIES: dict[str, tuple[ChannelCapability, ...]] = {
    "instagram": (
        ChannelCapability("feed_image", "Feed-Beiträge", "publish"),
        ChannelCapability("carousel", "Beiträge mit mehreren Bildern", "publish"),
        ChannelCapability("story", "Storys", "publish"),
        ChannelCapability("caption", "Begleittexte", "publish"),
    ),
    "facebook": (
        ChannelCapability("page_post", "Seitenbeiträge", "publish"),
        ChannelCapability("image_post", "Beiträge mit Bild", "publish"),
        ChannelCapability("multi_image", "Beiträge mit mehreren Bildern", "publish"),
        ChannelCapability("text", "Textbeiträge", "publish"),
    ),
    "whatsapp": (ChannelCapability("template_message", "Vorlagennachrichten", "send"),),
}

CHANNEL_LABELS = {
    "instagram": "Instagram",
    "facebook": "Facebook",
    "whatsapp": "WhatsApp",
}

STATUS_LABELS = {
    "connected": "Verbunden",
    "setup_required": "Einrichtung erforderlich",
    "check_required": "Verbindung prüfen",
    "expired": "Verbindung abgelaufen",
    "permission_missing": "Berechtigung fehlt",
    "publishing_disabled": "Veröffentlichung deaktiviert",
    "disrupted": "Verbindung gestört",
    "disconnected": "Getrennt",
    "unconfigured": "Einrichtung erforderlich",
}


def capability_keys(channel_type: str) -> set[str]:
    return {item.key for item in CHANNEL_CAPABILITIES.get(channel_type, ())}


def status_label(status: str | None) -> str:
    return STATUS_LABELS.get((status or "").casefold(), "Verbindung prüfen")
