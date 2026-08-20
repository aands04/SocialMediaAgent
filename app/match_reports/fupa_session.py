from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.meta.security import MetaSecretError, TokenCipher
from app.models import AuditLog, FupaBrowserSession


class FupaSessionError(ValueError):
    pass


def _fupa_host(value: str | None) -> bool:
    host = (value or "").strip().lower().lstrip(".")
    return host == "fupa.net" or host.endswith(".fupa.net")


def sanitize_storage_state(raw: bytes | str, *, max_bytes: int = 524_288) -> str:
    """Validate and reduce a Playwright state to FuPa-owned browser data.

    Foreign cookies and origins (for example Meta or Google login state) are
    deliberately discarded before anything is persisted.
    """

    payload = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not payload or len(payload) > max_bytes:
        raise FupaSessionError("Die FuPa-Sitzungsdatei ist leer oder zu groß")
    try:
        parsed: Any = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FupaSessionError("Die FuPa-Sitzungsdatei ist kein gültiges JSON") from exc
    if not isinstance(parsed, dict):
        raise FupaSessionError("Die FuPa-Sitzungsdatei besitzt ein ungültiges Format")

    cookies = [
        item
        for item in parsed.get("cookies", [])
        if isinstance(item, dict) and _fupa_host(str(item.get("domain") or ""))
    ]
    origins = []
    for item in parsed.get("origins", []):
        if not isinstance(item, dict):
            continue
        origin = str(item.get("origin") or "")
        target = urlparse(origin)
        if target.scheme == "https" and _fupa_host(target.hostname):
            origins.append(item)
    if not cookies:
        raise FupaSessionError(
            "Die Datei enthält keine FuPa-Sitzung. Bitte zuerst interaktiv bei FuPa anmelden"
        )
    canonical = json.dumps(
        {"cookies": cookies, "origins": origins},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(canonical.encode("utf-8")) > max_bytes:
        raise FupaSessionError("Die bereinigte FuPa-Sitzung ist zu groß")
    return canonical


def save_fupa_browser_session(
    db: Session,
    *,
    club_id: str,
    raw_state: bytes | str,
    user_id: str,
    settings,
) -> FupaBrowserSession:
    canonical = sanitize_storage_state(
        raw_state,
        max_bytes=settings.fupa_browser_session_max_bytes,
    )
    encrypted = TokenCipher(settings.meta_token_encryption_key).encrypt(canonical)
    item = db.scalar(select(FupaBrowserSession).where(FupaBrowserSession.club_id == club_id))
    if item is None:
        item = FupaBrowserSession(club_id=club_id, created_by=user_id)
    item.encrypted_storage_state = encrypted
    item.key_version = settings.meta_token_key_version
    item.status = "active"
    item.last_verified_at = None
    item.last_used_at = None
    item.last_error_category = None
    item.last_error = None
    db.add(item)
    db.flush()
    db.add(
        AuditLog(
            club_id=club_id,
            user_id=user_id,
            action="match_report.fupa_session_saved",
            entity_type="fupa_browser_session",
            entity_id=item.id,
            details={
                "key_version": item.key_version,
                "contains_password": False,
                "contains_encrypted_session": True,
            },
        )
    )
    return item


def decrypt_fupa_browser_session(item: FupaBrowserSession, settings) -> str:
    try:
        plaintext = TokenCipher(settings.meta_token_encryption_key).decrypt(
            item.encrypted_storage_state
        )
    except MetaSecretError as exc:
        raise FupaSessionError("Die FuPa-Sitzung kann nicht entschlüsselt werden") from exc
    return sanitize_storage_state(
        plaintext,
        max_bytes=settings.fupa_browser_session_max_bytes,
    )


def update_fupa_browser_session(
    item: FupaBrowserSession,
    raw_state: bytes | str,
    *,
    settings,
) -> None:
    canonical = sanitize_storage_state(
        raw_state,
        max_bytes=settings.fupa_browser_session_max_bytes,
    )
    item.encrypted_storage_state = TokenCipher(settings.meta_token_encryption_key).encrypt(
        canonical
    )
    item.key_version = settings.meta_token_key_version
    item.status = "active"
    item.last_verified_at = datetime.now(timezone.utc)
    item.last_used_at = datetime.now(timezone.utc)
    item.last_error_category = None
    item.last_error = None


def revoke_fupa_browser_session(
    db: Session,
    item: FupaBrowserSession,
    *,
    user_id: str,
) -> None:
    item.encrypted_storage_state = None
    item.status = "revoked"
    item.last_error_category = None
    item.last_error = None
    db.add(
        AuditLog(
            club_id=item.club_id,
            user_id=user_id,
            action="match_report.fupa_session_revoked",
            entity_type="fupa_browser_session",
            entity_id=item.id,
            details={},
        )
    )


def mark_fupa_session_error(
    item: FupaBrowserSession,
    *,
    category: str,
    message: str,
) -> None:
    item.status = "expired" if category == "authentication_required" else "error"
    item.last_error_category = category[:80]
    item.last_error = message[:2000]
    item.last_used_at = datetime.now(timezone.utc)
