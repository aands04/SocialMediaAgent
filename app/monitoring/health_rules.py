from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from app.config import Settings
from app.models import FussballSyncState, Team

FUSSBALL_STALE_REASONS = (
    "lease_missing",
    "lease_expired",
    "poll_overdue",
    "success_overdue",
)
FUSSBALL_SYNC_STATUSES = ("idle", "running", "error", "disabled")
FUSSBALL_ERROR_CATEGORIES = (
    "invalid_source",
    "upstream_network",
    "upstream_http",
    "parser_structure",
    "response_limit",
    "snapshot_storage",
    "provider_error",
    "unknown",
)
SOCIAL_CHANNEL_TYPES = ("instagram", "facebook", "whatsapp")
SOCIAL_CONNECTION_STATUSES = (
    "connected",
    "setup_required",
    "check_required",
    "expired",
    "permission_missing",
    "publishing_disabled",
    "disrupted",
    "disconnected",
    "invalid",
    "error",
    "unconfigured",
)
SOCIAL_HEALTH_REASONS = (
    "non_connected",
    "missing_last_success",
    "stale_last_success",
)

_FUSSBALL_ERROR_EXACT_CATEGORIES = {
    "Nur öffentliche HTTPS-URLs von FUSSBALL.DE sind erlaubt": "invalid_source",
    "URL enthält unzulässige Zugangsdaten oder Ports": "invalid_source",
    "Nicht erlaubter FUSSBALL.DE-AJAX-Pfad": "invalid_source",
    "Nicht erlaubter FUSSBALL.DE-Spielpfad": "invalid_source",
    "Spiel-ID der Detailseite stimmt nicht mit dem Spielplan überein": "invalid_source",
    "FUSSBALL.DE-Antwort überschreitet das Größenlimit": "response_limit",
    "FUSSBALL.DE-Schriftdatei ist unerwartet groß": "response_limit",
    (
        "FUSSBALL.DE nicht erreichbar: FUSSBALL.DE-Antwort überschreitet das Größenlimit"
    ): "response_limit",
    (
        "FUSSBALL.DE-Schrift nicht erreichbar: FUSSBALL.DE-Schriftdatei ist unerwartet groß"
    ): "response_limit",
    "Kanonische Spiel-URL fehlt auf der Detailseite": "parser_structure",
    "Spielort oder Platzart fehlen auf der Detailseite": "parser_structure",
    "Spielortdaten überschreiten die zulässige Länge": "parser_structure",
    "Keine Spiele erkannt; HTML-Struktur oder Pflichtfelder prüfen": "parser_structure",
    "Kompaktes Fixture ist unvollständig oder widersprüchlich": "parser_structure",
    "Ungültige Kennung der FUSSBALL.DE-Symbolschrift": "parser_structure",
    "Symbolschrift enthält kein eindeutig lesbares Torergebnis": "parser_structure",
    "Leeres Torergebnis in der Symbolschrift": "parser_structure",
    "Snapshot enthält einen Parserfehler": "parser_structure",
    "Anpfiff ohne Zeitzone wird nicht übernommen": "parser_structure",
    "Snapshot enthält keine vollständig parsebaren Spiele": "parser_structure",
    "Ungültiger Snapshot-Pfad": "snapshot_storage",
    "Unbekannte FUSSBALL.DE-AJAX-Ressource": "provider_error",
    "Snapshot hat keine gültige Mannschaft": "provider_error",
    "FUSSBALL.DE-Synchronisation wurde nicht beansprucht": "provider_error",
}
_PARSER_ERROR_PREFIXES = ("Ungültige FUSSBALL.DE-Symbolschrift:",)
_UPSTREAM_ERROR_PREFIXES = (
    "FUSSBALL.DE nicht erreichbar:",
    "FUSSBALL.DE-Schrift nicht erreichbar:",
)
_HTTP_ERROR_PREFIXES = (
    "Client error '",
    "Server error '",
    "Redirect response '",
)


class SocialHealthState(Protocol):
    status: str
    last_check_at: datetime | None
    last_success_at: datetime | None


def utc_datetime(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def fussball_sync_interval_hours(team: Team) -> int:
    try:
        interval_hours = int((team.rules or {}).get("sync_interval_hours", 24))
    except (TypeError, ValueError):
        interval_hours = 24
    return max(1, interval_hours)


def fussball_sync_stale_reason(
    state: FussballSyncState,
    team: Team,
    settings: Settings,
    *,
    now: datetime,
) -> str | None:
    """Return why an enabled team's sync is stale, using its persisted schedule."""

    now = utc_datetime(now)
    if state.status == "running":
        if state.lease_expires_at is None:
            return "lease_missing"
        if utc_datetime(state.lease_expires_at) <= now:
            return "lease_expired"
        return None

    grace = timedelta(seconds=max(60, settings.fussball_sync_error_backoff_seconds))
    if state.next_poll_at is None or utc_datetime(state.next_poll_at) + grace < now:
        return "poll_overdue"

    success_anchor = state.last_success_at or state.created_at
    if (
        success_anchor is not None
        and utc_datetime(success_anchor)
        + timedelta(hours=fussball_sync_interval_hours(team))
        + grace
        < now
    ):
        return "success_overdue"
    return None


def fussball_retry_scheduled(state: FussballSyncState, *, now: datetime) -> bool:
    return bool(
        state.next_poll_at is not None and utc_datetime(state.next_poll_at) > utc_datetime(now)
    )


def classify_fussball_provider_error(value: object) -> str:
    """Map only application-owned error forms to a fixed safe category."""

    if not isinstance(value, str) or not value:
        return "unknown"
    if category := _FUSSBALL_ERROR_EXACT_CATEGORIES.get(value):
        return category
    if value.startswith(_PARSER_ERROR_PREFIXES):
        return "parser_structure"
    for prefix in _UPSTREAM_ERROR_PREFIXES:
        if value.startswith(prefix):
            wrapped = value.removeprefix(prefix).lstrip()
            if wrapped.startswith(_HTTP_ERROR_PREFIXES):
                return "upstream_http"
            return "upstream_network"
    return "unknown"


def social_connection_health_reasons(
    connection: SocialHealthState,
    *,
    stale_before: datetime,
) -> tuple[str, ...]:
    """Return every health reason; one connection may have multiple reasons."""

    reasons = []
    if connection.status != "connected":
        reasons.append("non_connected")
    if connection.last_success_at is None:
        reasons.append("missing_last_success")
    elif utc_datetime(connection.last_success_at) < utc_datetime(stale_before):
        reasons.append("stale_last_success")
    return tuple(reasons)
