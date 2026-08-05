import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.meta.security import random_media_token, secret_hash
from app.models import (
    AuditLog,
    PublicationJob,
    PublicationMediaItem,
    PublicMediaGrant,
    User,
)


class MediaGrantError(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _inside(root: Path, path: Path) -> bool:
    return path == root or root in path.parents


def publication_media_items(db: Session, job: PublicationJob) -> list[PublicationMediaItem]:
    items = list(
        db.scalars(
            select(PublicationMediaItem)
            .where(PublicationMediaItem.publication_job_id == job.id)
            .order_by(PublicationMediaItem.position)
        )
    )
    if job.kind == "carousel":
        if not 2 <= len(items) <= 10:
            raise MediaGrantError("Karussell benötigt 2 bis 10 geordnete Bilder")
        if [item.position for item in items] != list(range(1, len(items) + 1)):
            raise MediaGrantError("Karussell-Reihenfolge ist nicht lückenlos")
    return items


def validate_publication_png(
    job: PublicationJob,
    settings: Settings,
    media_item: PublicationMediaItem | None = None,
) -> dict:
    root = settings.generated_root.resolve()
    if media_item and media_item.publication_job_id != job.id:
        raise MediaGrantError("Karussellbild gehört nicht zum Veröffentlichungsauftrag")
    raw = Path(media_item.media_path if media_item else job.media_path)
    path = raw.resolve(strict=True)
    if raw.is_symlink() or not _inside(root, path):
        raise MediaGrantError("Mediendatei liegt außerhalb des generierten Verzeichnisses")
    if path.suffix.lower() != ".png" or not path.is_file():
        raise MediaGrantError("Nur eine vorhandene PNG-Veröffentlichungsdatei ist zulässig")
    payload = path.read_bytes()
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            if image.format != "PNG":
                raise MediaGrantError("Dateiinhalt ist kein PNG")
    except (OSError, ValueError) as exc:
        raise MediaGrantError("PNG-Datei ist technisch nicht lesbar") from exc
    expected = (1080, 1350) if job.kind in {"feed", "carousel"} else (1080, 1920)
    if (width, height) != expected:
        raise MediaGrantError(f"Falsche Auflösung; erwartet {expected[0]} × {expected[1]}")
    return {
        "path": path,
        "checksum": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "mime_type": "image/png",
        "width": width,
        "height": height,
    }


def create_grant(
    db: Session,
    settings: Settings,
    job: PublicationJob,
    user: User,
    media_item: PublicationMediaItem | None = None,
) -> tuple[PublicMediaGrant, str, str]:
    if not settings.meta_public_base_url or not settings.meta_public_base_url.startswith(
        "https://"
    ):
        raise MediaGrantError("META_PUBLIC_BASE_URL muss eine öffentliche HTTPS-Adresse sein")
    report = validate_publication_png(job, settings, media_item)
    active_key = job.id if media_item is None else f"{job.id}:{media_item.id}"
    existing = db.scalar(
        select(PublicMediaGrant).where(PublicMediaGrant.active_key == active_key).with_for_update()
    )
    if existing:
        existing.revoked_at = datetime.now(timezone.utc)
        existing.active_key = None
        db.flush()
    raw_token = random_media_token()
    grant = PublicMediaGrant(
        publication_job_id=job.id,
        publication_media_item_id=media_item.id if media_item else None,
        token_hash=secret_hash(raw_token),
        active_key=active_key,
        media_path=str(report["path"]),
        file_checksum=report["checksum"],
        mime_type=report["mime_type"],
        file_size=report["size"],
        expires_at=datetime.now(timezone.utc)
        + timedelta(seconds=settings.meta_media_grant_ttl_seconds),
        created_by=user.id,
    )
    db.add(grant)
    db.flush()
    db.add(
        AuditLog(
            user_id=user.id,
            team_id=job.team_id,
            action="meta.media_grant_created",
            entity_type="public_media_grant",
            entity_id=grant.id,
            details={
                "publication_job_id": job.id,
                "publication_media_item_id": media_item.id if media_item else None,
                "checksum": grant.file_checksum,
                "expires_at": grant.expires_at.isoformat(),
            },
        )
    )
    url = f"{settings.meta_public_base_url.rstrip('/')}/public/meta-media/{raw_token}"
    return grant, raw_token, url


def verify_public_media_url(
    settings: Settings,
    grant: PublicMediaGrant,
    url: str,
    client: httpx.Client,
) -> dict:
    """Verify the exact public representation Meta will fetch."""
    if not url.startswith("https://"):
        raise MediaGrantError("Öffentliche Medienfreigabe verwendet kein HTTPS")
    try:
        response = client.get(
            url,
            headers={"Accept": "image/png"},
        )
    except httpx.RequestError as exc:
        raise MediaGrantError("Öffentliche Medien-URL ist von außen nicht erreichbar") from exc
    if response.status_code != 200:
        raise MediaGrantError(f"Öffentliche Medien-URL antwortet mit HTTP {response.status_code}")
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "image/png":
        raise MediaGrantError("Öffentliche Medien-URL liefert nicht image/png")
    payload = response.content
    if len(payload) != grant.file_size:
        raise MediaGrantError("Öffentliche Medien-URL liefert eine andere Dateigröße")
    if hashlib.sha256(payload).hexdigest() != grant.file_checksum:
        raise MediaGrantError("Öffentliche Medien-URL liefert eine andere Datei")
    return {
        "reachable": True,
        "status_code": response.status_code,
        "content_type": content_type,
        "size": len(payload),
    }


def revoke_grant(db: Session, grant: PublicMediaGrant, user: User | None, *, reason: str) -> None:
    grant.revoked_at = datetime.now(timezone.utc)
    grant.active_key = None
    db.add(
        AuditLog(
            user_id=user.id if user else None,
            action="meta.media_grant_revoked",
            entity_type="public_media_grant",
            entity_id=grant.id,
            details={"reason": reason},
        )
    )


def resolve_grant(db: Session, settings: Settings, raw_token: str) -> tuple[PublicMediaGrant, Path]:
    grant = db.scalar(
        select(PublicMediaGrant)
        .where(PublicMediaGrant.token_hash == secret_hash(raw_token))
        .with_for_update()
    )
    now = datetime.now(timezone.utc)
    if not grant or grant.revoked_at or _utc(grant.expires_at) <= now:
        raise MediaGrantError("Medienfreigabe ist ungültig oder abgelaufen")
    job = db.get(PublicationJob, grant.publication_job_id)
    media_item = (
        db.get(PublicationMediaItem, grant.publication_media_item_id)
        if grant.publication_media_item_id
        else None
    )
    expected_path = media_item.media_path if media_item else (job.media_path if job else "")
    if (
        not job
        or (grant.publication_media_item_id and not media_item)
        or str(Path(expected_path).resolve()) != grant.media_path
    ):
        raise MediaGrantError("Medienfreigabe gehört nicht mehr zur eingefrorenen Datei")
    report = validate_publication_png(job, settings, media_item)
    if report["checksum"] != grant.file_checksum or report["size"] != grant.file_size:
        raise MediaGrantError("Mediendatei wurde nach der Freigabe verändert")
    grant.fetch_count += 1
    grant.last_fetched_at = now
    db.commit()
    return grant, report["path"]
