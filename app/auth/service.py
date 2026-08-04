import re
from datetime import datetime, timedelta, timezone

from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Role, User, UserTeam

passwords=PasswordHash.recommended()
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256
MAX_EMAIL_LENGTH = 254
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PERMISSIONS={
    Role.ADMIN: {"*"},
    Role.APPROVER: {"view", "edit_post", "generate", "approve", "publish_retry"},
    Role.EDITOR: {"view", "edit_post", "generate"},
    Role.VIEWER: {"view"},
}
def hash_password(value:str)->str: return passwords.hash(value)
def verify_password(value:str,hashed:str)->bool: return passwords.verify(value,hashed)
def validate_new_password(value:str)->str|None:
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
def authenticate(db:Session,email:str,password:str)->User|None:
    try:
        normalized_email = normalize_email(email)
    except ValueError:
        return None
    user=db.scalar(select(User).where(User.email==normalized_email,User.archived_at.is_(None)))
    now=datetime.now(timezone.utc)
    if not user or not user.active or (user.locked_until and user.locked_until>now): return None
    if not verify_password(password,user.password_hash):
        user.failed_logins+=1
        if user.failed_logins>=5: user.locked_until=now+timedelta(minutes=15)
        db.commit(); return None
    user.failed_logins=0; user.locked_until=None; db.commit(); return user
def allowed(db:Session,user:User,permission:str,team_id:str|None=None)->bool:
    if permission not in PERMISSIONS[user.role] and "*" not in PERMISSIONS[user.role]: return False
    if team_id and not user.all_teams:
        return db.get(UserTeam,{"user_id":user.id,"team_id":team_id}) is not None
    return True
