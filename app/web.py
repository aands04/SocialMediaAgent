import secrets
from datetime import timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth.service import allowed
from app.db import get_db
from app.models import Role, User


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

def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("uid")
    current = db.get(User, user_id) if user_id else None
    session_auth_version = request.session.get("auth_version")
    if (
        not current
        or not current.active
        or current.archived_at
        or session_auth_version != current.auth_version
    ):
        request.session.clear()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)
    return current

def require(current: User, db: Session, permission: str, team_id: str | None = None) -> None:
    if not allowed(db, current, permission, team_id):
        raise HTTPException(403, "Keine Berechtigung für diese Aktion")

def require_admin(current: User) -> None:
    if current.role != Role.ADMIN:
        raise HTTPException(403, "Nur Administratoren dürfen diese Aktion ausführen")
