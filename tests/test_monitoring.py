import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import Settings
from app.db import get_db
from app.models import FussballSyncState, Team
from app.monitoring.service import _fussball_sync_stale_reason, system_status

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
):
    return FussballSyncState(
        team_id="team-monitoring",
        club_id="club-monitoring",
        status=status,
        next_poll_at=next_poll_at or NOW + timedelta(hours=1),
        last_success_at=last_success_at,
        lease_expires_at=lease_expires_at,
        created_at=created_at or NOW - timedelta(minutes=10),
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


def _persist_sync(
    db,
    *,
    next_poll_at,
    last_success_at,
    enabled=True,
    active=True,
    archived=False,
):
    team = _team(enabled=enabled, active=active, archived=archived)
    team.id = "team-persisted"
    team.club_id = db.info["test_club_id"]
    team.slug = "team-persisted"
    db.add(team)
    db.flush()
    state = FussballSyncState(
        team_id=team.id,
        club_id=team.club_id,
        status="idle",
        next_poll_at=next_poll_at,
        last_success_at=last_success_at,
        created_at=last_success_at,
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
