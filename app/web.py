import secrets
from collections.abc import AsyncGenerator
from datetime import timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth.service import allowed
from app.db import get_db
from app.models import AccountType, Club, ClubStatus, Role, User
from app.tenancy.state import activate_platform, activate_tenant, reset_scope, system_scope


def berlin_datetime(value, format_string="%d.%m.%Y, %H:%M Uhr") -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo("Europe/Berlin")).strftime(format_string)


def csrf_token(request: Request) -> str:
    return request.session.setdefault("csrf", secrets.token_urlsafe(32))


def check_csrf(request: Request, supplied: str) -> None:
    if not secrets.compare_digest(supplied, request.session.get("csrf", "")):
        raise HTTPException(403, "CSRF-Prüfung fehlgeschlagen")


async def optional_current_user(
    request: Request, db: Session = Depends(get_db)
) -> AsyncGenerator[User | None, None]:
    user_id = request.session.get("uid")
    with system_scope("Sitzung authentifizieren"):
        current = db.get(User, user_id) if user_id else None
    session_auth_version = request.session.get("auth_version")
    if (
        not current
        or not current.active
        or current.archived_at
        or session_auth_version != current.auth_version
        or request.session.get("account_type") != current.account_type.value
        or request.session.get("club_id") != current.club_id
        or (current.account_type == AccountType.CLUB_USER and current.club_id is None)
        or (current.account_type == AccountType.PLATFORM_ADMIN and current.club_id is not None)
    ):
        request.session.clear()
        yield None
        return
    token = (
        activate_platform(current.id)
        if current.account_type == AccountType.PLATFORM_ADMIN
        else activate_tenant(current.club_id, current.id)
    )
    try:
        if (
            current.account_type == AccountType.CLUB_USER
            and request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
            and request.url.path
            not in {
                "/logout",
                "/account/password",
                "/account/email",
            }
        ):
            club = db.get(Club, current.club_id)
            if club is None:
                raise HTTPException(403, "Der zugeordnete Verein ist nicht vorhanden")
            if club.status not in {
                ClubStatus.ACTIVE,
                ClubStatus.TRIAL,
                ClubStatus.SETUP_PENDING,
            }:
                raise HTTPException(
                    403,
                    "Der Verein ist derzeit gesperrt. Schreibende Aktionen sind nicht möglich.",
                )
        yield current
    finally:
        reset_scope(token)


async def current_user(
    current: User | None = Depends(optional_current_user),
) -> User:
    if current is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)
    return current


def require(current: User, db: Session, permission: str, team_id: str | None = None) -> None:
    if not allowed(db, current, permission, team_id):
        raise HTTPException(403, "Keine Berechtigung für diese Aktion")


def require_platform_admin(current: User) -> None:
    if current.account_type != AccountType.PLATFORM_ADMIN or current.club_id is not None:
        raise HTTPException(403, "PlatformAdmin-Berechtigung erforderlich")


def require_admin(current: User) -> None:
    if current.account_type != AccountType.CLUB_USER or current.role != Role.ADMIN:
        raise HTTPException(403, "Nur Administratoren dürfen diese Aktion ausführen")
