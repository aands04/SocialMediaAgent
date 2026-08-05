import re
from datetime import datetime, timedelta, timezone

from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountType, Role, Team, User, UserTeam
from app.tenancy.state import system_scope

passwords = PasswordHash.recommended()
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256
MAX_EMAIL_LENGTH = 254
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PERMISSIONS = {
    Role.ADMIN: {"*"},
    Role.APPROVER: {"view", "edit_post", "generate", "approve", "publish_retry"},
    Role.EDITOR: {"view", "edit_post", "generate"},
    Role.REVIEWER: {"view", "approve", "publish_retry"},
    Role.VIEWER: {"view"},
}
PLATFORM_PERMISSIONS = {
    "platform_view",
    "platform_manage_clubs",
    "platform_manage_users",
    "platform_manage_plans",
    "platform_manage_prompts",
    "platform_manage_usage",
    "platform_manage_features",
}


def hash_password(value: str) -> str:
    return passwords.hash(value)


def verify_password(value: str, hashed: str) -> bool:
    return passwords.verify(value, hashed)


def validate_new_password(value: str) -> str | None:
    if len(value) < MIN_PASSWORD_LENGTH:
        return f"Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen haben"
    if len(value) > MAX_PASSWORD_LENGTH:
        return f"Passwort darf höchstens {MAX_PASSWORD_LENGTH} Zeichen haben"
    return None


def normalize_email(value: str) -> str:
    normalized = value.strip().casefold()
    if (
        not normalized
        or len(normalized) > MAX_EMAIL_LENGTH
        or not _EMAIL_PATTERN.fullmatch(normalized)
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError("Bitte eine gültige E-Mail-Adresse eingeben")
    return normalized


def authenticate(db: Session, email: str, password: str) -> User | None:
    try:
        normalized_email = normalize_email(email)
    except ValueError:
        return None
    # Before authentication succeeds there cannot be a trusted tenant actor:
    # the account (and therefore its club) is only known after the global,
    # unique e-mail lookup. Keep the narrowly scoped authentication lookup
    # and its lockout-state update in one explicit system scope. Otherwise
    # the tenant write guard rejects the commit after the lookup scope ends.
    with system_scope("Anmeldedaten prüfen und Sperrstatus aktualisieren"):
        user = db.scalar(
            select(User).where(User.email == normalized_email, User.archived_at.is_(None))
        )
        now = datetime.now(timezone.utc)
        if not user or not user.active or (user.locked_until and user.locked_until > now):
            return None
        if not verify_password(password, user.password_hash):
            user.failed_logins += 1
            if user.failed_logins >= 5:
                user.locked_until = now + timedelta(minutes=15)
            db.commit()
            return None
        user.failed_logins = 0
        user.locked_until = None
        db.commit()
        return user


def allowed(db: Session, user: User, permission: str, team_id: str | None = None) -> bool:
    if user.account_type == AccountType.PLATFORM_ADMIN:
        return user.club_id is None and permission in PLATFORM_PERMISSIONS
    if user.account_type != AccountType.CLUB_USER or not user.club_id:
        return False
    if permission not in PERMISSIONS[user.role] and "*" not in PERMISSIONS[user.role]:
        return False
    if team_id:
        team = db.get(Team, team_id)
        if team is None or team.club_id != user.club_id:
            return False
        if not user.all_teams:
            assignment = db.get(UserTeam, {"user_id": user.id, "team_id": team_id})
            return assignment is not None and assignment.club_id == user.club_id
    return True
