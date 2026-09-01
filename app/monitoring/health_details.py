from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.monitoring.health_rules import (
    FUSSBALL_ERROR_CATEGORIES,
    FUSSBALL_STALE_REASONS,
    FUSSBALL_SYNC_STATUSES,
    SOCIAL_CHANNEL_TYPES,
    SOCIAL_CONNECTION_STATUSES,
)
from app.monitoring.service import system_status
from app.tenancy.state import system_scope

_ALLOWED_CRITICAL = frozenset(
    {
        "PostgreSQL",
        "Worker",
        "Scheduler",
        "Automatischer Instagram-Scheduler",
        "Automatischer FUSSBALL.DE-Abruf",
        "SMB",
        "Publishing",
        "Social-Media-Kanalverbindung",
        "Automatische FUSSBALL.DE-Synchronisation",
    }
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_bool(value: Any) -> bool | None:
    return value if type(value) is bool else None


def _optional_count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _optional_timestamp(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    normalized = (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
    return normalized.isoformat()


def _optional_label(value: Any, *, maximum: int) -> str | None:
    if type(value) is not str or not value or len(value) > maximum:
        return None
    if any(ord(character) < 32 for character in value) or "://" in value:
        return None
    return value


def _optional_enum(value: Any, allowed: tuple[str, ...]) -> str | None:
    if type(value) is not str:
        return None
    return value if value in allowed else "unknown"


def _check_ok(checks: Mapping[str, Any], name: str) -> bool | None:
    return _optional_bool(_mapping(checks.get(name)).get("ok"))


def _safe_critical(report: Mapping[str, Any]) -> list[str]:
    values = report.get("critical")
    if not isinstance(values, (list, tuple)):
        return []
    return [value for value in values if type(value) is str and value in _ALLOWED_CRITICAL]


def _unknown_critical_count(report: Mapping[str, Any]) -> int:
    values = report.get("critical")
    if not isinstance(values, (list, tuple)):
        return 0
    return sum(isinstance(value, str) and value not in _ALLOWED_CRITICAL for value in values)


def _safe_stale_reasons(detail: Mapping[str, Any]) -> dict[str, int]:
    source = _mapping(detail.get("stale_reasons"))
    return {
        reason: count
        for reason in FUSSBALL_STALE_REASONS
        if (count := _optional_count(source.get(reason))) is not None
    }


def _safe_status_counts(value: Any) -> dict[str, int]:
    source = _mapping(value)
    return {
        status: count
        for status in (*SOCIAL_CONNECTION_STATUSES, "unknown")
        if (count := _optional_count(source.get(status))) is not None
    }


def _safe_channel_detail(checks: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    detail = _mapping(_mapping(checks.get("social_media_channels")).get("detail"))
    return {
        channel_type: {
            "enabled_connections": _optional_count(channel.get("enabled_connections")),
            "unhealthy_connections": _optional_count(channel.get("unhealthy_connections")),
            "non_connected_connections": _optional_count(channel.get("non_connected_connections")),
            "missing_last_success": _optional_count(channel.get("missing_last_success")),
            "stale_last_success": _optional_count(channel.get("stale_last_success")),
            "last_check_at": _optional_timestamp(channel.get("last_check_at")),
            "last_successful_check": _optional_timestamp(channel.get("last_successful_check")),
            "status_counts": _safe_status_counts(channel.get("status_counts")),
        }
        for channel_type in SOCIAL_CHANNEL_TYPES
        for channel in (_mapping(detail.get(channel_type)),)
    }


def _safe_unhealthy_teams(detail: Mapping[str, Any]) -> list[dict[str, Any]]:
    source = detail.get("unhealthy_teams")
    if not isinstance(source, (list, tuple)):
        return []
    teams = []
    for value in source:
        team = _mapping(value)
        stale_reason = team.get("stale_reason")
        if stale_reason not in FUSSBALL_STALE_REASONS:
            continue
        teams.append(
            {
                "display_name": _optional_label(team.get("display_name"), maximum=120),
                "short_name": _optional_label(team.get("short_name"), maximum=30),
                "status": _optional_enum(team.get("status"), FUSSBALL_SYNC_STATUSES),
                "stale_reason": stale_reason,
                "sync_interval_hours": _optional_count(team.get("sync_interval_hours")),
                "consecutive_failures": _optional_count(team.get("consecutive_failures")),
                "last_success_at": _optional_timestamp(team.get("last_success_at")),
                "last_completed_at": _optional_timestamp(team.get("last_completed_at")),
                "next_poll_at": _optional_timestamp(team.get("next_poll_at")),
                "retry_scheduled": _optional_bool(team.get("retry_scheduled")),
                "error_category": _optional_enum(
                    team.get("error_category"), FUSSBALL_ERROR_CATEGORIES
                ),
            }
        )
    return teams


def sanitize_health_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Build a new diagnostic payload from an explicit field allowlist."""

    checks = _mapping(report.get("checks"))
    fussball_detail = _mapping(_mapping(checks.get("fussball_automatic")).get("detail"))
    return {
        "status": "ok" if report.get("ok") is True else "degraded",
        "critical": _safe_critical(report),
        "unknown_critical_count": _unknown_critical_count(report),
        "checks": {
            "postgresql": {"ok": _check_ok(checks, "postgresql")},
            "worker": {"ok": _check_ok(checks, "worker")},
            "scheduler": {"ok": _check_ok(checks, "scheduler")},
            "automatic_scheduler": {"ok": _check_ok(checks, "automatic_scheduler")},
            "automatic_fussball_sync": {"ok": _check_ok(checks, "automatic_fussball_sync")},
            "fussball_automatic": {
                "ok": _check_ok(checks, "fussball_automatic"),
                "detail": {
                    "global_sync_gate": _optional_bool(fussball_detail.get("global_sync_gate")),
                    "enabled_teams": _optional_count(fussball_detail.get("enabled_teams")),
                    "running": _optional_count(fussball_detail.get("running")),
                    "errors": _optional_count(fussball_detail.get("errors")),
                    "stale": _optional_count(fussball_detail.get("stale")),
                    "stale_reasons": _safe_stale_reasons(fussball_detail),
                    "unhealthy_teams": _safe_unhealthy_teams(fussball_detail),
                },
            },
            "smb": {"ok": _check_ok(checks, "smb")},
            "publishing": {"ok": _check_ok(checks, "publishing")},
            "social_media_channels": {
                "ok": _check_ok(checks, "social_media_channels"),
                "detail": _safe_channel_detail(checks),
            },
        },
    }


def collect_health_details(db: Session, settings: Settings) -> dict[str, Any]:
    return sanitize_health_report(system_status(db, settings))


def _open_session():
    # Import lazily so configuration/engine failures are covered by the
    # generic, non-sensitive CLI error below.
    from app.db import SessionLocal

    return SessionLocal()


def main() -> int:
    try:
        with system_scope("Sanitizierte read-only Health-Diagnose"), _open_session() as db:
            payload = collect_health_details(db, get_settings())
    except Exception:
        print("Sanitizierte Health-Diagnose ist nicht verfügbar.", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
