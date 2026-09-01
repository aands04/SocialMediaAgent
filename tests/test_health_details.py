import json
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import event

import app.monitoring.health_details as health_details
from app.config import Settings
from app.monitoring.health_details import collect_health_details, sanitize_health_report
from app.tenancy.state import system_scope


def _unsafe_report():
    return {
        "ok": False,
        "critical": [
            "Social-Media-Kanalverbindung",
            "Automatische FUSSBALL.DE-Synchronisation",
            "user@example.invalid",
        ],
        "database_url": "postgresql://admin:secret@db/production",
        "checks": {
            "postgresql": {
                "ok": True,
                "detail": "postgresql://diagnostic:password@db/production",
            },
            "worker": {
                "ok": True,
                "detail": "worker path /private/tenant and token=worker-secret",
            },
            "scheduler": {"ok": True, "detail": "secret scheduler detail"},
            "automatic_scheduler": {"ok": True, "token": "scheduler-token"},
            "automatic_fussball_sync": {
                "ok": False,
                "detail": {
                    "last_cycle": {"team_id": "private-team-id"},
                    "provider_token": "fussball-provider-token",
                },
            },
            "fussball_automatic": {
                "ok": False,
                "detail": {
                    "global_sync_gate": True,
                    "global_generation_gate": True,
                    "enabled_teams": 3,
                    "running": 1,
                    "errors": 1,
                    "stale": 2,
                    "stale_reasons": {
                        "poll_overdue": 1,
                        "success_overdue": 1,
                        "team-id-with-private-data": 99,
                    },
                    "last_success": datetime(2026, 9, 1, tzinfo=timezone.utc),
                    "last_errors": {
                        "team-secret-id": "provider response contained private match data"
                    },
                },
            },
            "smb": {"ok": True, "detail": "/private/club/share"},
            "publishing": {"ok": True, "access_token": "publishing-token"},
            "social_media_channels": {
                "ok": False,
                "detail": {
                    "instagram": {
                        "active_connections": 4,
                        "enabled_connections": 2,
                        "unhealthy_connections": 1,
                        "last_successful_check": datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc),
                        "account_id": "instagram-account-id",
                        "access_token": "instagram-access-token",
                    },
                    "facebook": {
                        "enabled_connections": 1,
                        "unhealthy_connections": 0,
                        "last_successful_check": datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc),
                        "connection_url": "https://example.invalid/?token=secret",
                    },
                    "whatsapp": {
                        "enabled_connections": 0,
                        "unhealthy_connections": 0,
                        "last_successful_check": None,
                        "phone_number": "+49 000 000000",
                    },
                    "unexpected_provider": {
                        "enabled_connections": 100,
                        "unhealthy_connections": 100,
                    },
                },
            },
            "new_future_check": {
                "ok": False,
                "secret": "must never be included automatically",
            },
        },
    }


def test_health_details_uses_strict_allowlist_and_aggregates_channels():
    payload = sanitize_health_report(_unsafe_report())

    assert payload == {
        "status": "degraded",
        "critical": [
            "Social-Media-Kanalverbindung",
            "Automatische FUSSBALL.DE-Synchronisation",
        ],
        "unknown_critical_count": 1,
        "checks": {
            "postgresql": {"ok": True},
            "worker": {"ok": True},
            "scheduler": {"ok": True},
            "automatic_scheduler": {"ok": True},
            "automatic_fussball_sync": {"ok": False},
            "fussball_automatic": {
                "ok": False,
                "detail": {
                    "global_sync_gate": True,
                    "enabled_teams": 3,
                    "running": 1,
                    "errors": 1,
                    "stale": 2,
                    "stale_reasons": {
                        "poll_overdue": 1,
                        "success_overdue": 1,
                    },
                },
            },
            "smb": {"ok": True},
            "publishing": {"ok": True},
            "social_media_channels": {
                "ok": False,
                "detail": {
                    "enabled_connections": 3,
                    "unhealthy_connections": 1,
                    "last_successful_check": "2026-09-01T09:00:00+00:00",
                },
            },
        },
    }

    serialized = json.dumps(payload)
    for forbidden in (
        "secret",
        "token",
        "account_id",
        "team-id",
        "team-secret-id",
        "user@example.invalid",
        "phone_number",
        "+49",
        "connection_url",
        "postgresql://",
        "diagnostic:password",
        "worker path",
        "private-team-id",
        "provider_token",
        "new_future_check",
        "global_generation_gate",
        "last_errors",
    ):
        assert forbidden not in serialized


def test_all_critical_checks_expose_only_allowed_health_fields():
    checks = sanitize_health_report(_unsafe_report())["checks"]
    critical_checks = {
        "postgresql",
        "worker",
        "scheduler",
        "automatic_scheduler",
        "automatic_fussball_sync",
        "smb",
        "publishing",
        "social_media_channels",
        "fussball_automatic",
    }

    assert set(checks) == critical_checks
    assert checks["automatic_fussball_sync"] == {"ok": False}
    for name in critical_checks:
        assert set(checks[name]) - {"detail"} == {"ok"}
    assert set(checks["social_media_channels"]) == {"ok", "detail"}
    assert set(checks["fussball_automatic"]) == {"ok", "detail"}
    for name in critical_checks - {"social_media_channels", "fussball_automatic"}:
        assert set(checks[name]) == {"ok"}


def test_known_critical_entries_do_not_increase_unknown_count():
    report = {
        "ok": False,
        "critical": [
            "PostgreSQL",
            "Worker",
            "Scheduler",
            "Automatischer Instagram-Scheduler",
            "Automatischer FUSSBALL.DE-Abruf",
            "SMB",
            "Publishing",
            "Social-Media-Kanalverbindung",
            "Automatische FUSSBALL.DE-Synchronisation",
        ],
    }

    assert sanitize_health_report(report)["unknown_critical_count"] == 0


def test_health_details_tolerates_missing_optional_checks():
    payload = sanitize_health_report({"ok": True})

    assert payload["status"] == "ok"
    assert payload["critical"] == []
    assert payload["unknown_critical_count"] == 0
    assert payload["checks"]["postgresql"] == {"ok": None}
    assert payload["checks"]["worker"] == {"ok": None}
    assert payload["checks"]["automatic_fussball_sync"] == {"ok": None}
    assert payload["checks"]["scheduler"] == {"ok": None}
    assert payload["checks"]["fussball_automatic"]["detail"] == {
        "global_sync_gate": None,
        "enabled_teams": None,
        "running": None,
        "errors": None,
        "stale": None,
        "stale_reasons": {},
    }
    assert payload["checks"]["social_media_channels"]["detail"] == {
        "enabled_connections": None,
        "unhealthy_connections": None,
        "last_successful_check": None,
    }


def test_collect_health_details_executes_no_database_writes(db, tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "worker-heartbeat.json").write_text(
        json.dumps(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "loops": 1,
                "scheduler": False,
                "automatic_scheduler": False,
                "automatic_fussball_sync": False,
                "automatic_post_generation": False,
            }
        )
    )
    settings = Settings(
        media_root=tmp_path,
        generated_root=tmp_path,
        log_root=log_root,
        backup_root=tmp_path / "backups",
    )
    writes = []

    def capture_statement(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().split(None, 1)[0].upper() in {
            "INSERT",
            "UPDATE",
            "DELETE",
            "MERGE",
            "CREATE",
            "ALTER",
            "DROP",
            "TRUNCATE",
        }:
            writes.append(statement)

    event.listen(db.bind, "before_cursor_execute", capture_statement)
    try:
        with system_scope("Read-only Diagnose in Test prüfen"):
            payload = collect_health_details(db, settings)
    finally:
        event.remove(db.bind, "before_cursor_execute", capture_statement)

    assert payload["status"] in {"ok", "degraded"}
    assert writes == []


def test_cli_failure_does_not_expose_exception_details(monkeypatch, capsys):
    monkeypatch.setattr(health_details, "_open_session", lambda: nullcontext(object()))
    monkeypatch.setattr(health_details, "get_settings", lambda: object())

    def fail_safely(_db, _settings):
        raise RuntimeError("postgresql://admin:password@db token=secret")

    monkeypatch.setattr(health_details, "collect_health_details", fail_safely)

    assert health_details.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Sanitizierte Health-Diagnose ist nicht verfügbar.\n"
    assert "password" not in captured.err
    assert "token" not in captured.err


def test_cli_prints_only_the_sanitized_payload(monkeypatch, capsys):
    monkeypatch.setattr(health_details, "_open_session", lambda: nullcontext(object()))
    monkeypatch.setattr(health_details, "get_settings", lambda: object())
    expected = sanitize_health_report(_unsafe_report())
    monkeypatch.setattr(health_details, "collect_health_details", lambda _db, _settings: expected)

    assert health_details.main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == expected
    assert captured.err == ""


def test_container_entrypoint_for_health_details_has_no_file_mutations():
    script = Path("scripts/health-details.sh").read_text()

    assert "python -m app.monitoring.health_details" in script
    for mutating_command in ("mkdir", "touch", "rm ", "mv ", "cp ", "chmod", "chown"):
        assert mutating_command not in script
