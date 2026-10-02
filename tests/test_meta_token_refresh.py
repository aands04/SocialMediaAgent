from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from test_meta_integration import OAuthApi, make_context, production_settings

from app.meta.api import MetaApiClient, MetaApiError, OAuthToken
from app.meta.connection_health import run_automatic_connection_check_cycle
from app.meta.publishing import MetaPublishingError
from app.meta.security import TokenCipher
from app.models import AuditLog, SystemSetting


class RefreshApi(OAuthApi):
    def __init__(self):
        super().__init__()
        self.refresh_calls = 0
        self.fail = False

    def refresh_token(self, token, user_id):
        self.refresh_calls += 1
        if self.fail:
            raise MetaApiError("private-provider-error-token", retryable=True)
        return OAuthToken("new-secret-token", user_id, 60 * 86400)


def refresh_context(db, tmp_path, *, days=14):
    settings = production_settings(tmp_path)
    _, page, connection, post, job = make_context(db, settings)
    now = datetime.now(timezone.utc)
    connection.created_at = now - timedelta(days=46)
    connection.token_expires_at = now + timedelta(days=days)
    connection.last_check_at = now - timedelta(hours=13)
    page.last_check_at = connection.last_check_at
    db.commit()
    return settings, now, page, connection, post, job


def test_due_token_is_refreshed_without_changing_gates(db, tmp_path):
    settings, now, page, connection, post, job = refresh_context(db, tmp_path)
    page.publishing_enabled = False
    db.commit()
    snapshot = (post.status, post.approved_version, job.status, job.idempotency_key)
    api = RefreshApi()
    result = run_automatic_connection_check_cycle(db, settings, api=api, now=now)
    assert result.refreshed == result.succeeded == 1
    assert result.failed == 0
    assert api.refresh_calls == 1
    assert connection.token_expires_at == now + timedelta(days=60)
    assert (
        TokenCipher(settings.meta_token_encryption_key).decrypt(connection.encrypted_token)
        == "new-secret-token"
    )
    assert connection.encrypted_token != "new-secret-token"
    assert not page.publishing_enabled
    assert (post.status, post.approved_version, job.status, job.idempotency_key) == snapshot
    audit = db.scalar(select(AuditLog).where(AuditLog.action == "meta.token_refreshed_automatic"))
    assert audit.club_id == connection.club_id
    assert audit.user_id is None
    assert "secret" not in str(audit.details)
    run_automatic_connection_check_cycle(db, settings, api=api, now=now + timedelta(hours=13))
    assert api.refresh_calls == 1


@pytest.mark.parametrize("days", [15, 60])
def test_token_outside_refresh_window_is_only_checked(db, tmp_path, days):
    settings, now, _, connection, _, _ = refresh_context(db, tmp_path, days=days)
    token = connection.encrypted_token
    api = RefreshApi()
    result = run_automatic_connection_check_cycle(db, settings, api=api, now=now)
    assert result.succeeded == 1
    assert api.refresh_calls == result.refreshed == 0
    assert connection.encrypted_token == token


@pytest.mark.parametrize(
    "action", ["meta.oauth_completed", "meta.token_refreshed", "meta.token_refreshed_automatic"]
)
def test_recent_issuance_prevents_refresh(db, tmp_path, action):
    settings, now, _, connection, _, _ = refresh_context(db, tmp_path)
    db.add(
        AuditLog(
            action=action,
            entity_type="instagram_connection",
            entity_id=connection.id,
            at=now - timedelta(hours=23),
            details={},
        )
    )
    db.commit()
    api = RefreshApi()
    run_automatic_connection_check_cycle(db, settings, api=api, now=now)
    assert api.refresh_calls == 0


@pytest.mark.parametrize("age_hours,expected", [(23, 0), (24, 1)])
def test_creation_time_fallback_respects_minimum_age(db, tmp_path, age_hours, expected):
    settings, now, _, connection, _, _ = refresh_context(db, tmp_path)
    connection.created_at = now - timedelta(hours=age_hours)
    db.commit()
    api = RefreshApi()
    run_automatic_connection_check_cycle(db, settings, api=api, now=now)
    assert api.refresh_calls == expected


def test_unknown_expiry_does_not_trigger_refresh(db, tmp_path):
    settings, now, _, connection, _, _ = refresh_context(db, tmp_path)
    connection.token_expires_at = None
    db.commit()
    api = RefreshApi()
    run_automatic_connection_check_cycle(db, settings, api=api, now=now)
    assert api.refresh_calls == 0


def test_disabled_global_gate_prevents_all_calls(db, tmp_path):
    settings, now, _, _, _, _ = refresh_context(db, tmp_path)
    settings.global_publish_enabled = False
    api = RefreshApi()
    with pytest.raises(MetaPublishingError):
        run_automatic_connection_check_cycle(db, settings, api=api, now=now)
    assert api.refresh_calls == api.profile_calls == 0


def test_failed_refresh_keeps_token_and_retries_after_interval(db, tmp_path):
    settings, now, page, connection, _, _ = refresh_context(db, tmp_path)
    token, expiry = connection.encrypted_token, connection.token_expires_at
    api = RefreshApi()
    api.fail = True
    first = run_automatic_connection_check_cycle(db, settings, api=api, now=now)
    assert first.failed == first.refresh_failed == 1
    assert connection.encrypted_token == token
    assert connection.status == page.connection_status == "error"
    assert "private-provider" not in connection.last_error
    assert connection.token_expires_at.replace(tzinfo=timezone.utc) == expiry
    second = run_automatic_connection_check_cycle(
        db, settings, api=api, now=now + timedelta(hours=1)
    )
    assert second.claimed == 0
    api.fail = False
    third = run_automatic_connection_check_cycle(
        db, settings, api=api, now=now + timedelta(hours=13)
    )
    assert third.refreshed == 1
    assert api.refresh_calls == 2
    assert connection.status == page.connection_status == "connected"


@pytest.mark.parametrize("days", [0, -1])
def test_expired_token_requires_login_without_external_calls(db, tmp_path, days):
    settings, now, page, connection, _, _ = refresh_context(db, tmp_path, days=days)
    api = RefreshApi()
    result = run_automatic_connection_check_cycle(db, settings, api=api, now=now)
    assert result.failed == 1
    assert api.refresh_calls == api.profile_calls == 0
    assert "Instagram neu verbinden" in connection.last_error
    assert page.connection_status == "error"


@pytest.mark.parametrize(
    "blocked", ["emergency_stop", "inactive", "disconnected", "missing_scopes"]
)
def test_refresh_respects_stop_and_connection_gates(db, tmp_path, blocked):
    settings, now, page, connection, _, _ = refresh_context(db, tmp_path)
    if blocked == "emergency_stop":
        db.add(SystemSetting(key="emergency_stop", value={"enabled": True}))
    elif blocked == "inactive":
        page.active = False
    elif blocked == "disconnected":
        connection.disconnected_at = now
        connection.status = "disconnected"
    else:
        connection.scopes = []
    db.commit()
    api = RefreshApi()
    run_automatic_connection_check_cycle(db, settings, api=api, now=now)
    assert api.refresh_calls == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"access_token": "new-secret", "expires_in": 0},
        {"expires_in": 100},
        {"access_token": "new-secret", "expires_in": "invalid"},
    ],
)
def test_invalid_refresh_response_does_not_invent_new_expiry(tmp_path, payload):
    settings = production_settings(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    with pytest.raises(MetaApiError, match="keine gültige Tokenverlängerung"):
        MetaApiClient(settings, client).refresh_token("old-secret", "ig-user")
