from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from io import BytesIO
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.limits.service import effective_limits
from app.models import (
    DirectUploadSession,
    LedgerStatus,
    MediaAsset,
    StorageLedgerEntry,
    StorageObject,
    StorageReconciliationRun,
    User,
    uid,
)
from app.storage.providers import LocalObjectStorageProvider, ObjectStorageProvider

ALLOWED_CATEGORIES = {
    "logos",
    "fonts",
    "players",
    "backgrounds",
    "generated/feed",
    "generated/story",
    "previews",
    "imports",
    "exports",
    "provider_snapshots",
}
ALLOWED_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "font/ttf",
    "font/woff2",
    "text/html",
    "text/csv",
    "application/zip",
}


class StorageQuotaError(ValueError):
    pass


def object_key(club_id: str, category: str) -> str:
    if category not in ALLOWED_CATEGORIES:
        raise StorageQuotaError("Unzulässige Medienkategorie")
    return f"clubs/{club_id}/{category}/{uuid4()}"


def _storage_usage(
    db: Session,
    club_id: str,
    *,
    exclude_media_asset_ids: set[str] | None = None,
) -> tuple[int, int]:
    committed_objects = int(
        db.scalar(
            select(func.coalesce(func.sum(StorageObject.size_bytes), 0)).where(
                StorageObject.club_id == club_id,
                StorageObject.deleted_at.is_(None),
                StorageObject.billable.is_(True),
            )
        )
        or 0
    )
    # Media uploaded through the historical dashboard predates the storage
    # ledger.  Count it until it is represented by a StorageObject so an
    # upgrade cannot silently reset a club's consumed quota.
    tracked_media_ids = {
        str(references.get("media_asset_id"))
        for references in db.scalars(
            select(StorageObject.references).where(
                StorageObject.club_id == club_id,
                StorageObject.deleted_at.is_(None),
            )
        )
        if isinstance(references, dict) and references.get("media_asset_id")
    }
    excluded = exclude_media_asset_ids or set()
    legacy_media_bytes = sum(
        int(size or 0)
        for media_id, size in db.execute(
            select(MediaAsset.id, MediaAsset.size).where(
                MediaAsset.club_id == club_id,
                # A used, soft-deleted asset remains available to historical
                # posts and therefore still occupies storage.  An unused
                # deleted upload is marked unavailable after physical removal.
                (MediaAsset.deleted_at.is_(None)) | (MediaAsset.available.is_(True)),
            )
        )
        if media_id not in tracked_media_ids and media_id not in excluded
    )
    committed = committed_objects + legacy_media_bytes
    reserved = int(
        db.scalar(
            select(func.coalesce(func.sum(StorageLedgerEntry.reserved_bytes), 0)).where(
                StorageLedgerEntry.club_id == club_id,
                StorageLedgerEntry.status == LedgerStatus.RESERVED,
            )
        )
        or 0
    )
    return committed, reserved


def storage_usage(db: Session, club_id: str) -> tuple[int, int]:
    """Return committed and currently reserved billable bytes for one club."""

    return _storage_usage(db, club_id)


def commit_local_media_upload(
    db: Session,
    ledger: StorageLedgerEntry,
    *,
    club_id: str,
    media_asset_id: str,
    team_id: str,
    object_key: str,
    size_bytes: int,
    checksum: str,
    mime_type: str,
) -> StorageObject:
    """Commit one validated dashboard upload to the shared storage ledger."""

    if ledger.club_id != club_id or ledger.status != LedgerStatus.RESERVED:
        raise StorageQuotaError("Die Speicherreservierung ist nicht mehr gültig")
    if not object_key.startswith(f"clubs/{club_id}/"):
        raise StorageQuotaError("Upload liegt außerhalb des Vereinsspeichers")
    limit = effective_limits(db, club_id, lock=True)["storage_bytes"].value
    committed, reserved = _storage_usage(
        db, club_id, exclude_media_asset_ids={media_asset_id}
    )
    other_reserved = max(0, reserved - int(ledger.reserved_bytes))
    if committed + other_reserved + size_bytes > limit:
        raise StorageQuotaError("Speicherlimit wird durch den tatsächlichen Upload überschritten")
    item = StorageObject(
        club_id=club_id,
        provider="local",
        bucket="application-uploads",
        object_key=object_key,
        category="players",
        size_bytes=size_bytes,
        checksum=checksum,
        mime_type=mime_type,
        references={"media_asset_id": media_asset_id, "team_id": team_id},
        billable=True,
        provider_metadata={"source": "dashboard_media_library"},
    )
    db.add(item)
    db.flush()
    ledger.storage_object_id = item.id
    ledger.status = LedgerStatus.COMMITTED
    ledger.actual_bytes = size_bytes
    ledger.reserved_bytes = 0
    return item


def mark_local_media_deleted(
    db: Session,
    *,
    club_id: str,
    media_asset_id: str,
    actor_user_id: str | None,
) -> None:
    """Release billed storage after a physical dashboard upload is removed."""

    objects = db.scalars(
        select(StorageObject).where(
            StorageObject.club_id == club_id,
            StorageObject.provider == "local",
            StorageObject.deleted_at.is_(None),
        )
    ).all()
    item = next(
        (
            candidate
            for candidate in objects
            if (candidate.references or {}).get("media_asset_id") == media_asset_id
        ),
        None,
    )
    if item is None:
        return
    item.deleted_at = datetime.now(timezone.utc)
    db.add(
        StorageLedgerEntry(
            club_id=club_id,
            storage_object_id=item.id,
            action="delete",
            status=LedgerStatus.DELETED,
            reserved_bytes=0,
            actual_bytes=0,
            actor_user_id=actor_user_id,
            idempotency_key=f"dashboard-media-delete:{media_asset_id}",
            details={"released_bytes": item.size_bytes, "media_asset_id": media_asset_id},
        )
    )


def move_local_media_storage_object(
    db: Session,
    *,
    club_id: str,
    media_asset_id: str,
    team_id: str,
    object_key: str,
) -> None:
    """Keep ledger metadata aligned when an unused upload changes teams."""

    objects = db.scalars(
        select(StorageObject).where(
            StorageObject.club_id == club_id,
            StorageObject.provider == "local",
            StorageObject.deleted_at.is_(None),
        )
    ).all()
    item = next(
        (
            candidate
            for candidate in objects
            if (candidate.references or {}).get("media_asset_id") == media_asset_id
        ),
        None,
    )
    if item:
        item.object_key = object_key
        item.references = {**(item.references or {}), "team_id": team_id}


def reserve_storage(
    db: Session,
    *,
    club_id: str,
    bytes_requested: int,
    idempotency_key: str,
    actor_user_id: str | None,
    actor_job_id: str | None = None,
) -> StorageLedgerEntry:
    existing = db.scalar(
        select(StorageLedgerEntry).where(
            StorageLedgerEntry.club_id == club_id,
            StorageLedgerEntry.idempotency_key == idempotency_key,
        )
    )
    if existing:
        return existing
    if bytes_requested <= 0:
        raise StorageQuotaError("Reservierte Dateigröße muss positiv sein")
    limit = effective_limits(db, club_id, lock=True)["storage_bytes"].value
    committed, reserved = _storage_usage(db, club_id)
    if committed + reserved + bytes_requested > limit:
        raise StorageQuotaError(
            f"Speicherlimit überschritten: {committed + reserved} von {limit} Byte belegt"
        )
    entry = StorageLedgerEntry(
        id=uid(),
        club_id=club_id,
        action="upload",
        status=LedgerStatus.RESERVED,
        reserved_bytes=bytes_requested,
        actual_bytes=0,
        actor_user_id=actor_user_id,
        actor_job_id=actor_job_id,
        idempotency_key=idempotency_key,
        details={},
    )
    db.add(entry)
    db.flush()
    return entry


def create_direct_upload(
    db: Session,
    settings: Settings,
    provider: ObjectStorageProvider,
    *,
    user: User,
    category: str,
    expected_size: int,
    mime_type: str,
    checksum: str | None,
    idempotency_key: str,
) -> tuple[DirectUploadSession, str]:
    if not user.club_id:
        raise StorageQuotaError("Vereinszuordnung fehlt")
    if mime_type not in ALLOWED_MIME_TYPES:
        raise StorageQuotaError("Dateityp ist nicht erlaubt")
    existing = db.scalar(
        select(DirectUploadSession).where(
            DirectUploadSession.club_id == user.club_id,
            DirectUploadSession.idempotency_key == idempotency_key,
        )
    )
    if existing:
        raise StorageQuotaError("Dieser Upload wurde bereits reserviert")
    ledger = reserve_storage(
        db,
        club_id=user.club_id,
        bytes_requested=expected_size,
        idempotency_key=f"direct-upload:{idempotency_key}",
        actor_user_id=user.id,
    )
    key = object_key(user.club_id, category)
    raw_token = secrets.token_urlsafe(32) if isinstance(provider, LocalObjectStorageProvider) else ""
    upload = DirectUploadSession(
        club_id=user.club_id,
        actor_user_id=user.id,
        ledger_entry_id=ledger.id,
        provider=provider.name,
        bucket=provider.bucket,
        object_key=key,
        category=category,
        expected_size_bytes=expected_size,
        expected_mime_type=mime_type,
        expected_checksum=checksum,
        upload_token_hash=hashlib.sha256(raw_token.encode()).hexdigest() if raw_token else None,
        status="reserved",
        expires_at=datetime.now(timezone.utc)
        + timedelta(seconds=settings.s3_presign_ttl_seconds),
        idempotency_key=idempotency_key,
    )
    db.add(upload)
    db.flush()
    url = (
        f"/api/storage/uploads/{upload.id}/content?token={raw_token}"
        if raw_token
        else provider.presign_put(key, mime_type, settings.s3_presign_ttl_seconds)
    )
    return upload, url


def validate_object_payload(payload: bytes, mime_type: str) -> None:
    if mime_type.startswith("image/"):
        try:
            with Image.open(BytesIO(payload)) as image:
                image.verify()
        except (UnidentifiedImageError, OSError, SyntaxError) as exc:
            raise StorageQuotaError("Upload ist keine technisch lesbare Bilddatei") from exc
    elif mime_type == "font/woff2" and not payload.startswith(b"wOF2"):
        raise StorageQuotaError("Ungültige WOFF2-Dateisignatur")
    elif mime_type == "font/ttf" and payload[:4] not in {b"\x00\x01\x00\x00", b"true"}:
        raise StorageQuotaError("Ungültige TTF-Dateisignatur")


def complete_direct_upload(
    db: Session,
    provider: ObjectStorageProvider,
    upload: DirectUploadSession,
) -> StorageObject:
    if upload.status == "completed" and upload.storage_object_id:
        return db.get(StorageObject, upload.storage_object_id)
    if upload.status != "reserved" or upload.expires_at <= datetime.now(timezone.utc):
        raise StorageQuotaError("Upload-Reservierung ist abgelaufen oder nicht aktiv")
    head = provider.head(upload.object_key)
    actual_size = int(head["size"])
    actual_mime = str(head.get("content_type") or "").split(";", 1)[0].casefold()
    if actual_size <= 0 or actual_mime != upload.expected_mime_type:
        provider.delete(upload.object_key)
        raise StorageQuotaError("Tatsächliche Dateigröße oder MIME-Type ist ungültig")
    payload = provider.get(upload.object_key)
    checksum = hashlib.sha256(payload).hexdigest()
    try:
        validate_object_payload(payload, actual_mime)
        if upload.expected_checksum and checksum != upload.expected_checksum:
            raise StorageQuotaError("Prüfsumme stimmt nicht mit der Reservierung überein")
        limit = effective_limits(db, upload.club_id, lock=True)["storage_bytes"].value
        committed, reserved = _storage_usage(db, upload.club_id)
        ledger = db.get(StorageLedgerEntry, upload.ledger_entry_id)
        other_reserved = max(0, reserved - int(ledger.reserved_bytes))
        if committed + other_reserved + actual_size > limit:
            raise StorageQuotaError("Speicherlimit wird durch den tatsächlichen Upload überschritten")
    except Exception:
        provider.delete(upload.object_key)
        ledger = db.get(StorageLedgerEntry, upload.ledger_entry_id)
        ledger.status = LedgerStatus.RELEASED
        ledger.reserved_bytes = 0
        upload.status = "failed"
        raise
    item = StorageObject(
        club_id=upload.club_id,
        provider=provider.name,
        bucket=provider.bucket,
        object_key=upload.object_key,
        category=upload.category,
        size_bytes=actual_size,
        checksum=checksum,
        mime_type=actual_mime,
        references={},
        billable=True,
        provider_metadata={},
    )
    db.add(item)
    db.flush()
    ledger.storage_object_id = item.id
    ledger.status = LedgerStatus.COMMITTED
    ledger.actual_bytes = actual_size
    ledger.reserved_bytes = 0
    upload.status = "completed"
    upload.completed_at = datetime.now(timezone.utc)
    upload.storage_object_id = item.id
    return item


def cleanup_expired_uploads(
    db: Session,
    provider: ObjectStorageProvider,
    *,
    now: datetime | None = None,
    batch_size: int = 100,
) -> int:
    current = now or datetime.now(timezone.utc)
    statement = (
        select(DirectUploadSession)
        .where(
            DirectUploadSession.provider == provider.name,
            DirectUploadSession.status == "reserved",
            DirectUploadSession.expires_at <= current,
        )
        .order_by(DirectUploadSession.expires_at)
        .limit(batch_size)
    )
    if db.bind.dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)
    expired = list(db.scalars(statement))
    for upload in expired:
        provider.delete(upload.object_key)
        ledger = db.get(StorageLedgerEntry, upload.ledger_entry_id)
        if ledger and ledger.status == LedgerStatus.RESERVED:
            ledger.status = LedgerStatus.RELEASED
            ledger.reserved_bytes = 0
            ledger.details = {
                **(ledger.details or {}),
                "release_reason": "direct_upload_expired",
            }
        upload.status = "expired"
    db.commit()
    return len(expired)


def reconcile_storage(
    db: Session,
    provider: ObjectStorageProvider,
    *,
    club_id: str | None,
    started_by: str | None,
) -> StorageReconciliationRun:
    run = StorageReconciliationRun(
        club_id=club_id,
        provider=provider.name,
        status="running",
        report={},
        started_by=started_by,
    )
    db.add(run)
    db.flush()
    query = select(StorageObject).where(
        StorageObject.provider == provider.name,
        StorageObject.deleted_at.is_(None),
    )
    if club_id:
        query = query.where(StorageObject.club_id == club_id)
    expected = {item.object_key: item for item in db.scalars(query)}
    prefix = f"clubs/{club_id}/" if club_id else "clubs/"
    actual = {item["key"]: item for item in provider.list(prefix)}
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    mismatches = sorted(
        key
        for key in set(expected) & set(actual)
        if expected[key].size_bytes != int(actual[key]["size"])
    )
    run.status = "attention_required" if missing or unexpected or mismatches else "completed"
    run.checked_objects = len(expected)
    run.missing_objects = len(missing)
    run.unexpected_objects = len(unexpected)
    run.size_mismatches = len(mismatches)
    run.report = {
        "missing": missing[:500],
        "unexpected": unexpected[:500],
        "size_mismatches": mismatches[:500],
    }
    run.completed_at = datetime.now(timezone.utc)
    return run
