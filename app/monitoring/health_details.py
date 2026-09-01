from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
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
_ALLOWED_STALE_REASONS = (
    "lease_missing",
    "lease_expired",
    "poll_overdue",
    "success_overdue",
)
_ALLOWED_CHANNEL_TYPES = ("instagram", "facebook", "whatsapp")


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


def _check_ok(checks: Mapping[str, Any], name: str) -> bool | None:
    return _optional_bool(_mapping(checks.get(name)).get("ok"))


def _safe_critical(report: Mapping[str, Any]) -> list[str]:
    values = report.get("critical")
    if not isinstance(values, (list, tuple)):
        return []
    return [value for value in values if type(value) is str and value in _ALLOWED_CRITICAL]


def _safe_stale_reasons(detail: Mapping[str, Any]) -> dict[str, int]:
    source = _mapping(detail.get("stale_reasons"))
    return {
        reason: count
        for reason in _ALLOWED_STALE_REASONS
        if (count := _optional_count(source.get(reason))) is not None
    }


def _aggregated_channel_detail(checks: Mapping[str, Any]) -> dict[str, int | str | None]:
    detail = _mapping(_mapping(checks.get("social_media_channels")).get("detail"))
    enabled_total = 0
    enabled_seen = False
    unhealthy_total = 0
    unhealthy_seen = False
    successful_checks: list[datetime] = []

    for channel_type in _ALLOWED_CHANNEL_TYPES:
        channel = _mapping(detail.get(channel_type))
        if (enabled := _optional_count(channel.get("enabled_connections"))) is not None:
            enabled_total += enabled
            enabled_seen = True
        if (unhealthy := _optional_count(channel.get("unhealthy_connections"))) is not None:
            unhealthy_total += unhealthy
            unhealthy_seen = True
        last_success = channel.get("last_successful_check")
        if isinstance(last_success, datetime):
            successful_checks.append(last_success)

    normalized_checks = [
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
        for value in successful_checks
    ]
    return {
        "enabled_connections": enabled_total if enabled_seen else None,
        "unhealthy_connections": unhealthy_total if unhealthy_seen else None,
        "last_successful_check": _optional_timestamp(
            max(normalized_checks) if normalized_checks else None
        ),
    }


def sanitize_health_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Build a new diagnostic payload from an explicit field allowlist."""

    checks = _mapping(report.get("checks"))
    fussball_detail = _mapping(_mapping(checks.get("fussball_automatic")).get("detail"))
    return {
        "status": "ok" if report.get("ok") is True else "degraded",
        "critical": _safe_critical(report),
        "checks": {
            "scheduler": {"ok": _check_ok(checks, "scheduler")},
            "automatic_scheduler": {"ok": _check_ok(checks, "automatic_scheduler")},
            "fussball_automatic": {
                "ok": _check_ok(checks, "fussball_automatic"),
                "detail": {
                    "global_sync_gate": _optional_bool(fussball_detail.get("global_sync_gate")),
                    "enabled_teams": _optional_count(fussball_detail.get("enabled_teams")),
                    "running": _optional_count(fussball_detail.get("running")),
                    "errors": _optional_count(fussball_detail.get("errors")),
                    "stale": _optional_count(fussball_detail.get("stale")),
                    "stale_reasons": _safe_stale_reasons(fussball_detail),
                },
            },
            "smb": {"ok": _check_ok(checks, "smb")},
            "publishing": {"ok": _check_ok(checks, "publishing")},
            "social_media_channels": {
                "ok": _check_ok(checks, "social_media_channels"),
                "detail": _aggregated_channel_detail(checks),
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
