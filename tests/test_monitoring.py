import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import Settings
from app.db import get_db
from app.models import FussballSyncState, SocialChannelConnection, Team
from app.monitoring.health_details import collect_health_details
from app.monitoring.health_rules import (
    classify_fussball_provider_error,
    fussball_retry_scheduled,
    social_connection_health_reasons,
)
from app.monitoring.service import _fussball_sync_stale_reason, system_status
from app.tenancy.state import system_scope

NOW = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def _team(*, interval_hours=24, enabled=True, active=True, archived=False):
    return Team(
        id="team-monitoring",
        club_id="club-monitoring",
        internal_name="monitoring",
        display_name="Monitoring",
        short_name="MON",
        slug="monitoring",
        club="Monitoring Club",
        active=active,
        archived_at=NOW if archived else None,
        fussball_url="https://www.fussball.de/mannschaft/monitoring",
        media_subdir="monitoring",
        rules={
            "automatic_sync_enabled": enabled,
            "sync_interval_hours": interval_hours,
        },
    )


def _state(
    *,
    status="idle",
    next_poll_at=None,
    last_success_at=None,
    lease_expires_at=None,
    created_at=None,
    last_completed_at=None,
    consecutive_failures=0,
    last_error=None,
):
    return FussballSyncState(
        team_id="team-monitoring",
        club_id="club-monitoring",
        status=status,
        next_poll_at=next_poll_at or NOW + timedelta(hours=1),
        last_success_at=last_success_at,
        lease_expires_at=lease_expires_at,
        created_at=created_at or NOW - timedelta(minutes=10),
        last_completed_at=last_completed_at,
        consecutive_failures=consecutive_failures,
        last_error=last_error,
    )


def _reason(state, team=None, settings=None, *, now=NOW):
    return _fussball_sync_stale_reason(
        state,
        team or _team(),
        settings or Settings(fussball_sync_error_backoff_seconds=300),
        now=now,
    )


def test_default_daily_sync_is_healthy_after_ninety_minutes():
    state = _state(
        last_success_at=NOW - timedelta(minutes=90),
        next_poll_at=NOW + timedelta(hours=22, minutes=30),
    )
    assert _reason(state) is None


def test_daily_sync_is_stale_after_interval_and_grace():
    state = _state(
        last_success_at=NOW - timedelta(hours=24, minutes=5, seconds=1),
        next_poll_at=NOW - timedelta(minutes=5, seconds=1),
    )
    assert _reason(state) == "poll_overdue"


@pytest.mark.parametrize("interval_hours", [1, 24, 168])
def test_team_specific_sync_intervals_control_long_term_freshness(interval_hours):
    team = _team(interval_hours=interval_hours)
    healthy = _state(
        last_success_at=NOW - timedelta(hours=interval_hours, minutes=4),
        next_poll_at=NOW + timedelta(minutes=1),
    )
    stale = _state(
        last_success_at=NOW - timedelta(hours=interval_hours, minutes=5, seconds=1),
        next_poll_at=NOW + timedelta(minutes=1),
    )
    assert _reason(healthy, team) is None
    assert _reason(stale, team) == "success_overdue"


def test_future_next_poll_is_healthy():
    assert (
        _reason(
            _state(
                last_success_at=NOW - timedelta(hours=2),
                next_poll_at=NOW + timedelta(hours=22),
            )
        )
        is None
    )


def test_overdue_next_poll_is_stale_after_grace():
    state = _state(
        last_success_at=NOW - timedelta(minutes=30),
        next_poll_at=NOW - timedelta(minutes=5, seconds=1),
    )
    assert _reason(state) == "poll_overdue"


def test_grace_is_at_least_sixty_seconds():
    settings = Settings(fussball_sync_error_backoff_seconds=30)
    within_grace = _state(
        last_success_at=NOW - timedelta(minutes=30),
        next_poll_at=NOW - timedelta(seconds=60),
    )
    beyond_grace = _state(
        last_success_at=NOW - timedelta(minutes=30),
        next_poll_at=NOW - timedelta(seconds=61),
    )
    assert _reason(within_grace, settings=settings) is None
    assert _reason(beyond_grace, settings=settings) == "poll_overdue"


def test_short_matchday_poll_uses_persisted_next_poll():
    healthy = _state(
        last_success_at=NOW - timedelta(minutes=14),
        next_poll_at=NOW + timedelta(minutes=1),
    )
    overdue = _state(
        last_success_at=NOW - timedelta(minutes=21),
        next_poll_at=NOW - timedelta(minutes=6),
    )
    assert _reason(healthy) is None
    assert _reason(overdue) == "poll_overdue"


def test_running_sync_with_valid_lease_is_healthy():
    state = _state(
        status="running",
        last_success_at=NOW - timedelta(days=2),
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    assert _reason(state) is None


def test_running_sync_with_expired_lease_is_stale():
    state = _state(status="running", lease_expires_at=NOW - timedelta(seconds=1))
    assert _reason(state) == "lease_expired"


def test_running_sync_without_lease_is_stale():
    assert _reason(_state(status="running")) == "lease_missing"


def test_transient_error_with_future_retry_is_healthy():
    state = _state(
        status="error",
        last_success_at=NOW - timedelta(hours=2),
        next_poll_at=NOW + timedelta(minutes=5),
    )
    assert _reason(state) is None


def test_repeated_errors_eventually_fail_long_term_freshness():
    state = _state(
        status="error",
        last_success_at=NOW - timedelta(hours=24, minutes=6),
        next_poll_at=NOW + timedelta(minutes=5),
    )
    assert _reason(state) == "success_overdue"


def test_initial_state_without_success_gets_interval_and_grace():
    initial = _state(
        last_success_at=None,
        created_at=NOW - timedelta(minutes=10),
        next_poll_at=NOW + timedelta(minutes=1),
    )
    abandoned = _state(
        last_success_at=None,
        created_at=NOW - timedelta(hours=24, minutes=6),
        next_poll_at=NOW + timedelta(minutes=1),
    )
    assert _reason(initial) is None
    assert _reason(abandoned) == "success_overdue"


def test_retry_scheduled_is_derived_only_from_future_next_poll():
    assert fussball_retry_scheduled(
        _state(next_poll_at=NOW + timedelta(seconds=1)),
        now=NOW,
    )
    assert not fussball_retry_scheduled(_state(next_poll_at=NOW), now=NOW)
    assert not fussball_retry_scheduled(
        _state(next_poll_at=NOW - timedelta(seconds=1)),
        now=NOW,
    )


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("Nur öffentliche HTTPS-URLs von FUSSBALL.DE sind erlaubt", "invalid_source"),
        (
            "FUSSBALL.DE nicht erreichbar: Client error '503 Service Unavailable' "
            "for url 'https://provider.invalid/?token=secret'",
            "upstream_http",
        ),
        ("FUSSBALL.DE nicht erreichbar: ConnectError private-host", "upstream_network"),
        ("Keine Spiele erkannt; HTML-Struktur oder Pflichtfelder prüfen", "parser_structure"),
        (
            "FUSSBALL.DE nicht erreichbar: FUSSBALL.DE-Antwort überschreitet das Größenlimit",
            "response_limit",
        ),
        ("Ungültiger Snapshot-Pfad", "snapshot_storage"),
        ("Unbekannte FUSSBALL.DE-AJAX-Ressource", "provider_error"),
    ],
)
def test_known_fussball_provider_errors_use_fixed_categories(message, category):
    assert classify_fussball_provider_error(message) == category


def test_unknown_fussball_provider_error_is_not_copied():
    raw = "token=secret https://provider.invalid/team/internal-id"
    category = classify_fussball_provider_error(raw)

    assert category == "unknown"
    assert raw not in category


def test_social_connection_health_reports_each_applicable_reason():
    healthy = SocialChannelConnection(
        status="connected",
        last_success_at=NOW - timedelta(minutes=5),
    )
    multiple = SocialChannelConnection(status="disrupted", last_success_at=None)
    stale = SocialChannelConnection(
        status="connected",
        last_success_at=NOW - timedelta(days=2),
    )

    assert (
        social_connection_health_reasons(
            healthy,
            stale_before=NOW - timedelta(days=1),
        )
        == ()
    )
    assert social_connection_health_reasons(
        multiple,
        stale_before=NOW - timedelta(days=1),
    ) == ("non_connected", "missing_last_success")
    assert social_connection_health_reasons(
        stale,
        stale_before=NOW - timedelta(days=1),
    ) == ("stale_last_success",)


def _persist_sync(
    db,
    *,
    next_poll_at,
    last_success_at,
    enabled=True,
    active=True,
    archived=False,
    team_id="team-persisted",
    display_name="Persisted Team",
    short_name="PST",
    status="idle",
    last_completed_at=None,
    consecutive_failures=0,
    last_error=None,
):
    team = _team(enabled=enabled, active=active, archived=archived)
    team.id = team_id
    team.club_id = db.info["test_club_id"]
    team.slug = team_id
    team.display_name = display_name
    team.short_name = short_name
    db.add(team)
    db.flush()
    state = FussballSyncState(
        team_id=team.id,
        club_id=team.club_id,
        status=status,
        next_poll_at=next_poll_at,
        last_success_at=last_success_at,
        created_at=last_success_at,
        last_completed_at=last_completed_at,
        consecutive_failures=consecutive_failures,
        last_error=last_error,
    )
    db.add(state)
    db.commit()
    return team, state


def _status_settings(tmp_path, *, enabled=True):
    media_root = tmp_path / "media"
    generated_root = tmp_path / "generated"
    log_root = tmp_path / "logs"
    backup_root = tmp_path / "backups"
    for path in (media_root, generated_root, log_root, backup_root):
        path.mkdir()
    (log_root / "worker-heartbeat.json").write_text(
        json.dumps(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "loops": 1,
                "scheduler": False,
                "automatic_scheduler": False,
                "automatic_fussball_sync": enabled,
                "automatic_post_generation": False,
            }
        )
    )
    return Settings(
        environment="production",
        publisher_mode="instagram",
        meta_production_enabled=True,
        fussball_automatic_sync_enabled=enabled,
        media_root=media_root,
        generated_root=generated_root,
        log_root=log_root,
        backup_root=backup_root,
    )


@pytest.mark.parametrize(
    ("enabled", "active", "archived"),
    [(False, True, False), (True, False, False), (True, True, True)],
)
def test_disabled_inactive_or_archived_team_is_ignored(db, tmp_path, enabled, active, archived):
    _persist_sync(
        db,
        enabled=enabled,
        active=active,
        archived=archived,
        next_poll_at=NOW - timedelta(days=2),
        last_success_at=NOW - timedelta(days=3),
    )
    report = system_status(db, _status_settings(tmp_path))
    assert report["checks"]["fussball_automatic"]["ok"] is True
    assert report["checks"]["fussball_automatic"]["detail"]["enabled_teams"] == 0


def test_globally_disabled_sync_is_never_critical(db, tmp_path):
    _persist_sync(
        db,
        next_poll_at=NOW - timedelta(days=2),
        last_success_at=NOW - timedelta(days=3),
    )
    report = system_status(db, _status_settings(tmp_path, enabled=False))
    assert report["checks"]["fussball_automatic"]["ok"] is True
    assert report["checks"]["fussball_automatic"]["detail"]["stale"] == 0


def test_health_details_lists_only_unhealthy_teams_without_ids_urls_or_raw_errors(db, tmp_path):
    now = datetime.now(timezone.utc)
    _persist_sync(
        db,
        team_id="healthy-internal-id",
        display_name="Gesunde Mannschaft",
        short_name="OK",
        next_poll_at=now + timedelta(hours=23),
        last_success_at=now - timedelta(hours=1),
    )
    _persist_sync(
        db,
        team_id="unhealthy-internal-id",
        display_name="A-Jugend",
        short_name="U19",
        status="error",
        next_poll_at=now + timedelta(minutes=30),
        last_success_at=now - timedelta(hours=25),
        last_completed_at=now - timedelta(minutes=1),
        consecutive_failures=9,
        last_error=(
            "FUSSBALL.DE nicht erreichbar: Client error '503 Service Unavailable' "
            "for url 'https://provider.invalid/team/internal-id?token=secret'"
        ),
    )

    with system_scope("Sanitizierte Teamursachen testen"):
        payload = collect_health_details(db, _status_settings(tmp_path))

    teams = payload["checks"]["fussball_automatic"]["detail"]["unhealthy_teams"]
    assert teams == [
        {
            "display_name": "A-Jugend",
            "short_name": "U19",
            "status": "error",
            "stale_reason": "success_overdue",
            "sync_interval_hours": 24,
            "consecutive_failures": 9,
            "last_success_at": (now - timedelta(hours=25)).isoformat(),
            "last_completed_at": (now - timedelta(minutes=1)).isoformat(),
            "next_poll_at": (now + timedelta(minutes=30)).isoformat(),
            "retry_scheduled": True,
            "error_category": "upstream_http",
        }
    ]
    serialized = json.dumps(payload)
    for forbidden in (
        "healthy-internal-id",
        "unhealthy-internal-id",
        "provider.invalid",
        "token=secret",
        "503 Service Unavailable",
    ):
        assert forbidden not in serialized


def test_health_details_reports_no_future_retry_as_false(db, tmp_path):
    now = datetime.now(timezone.utc)
    _persist_sync(
        db,
        status="error",
        next_poll_at=now - timedelta(minutes=10),
        last_success_at=now - timedelta(hours=25),
        consecutive_failures=2,
        last_error="arbitrary private provider response token=secret",
    )

    with system_scope("Sanitisierten Retry-Status testen"):
        payload = collect_health_details(db, _status_settings(tmp_path))

    team = payload["checks"]["fussball_automatic"]["detail"]["unhealthy_teams"][0]
    assert team["retry_scheduled"] is False
    assert team["error_category"] == "unknown"
    assert "arbitrary private provider response" not in json.dumps(payload)


def test_social_health_details_are_separated_and_share_reason_criteria(db, tmp_path):
    now = datetime.now(timezone.utc)
    common = {
        "club_id": db.info["test_club_id"],
        "active": True,
        "publishing_enabled": True,
    }
    db.add_all(
        [
            SocialChannelConnection(
                **common,
                channel_type="instagram",
                internal_name="instagram",
                display_name="Instagram",
                status="connected",
                last_check_at=now - timedelta(minutes=2),
                last_success_at=now - timedelta(minutes=2),
            ),
            SocialChannelConnection(
                **common,
                channel_type="facebook",
                internal_name="facebook",
                display_name="Facebook",
                status="disrupted",
                last_check_at=now - timedelta(minutes=3),
                last_success_at=None,
            ),
            SocialChannelConnection(
                **common,
                channel_type="whatsapp",
                internal_name="whatsapp",
                display_name="WhatsApp",
                status="connected",
                last_check_at=now - timedelta(days=2),
                last_success_at=now - timedelta(days=2),
            ),
        ]
    )
    db.commit()

    with system_scope("Sanitizierte Kanalursachen testen"):
        payload = collect_health_details(db, _status_settings(tmp_path, enabled=False))

    detail = payload["checks"]["social_media_channels"]["detail"]
    assert set(detail) == {"instagram", "facebook", "whatsapp"}
    assert detail["instagram"]["unhealthy_connections"] == 0
    assert detail["instagram"]["status_counts"] == {"connected": 1}
    assert detail["facebook"]["unhealthy_connections"] == 1
    assert detail["facebook"]["non_connected_connections"] == 1
    assert detail["facebook"]["missing_last_success"] == 1
    assert detail["facebook"]["stale_last_success"] == 0
    assert detail["facebook"]["status_counts"] == {"disrupted": 1}
    assert detail["whatsapp"]["unhealthy_connections"] == 1
    assert detail["whatsapp"]["non_connected_connections"] == 0
    assert detail["whatsapp"]["missing_last_success"] == 0
    assert detail["whatsapp"]["stale_last_success"] == 1
    assert payload["checks"]["social_media_channels"]["ok"] is False


def test_unknown_social_status_is_counted_without_exposing_raw_text(db, tmp_path):
    now = datetime.now(timezone.utc)
    db.add(
        SocialChannelConnection(
            club_id=db.info["test_club_id"],
            channel_type="facebook",
            internal_name="facebook-unknown",
            display_name="Facebook",
            status="token=private-status",
            active=True,
            publishing_enabled=True,
            last_check_at=now,
            last_success_at=now,
        )
    )
    db.commit()

    with system_scope("Unbekannten Kanalstatus sanitizen"):
        payload = collect_health_details(db, _status_settings(tmp_path, enabled=False))

    facebook = payload["checks"]["social_media_channels"]["detail"]["facebook"]
    assert facebook["unhealthy_connections"] == 1
    assert facebook["non_connected_connections"] == 1
    assert facebook["status_counts"] == {"unknown": 1}
    assert "private-status" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("overdue", "expected_status"),
    [(False, "ok"), (True, "degraded")],
)
def test_health_reflects_actual_sync_schedule(db, tmp_path, monkeypatch, overdue, expected_status):
    now = datetime.now(timezone.utc)
    _persist_sync(
        db,
        next_poll_at=(
            now - timedelta(minutes=6) if overdue else now + timedelta(hours=22, minutes=30)
        ),
        last_success_at=now - timedelta(minutes=90),
    )
    settings = _status_settings(tmp_path)
    for name in (
        "environment",
        "publisher_mode",
        "meta_production_enabled",
        "global_publish_enabled",
        "meta_scheduler_enabled",
        "meta_automatic_publish_enabled",
        "fussball_automatic_sync_enabled",
        "automatic_post_generation_enabled",
        "media_root",
        "generated_root",
        "log_root",
        "backup_root",
    ):
        monkeypatch.setattr(main.settings, name, getattr(settings, name))

    def override_db():
        yield db

    main.app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(main.app) as client:
            response = client.get("/health")
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == expected_status
