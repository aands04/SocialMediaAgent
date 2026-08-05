from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import AuditLog, DirectUploadSession, User
from app.storage.providers import LocalObjectStorageProvider, build_object_storage_provider
from app.storage.service import (
    StorageQuotaError,
    complete_direct_upload,
    create_direct_upload,
)
from app.web import current_user, require

router = APIRouter(prefix="/api/storage")


@router.post("/uploads")
async def reserve_upload(
    request: Request,
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    require(current, db, "generate")
    payload = await request.json()
    idempotency_key = str(payload.get("idempotency_key") or "").strip()
    if not idempotency_key or len(idempotency_key) > 180:
        raise HTTPException(422, "Idempotency Key fehlt oder ist zu lang")
    settings = get_settings()
    provider = build_object_storage_provider(settings)
    try:
        upload, upload_url = create_direct_upload(
            db,
            settings,
            provider,
            user=current,
            category=str(payload.get("category") or ""),
            expected_size=int(payload.get("size") or 0),
            mime_type=str(payload.get("mime_type") or "").casefold(),
            checksum=(str(payload["checksum"]).casefold() if payload.get("checksum") else None),
            idempotency_key=idempotency_key,
        )
        db.commit()
    except (StorageQuotaError, ValueError) as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    return {
        "upload_id": upload.id,
        "upload_url": upload_url,
        "method": "PUT",
        "headers": {"Content-Type": upload.expected_mime_type},
        "expires_at": upload.expires_at.isoformat(),
    }


@router.put("/uploads/{upload_id}/content", status_code=204)
async def local_upload_content(
    upload_id: str,
    request: Request,
    token: str,
    content_type: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    provider = build_object_storage_provider(settings)
    if not isinstance(provider, LocalObjectStorageProvider):
        raise HTTPException(404)
    # This endpoint intentionally authenticates with the short-lived random
    # upload token, not a dashboard session.
    from app.tenancy.state import system_scope

    with system_scope("Lokales signiertes Upload-Token prüfen"):
        upload = db.scalar(
            select(DirectUploadSession).where(DirectUploadSession.id == upload_id)
        )
        digest = hashlib.sha256(token.encode()).hexdigest()
        if (
            not upload
            or not upload.upload_token_hash
            or not secrets_compare(digest, upload.upload_token_hash)
            or upload.status != "reserved"
            or upload.expires_at <= datetime.now(timezone.utc)
        ):
            raise HTTPException(404)
        if content_type != upload.expected_mime_type:
            raise HTTPException(415, "Content-Type stimmt nicht mit der Reservierung überein")
        body = await request.body()
        if len(body) > upload.expected_size_bytes:
            raise HTTPException(413, "Datei ist größer als reserviert")
        provider.put(upload.object_key, body, upload.expected_mime_type)


def secrets_compare(left: str, right: str) -> bool:
    import secrets

    return secrets.compare_digest(left, right)


@router.post("/uploads/{upload_id}/complete")
def complete_upload(
    upload_id: str,
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    require(current, db, "generate")
    upload = db.get(DirectUploadSession, upload_id)
    if not upload or upload.actor_user_id != current.id:
        raise HTTPException(404)
    provider = build_object_storage_provider(get_settings())
    try:
        item = complete_direct_upload(db, provider, upload)
        db.add(
            AuditLog(
                club_id=current.club_id,
                user_id=current.id,
                action="storage.direct_upload_completed",
                entity_type="storage_object",
                entity_id=item.id,
                details={
                    "category": item.category,
                    "size_bytes": item.size_bytes,
                    "checksum": item.checksum,
                },
            )
        )
        db.commit()
    except StorageQuotaError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    return {"storage_object_id": item.id, "size": item.size_bytes, "checksum": item.checksum}
