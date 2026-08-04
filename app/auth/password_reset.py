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

from app.config import Settings
from app.models import AuditLog, PasswordResetToken, User

GENERIC_RESET_MESSAGE = (
    "Falls ein aktives Konto mit dieser E-Mail-Adresse existiert, wurde eine "
    "Nachricht mit den weiteren Schritten versendet."
)


@dataclass(frozen=True)
class ResetRequestResult:
    accepted: bool
    delivered: bool


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _clean_header(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()


def _reset_base_url(settings: Settings) -> str:
    value = (settings.app_public_base_url or "").rstrip("/")
    parsed = urlparse(value)
    if not value or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("APP_PUBLIC_BASE_URL ist nicht gültig konfiguriert")
    if settings.environment == "production" and parsed.scheme != "https":
        raise RuntimeError("APP_PUBLIC_BASE_URL muss in Produktion HTTPS verwenden")
    if parsed.username or parsed.password:
        raise RuntimeError("APP_PUBLIC_BASE_URL darf keine Zugangsdaten enthalten")
    return value


def _send_reset_email(settings: Settings, recipient: str, reset_url: str) -> None:
    if not settings.smtp_host or not settings.smtp_from_email:
        raise RuntimeError("SMTP ist nicht vollständig konfiguriert")
    if settings.smtp_starttls and settings.smtp_use_ssl:
        raise RuntimeError("SMTP_STARTTLS und SMTP_USE_SSL dürfen nicht gleichzeitig aktiv sein")
    if settings.smtp_username and settings.smtp_password is None:
        raise RuntimeError("SMTP-Passwort fehlt")

    message = EmailMessage()
    message["Subject"] = "Passwort für die Vereinszentrale zurücksetzen"
    message["From"] = (
        f"{_clean_header(settings.smtp_from_name)} "
        f"<{_clean_header(settings.smtp_from_email)}>"
    )
    message["To"] = _clean_header(recipient)
    message.set_content(
        "Für dein Konto wurde ein neues Passwort angefordert.\n\n"
        f"Passwort zurücksetzen: {reset_url}\n\n"
        f"Der Link ist {settings.password_reset_token_ttl_seconds // 60} Minuten gültig "
        "und kann nur einmal verwendet werden. Wenn du diese Anfrage nicht gestellt hast, "
        "kannst du diese Nachricht ignorieren."
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


def request_password_reset(
    db: Session,
    settings: Settings,
    email: str,
    requested_ip: str | None,
) -> ResetRequestResult:
    if not settings.password_reset_enabled:
        return ResetRequestResult(accepted=False, delivered=False)

    normalized_email = email.strip().lower()
    user = db.scalar(
        select(User).where(
            User.email == normalized_email,
            User.active.is_(True),
            User.archived_at.is_(None),
        )
    )
    if user is None:
        return ResetRequestResult(accepted=True, delivered=False)

    now = _now()
    latest = db.scalar(
        select(PasswordResetToken)
        .where(PasswordResetToken.user_id == user.id)
        .order_by(PasswordResetToken.created_at.desc())
        .limit(1)
    )
    if latest and _aware(latest.created_at) > now - timedelta(
        seconds=settings.password_reset_request_cooldown_seconds
    ):
        return ResetRequestResult(accepted=True, delivered=False)

    db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    raw_token = secrets.token_urlsafe(32)
    item = PasswordResetToken(
        user_id=user.id,
        token_hash=_token_hash(raw_token),
        expires_at=now + timedelta(seconds=settings.password_reset_token_ttl_seconds),
        requested_ip=requested_ip,
    )
    db.add(item)
    db.add(
        AuditLog(
            user_id=user.id,
            action="password.reset_requested",
            entity_type="user",
            entity_id=user.id,
            details={"delivery": "pending"},
            ip=requested_ip,
        )
    )
    db.commit()

    try:
        reset_url = f"{_reset_base_url(settings)}/password/reset/{raw_token}"
        _send_reset_email(settings, user.email, reset_url)
    except Exception as exc:
        item.delivery_status = "failed"
        item.delivery_error = type(exc).__name__[:160]
        item.used_at = _now()
        db.add(
            AuditLog(
                user_id=user.id,
                action="password.reset_delivery_failed",
                entity_type="user",
                entity_id=user.id,
                details={"error_type": type(exc).__name__},
                ip=requested_ip,
            )
        )
        db.commit()
        return ResetRequestResult(accepted=True, delivered=False)

    item.delivery_status = "sent"
    item.delivery_error = None
    db.commit()
    return ResetRequestResult(accepted=True, delivered=True)


def find_valid_reset_token(db: Session, raw_token: str) -> PasswordResetToken | None:
    if len(raw_token) > 256:
        return None
    item = db.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == _token_hash(raw_token),
            PasswordResetToken.used_at.is_(None),
        )
    )
    if item is None or _aware(item.expires_at) <= _now():
        return None
    user = db.get(User, item.user_id)
    if user is None or not user.active or user.archived_at is not None:
        return None
    return item


def complete_password_reset(
    db: Session,
    item: PasswordResetToken,
    password_hash: str,
    request_ip: str | None,
) -> User:
    now = _now()
    user = db.get(User, item.user_id)
    if user is None or item.used_at is not None or _aware(item.expires_at) <= now:
        raise ValueError("Reset-Link ist ungültig oder abgelaufen")
    claimed = db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.id == item.id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    if claimed.rowcount != 1:
        db.rollback()
        raise ValueError("Reset-Link ist ungültig oder wurde bereits verwendet")
    user.password_hash = password_hash
    user.auth_version += 1
    user.failed_logins = 0
    user.locked_until = None
    db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.id != item.id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    db.add(
        AuditLog(
            user_id=user.id,
            action="password.reset_completed",
            entity_type="user",
            entity_id=user.id,
            details={},
            ip=request_ip,
        )
    )
    db.commit()
    return user
