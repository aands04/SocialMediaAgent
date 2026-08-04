import hashlib
import secrets
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urlparse

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth.service import normalize_email
from app.config import Settings
from app.models import AuditLog, EmailChangeToken, PasswordResetToken, User


@dataclass(frozen=True)
class EmailChangeRequestResult:
    delivered: bool
    error: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _clean_header(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()


def _public_base_url(settings: Settings) -> str:
    value = (settings.app_public_base_url or "").rstrip("/")
    parsed = urlparse(value)
    if not value or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("APP_PUBLIC_BASE_URL ist nicht gültig konfiguriert")
    if settings.environment == "production" and parsed.scheme != "https":
        raise RuntimeError("APP_PUBLIC_BASE_URL muss in Produktion HTTPS verwenden")
    if parsed.username or parsed.password:
        raise RuntimeError("APP_PUBLIC_BASE_URL darf keine Zugangsdaten enthalten")
    return value


def _send_email_change_confirmation(
    settings: Settings,
    recipient: str,
    new_email: str,
    confirmation_url: str,
) -> None:
    if not settings.smtp_host or not settings.smtp_from_email:
        raise RuntimeError("SMTP ist nicht vollständig konfiguriert")
    if settings.smtp_starttls and settings.smtp_use_ssl:
        raise RuntimeError("SMTP_STARTTLS und SMTP_USE_SSL dürfen nicht gleichzeitig aktiv sein")
    if settings.smtp_username and settings.smtp_password is None:
        raise RuntimeError("SMTP-Passwort fehlt")

    message = EmailMessage()
    message["Subject"] = "E-Mail-Adresse der Vereinszentrale bestätigen"
    message["From"] = (
        f"{_clean_header(settings.smtp_from_name)} "
        f"<{_clean_header(settings.smtp_from_email)}>"
    )
    message["To"] = _clean_header(recipient)
    message.set_content(
        "Für dein Konto wurde die Änderung der E-Mail-Adresse angefordert.\n\n"
        f"Neue E-Mail-Adresse: {new_email}\n"
        f"Änderung bestätigen: {confirmation_url}\n\n"
        f"Der Link ist {settings.email_change_token_ttl_seconds // 60} Minuten gültig "
        "und kann nur einmal verwendet werden. Wenn du diese Änderung nicht angefordert "
        "hast, bestätige sie nicht und ändere vorsorglich dein Passwort."
    )

    smtp_class = smtplib.SMTP_SSL if settings.smtp_use_ssl else smtplib.SMTP
    kwargs = {
        "host": settings.smtp_host,
        "port": settings.smtp_port,
        "timeout": settings.smtp_timeout_seconds,
    }
    if settings.smtp_use_ssl:
        kwargs["context"] = ssl.create_default_context()
    with smtp_class(**kwargs) as client:
        if settings.smtp_starttls:
            client.starttls(context=ssl.create_default_context())
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password or "")
        client.send_message(message)


def request_email_change(
    db: Session,
    settings: Settings,
    user: User,
    new_email: str,
    requested_ip: str | None,
) -> EmailChangeRequestResult:
    normalized = normalize_email(new_email)
    if normalized == user.email:
        raise ValueError("Die neue E-Mail-Adresse entspricht der bisherigen Adresse")
    if db.scalar(select(User.id).where(User.email == normalized)) is not None:
        raise ValueError("Diese E-Mail-Adresse wird bereits verwendet")

    now = _now()
    db.execute(
        update(EmailChangeToken)
        .where(
            EmailChangeToken.user_id == user.id,
            EmailChangeToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    raw_token = secrets.token_urlsafe(32)
    item = EmailChangeToken(
        user_id=user.id,
        token_hash=_token_hash(raw_token),
        old_email=user.email,
        new_email=normalized,
        auth_version=user.auth_version,
        expires_at=now + timedelta(seconds=settings.email_change_token_ttl_seconds),
        requested_ip=requested_ip,
    )
    db.add(item)
    db.add(
        AuditLog(
            user_id=user.id,
            action="email.change_requested",
            entity_type="user",
            entity_id=user.id,
            details={"old_email": user.email, "new_email": normalized, "delivery": "pending"},
            ip=requested_ip,
        )
    )
    db.commit()

    try:
        confirmation_url = f"{_public_base_url(settings)}/account/email/confirm/{raw_token}"
        _send_email_change_confirmation(settings, user.email, normalized, confirmation_url)
    except Exception as exc:
        item.delivery_status = "failed"
        item.delivery_error = type(exc).__name__[:160]
        item.used_at = _now()
        db.add(
            AuditLog(
                user_id=user.id,
                action="email.change_delivery_failed",
                entity_type="user",
                entity_id=user.id,
                details={"error_type": type(exc).__name__},
                ip=requested_ip,
            )
        )
        db.commit()
        return EmailChangeRequestResult(
            delivered=False,
            error="Bestätigungs-E-Mail konnte nicht versendet werden. Die Adresse wurde nicht geändert.",
        )

    item.delivery_status = "sent"
    item.delivery_error = None
    db.commit()
    return EmailChangeRequestResult(delivered=True)


def find_valid_email_change_token(
    db: Session, raw_token: str
) -> EmailChangeToken | None:
    if len(raw_token) > 256:
        return None
    item = db.scalar(
        select(EmailChangeToken).where(
            EmailChangeToken.token_hash == _token_hash(raw_token),
            EmailChangeToken.used_at.is_(None),
        )
    )
    if item is None or _aware(item.expires_at) <= _now():
        return None
    user = db.get(User, item.user_id)
    if (
        user is None
        or not user.active
        or user.archived_at is not None
        or user.email != item.old_email
        or user.auth_version != item.auth_version
    ):
        return None
    return item


def complete_email_change(
    db: Session,
    item: EmailChangeToken,
    request_ip: str | None,
) -> User:
    now = _now()
    user = db.get(User, item.user_id)
    if (
        user is None
        or item.used_at is not None
        or _aware(item.expires_at) <= now
        or user.email != item.old_email
        or user.auth_version != item.auth_version
    ):
        raise ValueError("Bestätigungslink ist ungültig oder abgelaufen")
    if db.scalar(
        select(User.id).where(User.email == item.new_email, User.id != user.id)
    ) is not None:
        raise ValueError("Die neue E-Mail-Adresse wird inzwischen bereits verwendet")

    claimed = db.execute(
        update(EmailChangeToken)
        .where(
            EmailChangeToken.id == item.id,
            EmailChangeToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    if claimed.rowcount != 1:
        db.rollback()
        raise ValueError("Bestätigungslink wurde bereits verwendet")

    old_email = user.email
    user.email = item.new_email
    user.auth_version += 1
    db.execute(
        update(EmailChangeToken)
        .where(
            EmailChangeToken.user_id == user.id,
            EmailChangeToken.id != item.id,
            EmailChangeToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    db.add(
        AuditLog(
            user_id=user.id,
            action="email.change_completed",
            entity_type="user",
            entity_id=user.id,
            details={"old_email": old_email, "new_email": user.email},
            ip=request_ip,
        )
    )
    db.commit()
    return user
