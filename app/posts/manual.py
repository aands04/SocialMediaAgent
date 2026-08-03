import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    AuditLog,
    InstagramPage,
    JobStatus,
    Post,
    PostStatus,
    PublicationJob,
    PublicationMediaItem,
    Team,
    User,
)
from app.rendering.service import Renderer, RenderValidationError

MAX_MANUAL_IMAGE_BYTES = 20 * 1024 * 1024
MAX_MANUAL_TEXT_CHARS = 2200
MANUAL_IMAGE_TYPES = {
    ".jpg": ("JPEG", "image/jpeg"),
    ".jpeg": ("JPEG", "image/jpeg"),
    ".png": ("PNG", "image/png"),
    ".webp": ("WEBP", "image/webp"),
}
MANUAL_IMAGE_SIZES = {
    "feed": (1080, 1350),
    "carousel": (1080, 1350),
    "story": (1080, 1920),
}
MAX_CAROUSEL_IMAGES = 10
_SUBMISSION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,120}$")


class ManualPostError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedManualImage:
    original_filename: str
    original_mime_type: str
    original_checksum: str
    png: bytes
    png_checksum: str
    width: int
    height: int


def validate_manual_image(
    filename: str,
    content_type: str | None,
    content: bytes,
    kind: str,
) -> ValidatedManualImage:
    if kind not in MANUAL_IMAGE_SIZES:
        raise ManualPostError("Medienart muss Feed, Karussell oder Story sein")
    if not content:
        raise ManualPostError("Bilddatei ist leer")
    if len(content) > MAX_MANUAL_IMAGE_BYTES:
        raise ManualPostError("Bilddatei ist größer als 20 MiB")
    safe_name = Path((filename or "").replace("\\", "/")).name
    suffix = Path(safe_name).suffix.lower()
    expected = MANUAL_IMAGE_TYPES.get(suffix)
    if not safe_name or not expected:
        raise ManualPostError("Erlaubt sind ausschließlich JPG, PNG und WebP")
    expected_format, expected_mime = expected
    if (content_type or "").split(";", 1)[0].lower() != expected_mime:
        raise ManualPostError("Dateiendung und MIME-Type stimmen nicht überein")
    try:
        with Image.open(BytesIO(content)) as probe:
            actual_format = probe.format
            frames = getattr(probe, "n_frames", 1)
            probe.verify()
        if actual_format != expected_format:
            raise ManualPostError("Dateiendung und tatsächlicher Bildtyp stimmen nicht überein")
        if frames != 1:
            raise ManualPostError("Animierte Bilder sind nicht zulässig")
        with Image.open(BytesIO(content)) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            if image.size != MANUAL_IMAGE_SIZES[kind]:
                width, height = MANUAL_IMAGE_SIZES[kind]
                raise ManualPostError(
                    f"Falsche Auflösung; für {kind} werden genau {width} × {height} Pixel benötigt"
                )
            if "A" in image.getbands():
                normalized = image.convert("RGBA")
                if normalized.getchannel("A").getextrema() == (0, 0):
                    raise ManualPostError("Bild ist vollständig transparent")
            else:
                normalized = image.convert("RGB")
            target = BytesIO()
            normalized.save(target, format="PNG", optimize=True)
            png = target.getvalue()
    except ManualPostError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise ManualPostError("Bilddatei ist technisch nicht lesbar") from exc
    return ValidatedManualImage(
        original_filename=safe_name,
        original_mime_type=expected_mime,
        original_checksum=hashlib.sha256(content).hexdigest(),
        png=png,
        png_checksum=hashlib.sha256(png).hexdigest(),
        width=MANUAL_IMAGE_SIZES[kind][0],
        height=MANUAL_IMAGE_SIZES[kind][1],
    )


def parse_manual_publication_time(value: str, timezone_name: str) -> datetime:
    try:
        local = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ManualPostError("Veröffentlichungszeitpunkt ist ungültig") from exc
    if local.tzinfo is not None:
        raise ManualPostError("Veröffentlichungszeitpunkt muss als lokale Uhrzeit angegeben werden")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ManualPostError("Mannschaftszeitzone ist ungültig") from exc
    first = local.replace(tzinfo=zone, fold=0)
    second = local.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ManualPostError(
            "Zeitpunkt liegt in einer Zeitumstellung und ist nicht eindeutig; bitte andere Uhrzeit wählen"
        )
    roundtrip = first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    if roundtrip != local:
        raise ManualPostError("Dieser lokale Zeitpunkt existiert wegen der Zeitumstellung nicht")
    scheduled_at = first.astimezone(timezone.utc)
    if scheduled_at <= datetime.now(timezone.utc):
        raise ManualPostError("Veröffentlichungszeitpunkt muss in der Zukunft liegen")
    return scheduled_at


def create_manual_post(
    db: Session,
    settings: Settings,
    *,
    team: Team,
    user: User,
    submission_id: str,
    kind: str,
    text: str,
    scheduled_at: datetime,
    images: list[ValidatedManualImage],
) -> tuple[Post, bool]:
    if not _SUBMISSION_PATTERN.fullmatch(submission_id or ""):
        raise ManualPostError("Ungültige Formular-ID; Seite bitte neu laden")
    body = text.strip()
    if not body:
        raise ManualPostError("Text darf nicht leer sein")
    if len(body) > MAX_MANUAL_TEXT_CHARS:
        raise ManualPostError(
            f"Text darf höchstens {MAX_MANUAL_TEXT_CHARS} Zeichen enthalten"
        )
    if kind not in MANUAL_IMAGE_SIZES:
        raise ManualPostError("Medienart muss Feed, Karussell oder Story sein")
    if kind == "carousel" and not 2 <= len(images) <= MAX_CAROUSEL_IMAGES:
        raise ManualPostError("Ein Karussell benötigt 2 bis 10 Bilder")
    if kind != "carousel" and len(images) != 1:
        raise ManualPostError("Feed und Story benötigen genau ein Bild")
    existing = db.scalar(
        select(Post).where(Post.manual_submission_id == submission_id)
    )
    if existing:
        return existing, False
    page = db.get(InstagramPage, team.instagram_page_id)
    if not team.active or team.archived_at:
        raise ManualPostError("Mannschaft ist nicht aktiv")
    if not page or page.archived_at:
        raise ManualPostError("Der Mannschaft ist keine Instagram-Seite zugeordnet")

    post = Post(
        game_id=None,
        team_id=team.id,
        instagram_page_id=page.id,
        post_type="manual",
        active_key="manual",
        manual_submission_id=submission_id,
        status=PostStatus.PENDING,
        text=body,
        last_edited_by=user.id,
        design_snapshot={},
        critical_warnings=[],
    )
    db.add(post)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        existing = db.scalar(
            select(Post).where(Post.manual_submission_id == submission_id)
        )
        if existing:
            return existing, False
        raise ManualPostError("Beitrag konnte nicht eindeutig angelegt werden") from exc

    root = settings.generated_root.resolve()
    targets: list[Path] = []
    try:
        renderer = Renderer(root, settings.media_root, settings.upload_root)
        for position, image in enumerate(images, start=1):
            filename = (
                f"carousel-{position:02d}-v1.png"
                if kind == "carousel"
                else f"{kind}-v1.png"
            )
            target = (root / "manual" / post.id / filename).resolve()
            if root not in target.parents:
                raise ManualPostError("Unsicherer Zielpfad wurde blockiert")
            targets.append(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(image.png)
            temporary.replace(target)
            renderer.validate(target, "feed" if kind == "carousel" else kind)
    except (OSError, RenderValidationError, ValueError) as exc:
        for target in targets:
            target.unlink(missing_ok=True)
            target.with_suffix(".tmp").unlink(missing_ok=True)
        db.rollback()
        raise ManualPostError(f"Bild konnte nicht sicher gespeichert werden: {exc}") from exc

    post.feed_path = str(targets[0]) if kind in {"feed", "carousel"} else None
    post.design_snapshot = {
        "source": "manual_upload",
        "mode": {"image": "manual", "text": "manual"},
        "manual_upload": {
            "kind": kind,
            "submission_id": submission_id,
            "images": [
                {
                    "position": position,
                    "original_filename": image.original_filename,
                    "original_mime_type": image.original_mime_type,
                    "original_checksum": image.original_checksum,
                    "final_checksum": image.png_checksum,
                    "width": image.width,
                    "height": image.height,
                }
                for position, image in enumerate(images, start=1)
            ],
            "scheduled_at": scheduled_at.isoformat(),
            "uploaded_by": user.id,
        },
    }
    publication = PublicationJob(
        post_id=post.id,
        game_id=None,
        team_id=team.id,
        instagram_page_id=page.id,
        story_rule_id=None,
        kind=kind,
        media_path=str(targets[0]),
        text_snapshot=body if kind in {"feed", "carousel"} else None,
        scheduled_at=scheduled_at,
        absolute_time=True,
        approval_status="unapproved",
        status=JobStatus.UNAPPROVED,
        idempotency_key=f"{post.id}:manual:{kind}:v1",
    )
    db.add(publication)
    db.flush()
    for position, (image, target) in enumerate(
        zip(images, targets, strict=True), start=1
    ):
        db.add(
            PublicationMediaItem(
                publication_job_id=publication.id,
                position=position,
                media_path=str(target),
                checksum=image.png_checksum,
                mime_type="image/png",
                file_size=len(image.png),
                width=image.width,
                height=image.height,
            )
        )
    db.add(
        AuditLog(
            user_id=user.id,
            team_id=team.id,
            action="manual_post.created",
            entity_type="post",
            entity_id=post.id,
            details={
                "kind": kind,
                "scheduled_at": scheduled_at.isoformat(),
                "checksums": [image.png_checksum for image in images],
                "original_filenames": [image.original_filename for image in images],
                "image_count": len(images),
            },
        )
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        for target in targets:
            target.unlink(missing_ok=True)
        raise
    return post, True
