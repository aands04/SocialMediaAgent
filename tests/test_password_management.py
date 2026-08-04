import hashlib
import re
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.auth.service import hash_password, verify_password
from app.db import Base, get_db
from app.main import app
from app.models import AuditLog, PasswordResetToken, Role, User


@pytest.fixture
def password_browser(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'passwords.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        db.add(
            User(
                email="account@test.invalid",
                password_hash=hash_password("Current-Secure-Password"),
                role=Role.ADMIN,
                all_teams=True,
            )
        )
        db.commit()

    def override():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override
    with TestClient(app) as client:
        yield client, factory, monkeypatch
    app.dependency_overrides.clear()


def _csrf(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def _login(client: TestClient, password: str = "Current-Secure-Password"):
    page = client.get("/login")
    return client.post(
        "/login",
        data={
            "email": "account@test.invalid",
            "password": password,
            "csrf_token": _csrf(page),
        },
        follow_redirects=False,
    )


def test_signed_in_identity_and_password_change_invalidates_session(password_browser):
    client, factory, _ = password_browser
    assert _login(client).status_code == 303
    dashboard = client.get("/")
    assert "Eingeloggt als" in dashboard.text
    assert "account@test.invalid" in dashboard.text
    assert "Administrator" in dashboard.text

    page = client.get("/account/password")
    response = client.post(
        "/account/password",
        data={
            "current_password": "Current-Secure-Password",
            "password": "New-Secure-Password-2026",
            "password_confirmation": "New-Secure-Password-2026",
            "csrf_token": _csrf(page),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    login_notice = client.get(response.headers["location"])
    assert "Passwort erfolgreich geändert" in login_notice.text
    assert client.get("/").status_code == 401

    with factory() as db:
        user = db.scalar(select(User).where(User.email == "account@test.invalid"))
        assert user.auth_version == 2
        assert verify_password("New-Secure-Password-2026", user.password_hash)
        assert db.scalar(select(AuditLog).where(AuditLog.action == "password.changed"))

    assert _login(client).status_code == 401
    assert _login(client, "New-Secure-Password-2026").status_code == 303


def test_password_reset_is_hashed_single_use_and_does_not_reveal_accounts(password_browser):
    client, factory, monkeypatch = password_browser
    import app.auth.password_reset as password_reset
    import app.main as main

    delivered = []
    monkeypatch.setattr(main.settings, "password_reset_enabled", True)
    monkeypatch.setattr(main.settings, "app_public_base_url", "https://social.example.test")
    monkeypatch.setitem(main.templates.env.globals, "password_reset_enabled", True)
    monkeypatch.setattr(
        password_reset,
        "_send_reset_email",
        lambda _settings, recipient, reset_url: delivered.append((recipient, reset_url)),
    )

    page = client.get("/password/forgot")
    response = client.post(
        "/password/forgot",
        data={"email": "account@test.invalid", "csrf_token": _csrf(page)},
    )
    assert response.status_code == 200
    assert password_reset.GENERIC_RESET_MESSAGE in response.text
    assert len(delivered) == 1

    raw_token = urlparse(delivered[0][1]).path.rsplit("/", 1)[-1]
    with factory() as db:
        item = db.scalar(select(PasswordResetToken))
        assert item.token_hash == hashlib.sha256(raw_token.encode()).hexdigest()
        assert raw_token not in item.token_hash
        assert item.delivery_status == "sent"

    reset_page = client.get(f"/password/reset/{raw_token}")
    completed = client.post(
        f"/password/reset/{raw_token}",
        data={
            "password": "Reset-Secure-Password-2026",
            "password_confirmation": "Reset-Secure-Password-2026",
            "csrf_token": _csrf(reset_page),
        },
        follow_redirects=False,
    )
    assert completed.status_code == 303
    assert client.get(f"/password/reset/{raw_token}").status_code == 400

    with factory() as db:
        item = db.scalar(select(PasswordResetToken))
        user = db.scalar(select(User).where(User.email == "account@test.invalid"))
        assert item.used_at is not None
        assert user.auth_version == 2
        assert verify_password("Reset-Secure-Password-2026", user.password_hash)
        assert db.scalar(
            select(AuditLog).where(AuditLog.action == "password.reset_completed")
        )

    unknown_page = client.get("/password/forgot")
    unknown = client.post(
        "/password/forgot",
        data={"email": "unknown@test.invalid", "csrf_token": _csrf(unknown_page)},
    )
    assert unknown.status_code == 200
    assert password_reset.GENERIC_RESET_MESSAGE in unknown.text
    assert "unbekannt" not in unknown.text.lower()
    assert len(delivered) == 1
