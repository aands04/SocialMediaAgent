import hashlib
import re
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.auth.service import hash_password, validate_new_password, verify_password
from app.db import Base, get_db
from app.main import app
from app.models import AuditLog, EmailChangeToken, PasswordResetToken, Role, User


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


def _login(
    client: TestClient,
    password: str = "Current-Secure-Password",
    email: str = "account@test.invalid",
):
    page = client.get("/login")
    return client.post(
        "/login",
        data={
            "email": email,
            "password": password,
            "csrf_token": _csrf(page),
        },
        follow_redirects=False,
    )


def test_signed_in_identity_and_password_change_invalidates_session(password_browser):
    client, factory, _ = password_browser
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 303
    assert root.headers["location"] == "/login"
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
    assert client.get("/", follow_redirects=False).status_code == 303

    with factory() as db:
        user = db.scalar(select(User).where(User.email == "account@test.invalid"))
        assert user.auth_version == 2
        assert verify_password("New-Secure-Password-2026", user.password_hash)
        assert db.scalar(select(AuditLog).where(AuditLog.action == "password.changed"))

    assert _login(client).status_code == 401
    assert _login(client, "New-Secure-Password-2026").status_code == 303


def test_minimum_password_length_is_eight_characters():
    assert validate_new_password("1234567") == "Passwort muss mindestens 8 Zeichen haben"
    assert validate_new_password("12345678") is None


def test_registration_requires_admin_approval(password_browser):
    client, factory, _ = password_browser
    page = client.get("/register")
    assert "erst nach Prüfung" in page.text
    too_short = client.post(
        "/register",
        data={
            "email": "member@test.invalid",
            "password": "1234567",
            "password_confirmation": "1234567",
            "csrf_token": _csrf(page),
        },
    )
    assert too_short.status_code == 422

    page = client.get("/register")
    created = client.post(
        "/register",
        data={
            "email": "Member@Test.Invalid",
            "password": "12345678",
            "password_confirmation": "12345678",
            "csrf_token": _csrf(page),
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    with factory() as db:
        member = db.scalar(select(User).where(User.email == "member@test.invalid"))
        assert member is not None
        assert member.registration_status == "pending"
        assert member.active is False
        assert member.role == Role.VIEWER
        member_id = member.id
        assert db.scalar(select(AuditLog).where(AuditLog.action == "registration.requested"))

    assert _login(client, "12345678", "member@test.invalid").status_code == 401
    assert _login(client).status_code == 303
    users_page = client.get("/users")
    assert "wartet auf Freigabe" in users_page.text
    approved = client.post(
        f"/users/{member_id}/registration",
        data={"csrf_token": _csrf(users_page), "action": "approve"},
        follow_redirects=False,
    )
    assert approved.status_code == 303
    client.post("/logout")
    assert _login(client, "12345678", "member@test.invalid").status_code == 303
    with factory() as db:
        member = db.get(User, member_id)
        assert member.active is True
        assert member.registration_status == "approved"
        assert db.scalar(select(AuditLog).where(AuditLog.action == "registration.approved"))


def test_email_change_is_confirmed_via_old_address_and_invalidates_session(
    password_browser,
):
    client, factory, monkeypatch = password_browser
    import app.auth.email_change as email_change
    import app.main as main

    delivered = []
    monkeypatch.setattr(main.settings, "app_public_base_url", "https://social.example.test")
    monkeypatch.setattr(
        email_change,
        "_send_email_change_confirmation",
        lambda _settings, recipient, new_email, confirmation_url: delivered.append(
            (recipient, new_email, confirmation_url)
        ),
    )
    assert _login(client).status_code == 303
    page = client.get("/account/email")
    requested = client.post(
        "/account/email",
        data={
            "current_password": "Current-Secure-Password",
            "new_email": "New-Account@Test.Invalid",
            "csrf_token": _csrf(page),
        },
    )
    assert requested.status_code == 200
    assert "bisherige E-Mail-Adresse" in requested.text
    assert delivered[0][0] == "account@test.invalid"
    assert delivered[0][1] == "new-account@test.invalid"
    raw_token = urlparse(delivered[0][2]).path.rsplit("/", 1)[-1]

    with factory() as db:
        item = db.scalar(select(EmailChangeToken))
        user = db.scalar(select(User).where(User.email == "account@test.invalid"))
        assert item.token_hash == hashlib.sha256(raw_token.encode()).hexdigest()
        assert raw_token not in item.token_hash
        assert item.delivery_status == "sent"
        assert user is not None

    confirmation_page = client.get(f"/account/email/confirm/{raw_token}")
    assert confirmation_page.status_code == 200
    with factory() as db:
        assert db.scalar(select(User).where(User.email == "account@test.invalid"))
    confirmed = client.post(
        f"/account/email/confirm/{raw_token}",
        data={"csrf_token": _csrf(confirmation_page)},
        follow_redirects=False,
    )
    assert confirmed.status_code == 303
    assert client.get(f"/account/email/confirm/{raw_token}").status_code == 400
    assert _login(client).status_code == 401
    assert _login(client, email="new-account@test.invalid").status_code == 303

    with factory() as db:
        user = db.scalar(select(User).where(User.email == "new-account@test.invalid"))
        item = db.scalar(select(EmailChangeToken))
        assert user.auth_version == 2
        assert item.used_at is not None
        assert db.scalar(select(AuditLog).where(AuditLog.action == "email.change_completed"))


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
