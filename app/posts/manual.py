import hashlib
import json
import math
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
from app.meta.user_tags import UserTagValidationError, parse_user_tag_specs
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
MAX_MANUAL_SOURCE_PIXELS = 50_000_000
MAX_MANUAL_SOURCE_DIMENSION = 12_000
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
    original: bytes
    original_extension: str
    source_width: int
    source_height: int
    crop: dict[str, float]
    png: bytes
    png_checksum: str
    width: int
    height: int


def parse_manual_crop_specs(value: str | None, image_count: int) -> list[dict[str, float] | None]:
    if not value or not value.strip():
        return [None] * image_count
    try:
        raw = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ManualPostError(
            "Zuschneidedaten sind ungültig; Bilder bitte erneut ausrichten"
        ) from exc
    if not isinstance(raw, list) or len(raw) != image_count:
        raise ManualPostError("Zuschneidedaten passen nicht zu den ausgewählten Bildern")
    result: list[dict[str, float] | None] = []
    for item in raw:
        if item is None:
            result.append(None)
            continue
        if not isinstance(item, dict):
            raise ManualPostError("Zuschneidedaten sind ungültig")
        try:
            crop = {key: float(item[key]) for key in ("x", "y", "width", "height")}
        except (KeyError, TypeError, ValueError) as exc:
            raise ManualPostError("Zuschneidedaten sind unvollständig") from exc
        if not all(math.isfinite(number) for number in crop.values()):
            raise ManualPostError("Zuschneidedaten enthalten ungültige Werte")
        if (
            crop["x"] < 0
            or crop["y"] < 0
            or crop["width"] <= 0
            or crop["height"] <= 0
            or crop["x"] + crop["width"] > 1.000001
            or crop["y"] + crop["height"] > 1.000001
        ):
            raise ManualPostError("Zuschneidebereich liegt außerhalb des Bildes")
        result.append(crop)
    return result


def parse_manual_user_tag_specs(
    value: str | None,
    image_count: int,
    kind: str,
) -> list[list[dict[str, float | str]]]:
    try:
        return parse_user_tag_specs(
            value,
            image_count,
            allow_tags=kind in {"feed", "carousel"},
        )
    except UserTagValidationError as exc:
        raise ManualPostError(str(exc)) from exc


def _crop_box(
    source_size: tuple[int, int],
    target_size: tuple[int, int],
    crop: dict[str, float] | None,
) -> tuple[tuple[int, int, int, int], dict[str, float]]:
    source_width, source_height = source_size
    target_width, target_height = target_size
    target_ratio = target_width / target_height
    if crop is None:
        if source_width / source_height > target_ratio:
            crop_height = source_height
            crop_width = crop_height * target_ratio
        else:
            crop_width = source_width
            crop_height = crop_width / target_ratio
        left = (source_width - crop_width) / 2
        top = (source_height - crop_height) / 2
        normalized = {
            "x": left / source_width,
            "y": top / source_height,
            "width": crop_width / source_width,
            "height": crop_height / source_height,
        }
    else:
        normalized = crop.copy()
        supplied_ratio = (normalized["width"] * source_width) / (
            normalized["height"] * source_height
        )
        if not math.isclose(supplied_ratio, target_ratio, rel_tol=0.01, abs_tol=0.01):
            raise ManualPostError("Zuschneidebereich besitzt nicht das gewählte Instagram-Format")
    left = max(0, round(normalized["x"] * source_width))
    top = max(0, round(normalized["y"] * source_height))
    right = min(source_width, round((normalized["x"] + normalized["width"]) * source_width))
    bottom = min(
        source_height,
        round((normalized["y"] + normalized["height"]) * source_height),
    )
    if right - left < 2 or bottom - top < 2:
        raise ManualPostError("Zuschneidebereich ist zu klein")
    return (left, top, right, bottom), normalized


def validate_manual_image(
    filename: str,
    content_type: str | None,
    content: bytes,
    kind: str,
    crop: dict[str, float] | None = None,
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
            if (
                image.width > MAX_MANUAL_SOURCE_DIMENSION
                or image.height > MAX_MANUAL_SOURCE_DIMENSION
                or image.width * image.height > MAX_MANUAL_SOURCE_PIXELS
            ):
                raise ManualPostError("Bildabmessungen sind zu groß")
            image.load()
            if "A" in image.getbands():
                normalized = image.convert("RGBA")
                if normalized.getchannel("A").getextrema() == (0, 0):
                    raise ManualPostError("Bild ist vollständig transparent")
            else:
                normalized = image.convert("RGB")
            source_width, source_height = normalized.size
            box, effective_crop = _crop_box(normalized.size, MANUAL_IMAGE_SIZES[kind], crop)
            normalized = normalized.crop(box).resize(
                MANUAL_IMAGE_SIZES[kind], Image.Resampling.LANCZOS
            )
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
        original=content,
        original_extension=suffix,
        source_width=source_width,
        source_height=source_height,
        crop=effective_crop,
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
    user_tags_by_image: list[list[dict[str, float | str]]] | None = None,
) -> tuple[Post, bool]:
    if not _SUBMISSION_PATTERN.fullmatch(submission_id or ""):
        raise ManualPostError("Ungültige Formular-ID; Seite bitte neu laden")
    body = text.strip()
    if not body:
        raise ManualPostError("Text darf nicht leer sein")
    if len(body) > MAX_MANUAL_TEXT_CHARS:
        raise ManualPostError(f"Text darf höchstens {MAX_MANUAL_TEXT_CHARS} Zeichen enthalten")
    if kind not in MANUAL_IMAGE_SIZES:
        raise ManualPostError("Medienart muss Feed, Karussell oder Story sein")
    if kind == "carousel" and not 2 <= len(images) <= MAX_CAROUSEL_IMAGES:
        raise ManualPostError("Ein Karussell benötigt 2 bis 10 Bilder")
    if kind != "carousel" and len(images) != 1:
        raise ManualPostError("Feed und Story benötigen genau ein Bild")
    if user_tags_by_image is None:
        user_tags_by_image = [[] for _ in images]
    try:
        user_tags_by_image = parse_user_tag_specs(
            json.dumps(user_tags_by_image),
            len(images),
            allow_tags=kind in {"feed", "carousel"},
        )
    except UserTagValidationError as exc:
        raise ManualPostError(str(exc)) from exc
    existing = db.scalar(select(Post).where(Post.manual_submission_id == submission_id))
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
        existing = db.scalar(select(Post).where(Post.manual_submission_id == submission_id))
        if existing:
            return existing, False
        raise ManualPostError("Beitrag konnte nicht eindeutig angelegt werden") from exc

    root = settings.generated_root.resolve()
    targets: list[Path] = []
    original_targets: list[Path] = []
    try:
        renderer = Renderer(root, settings.media_root, settings.upload_root)
        for position, image in enumerate(images, start=1):
            filename = f"carousel-{position:02d}-v1.png" if kind == "carousel" else f"{kind}-v1.png"
            target = (root / "manual" / post.id / filename).resolve()
            if root not in target.parents:
                raise ManualPostError("Unsicherer Zielpfad wurde blockiert")
            targets.append(target)
            original_target = (
                root
                / "manual"
                / post.id
                / "originals"
                / f"original-{position:02d}-{image.original_checksum[:12]}{image.original_extension}"
            ).resolve()
            if root not in original_target.parents:
                raise ManualPostError("Unsicherer Originalpfad wurde blockiert")
            original_targets.append(original_target)
            original_target.parent.mkdir(parents=True, exist_ok=True)
            original_temporary = original_target.with_suffix(original_target.suffix + ".tmp")
            original_temporary.write_bytes(image.original)
            original_temporary.replace(original_target)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(image.png)
            temporary.replace(target)
            renderer.validate(target, "feed" if kind == "carousel" else kind)
    except (OSError, RenderValidationError, ValueError) as exc:
        for target in targets:
            target.unlink(missing_ok=True)
            target.with_suffix(".tmp").unlink(missing_ok=True)
        for target in original_targets:
            target.unlink(missing_ok=True)
            target.with_suffix(target.suffix + ".tmp").unlink(missing_ok=True)
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
                    "user_tags": user_tags_by_image[position - 1],
                }
                | {
                    "source_width": image.source_width,
                    "source_height": image.source_height,
                    "crop": image.crop,
                    "original_path": str(original_targets[position - 1]),
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
    for position, (image, target) in enumerate(zip(images, targets, strict=True), start=1):
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
                "instagram_user_tag_count": sum(len(tags) for tags in user_tags_by_image),
            },
        )
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        for target in targets:
            target.unlink(missing_ok=True)
        for target in original_targets:
            target.unlink(missing_ok=True)
        raise
    return post, True
