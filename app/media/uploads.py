import hashlib
import os
import re
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

MAX_PLAYER_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PLAYER_IMAGE_FILES = 25
MIN_PLAYER_IMAGE_EDGE = 320
MAX_PLAYER_IMAGE_EDGE = 8000

_FORMATS = {
    "JPEG": ({".jpg", ".jpeg"}, {"image/jpeg", "image/jpg"}, ".jpg", "image/jpeg"),
    "PNG": ({".png"}, {"image/png"}, ".png", "image/png"),
    "WEBP": ({".webp"}, {"image/webp"}, ".webp", "image/webp"),
}


class PlayerImageUploadError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedPlayerImage:
    original_filename: str
    player_name: str
    content: bytes
    mime_type: str
    extension: str
    width: int
    height: int
    checksum: str


def _safe_original_name(value: str) -> str:
    name = Path((value or "").replace("\\", "/")).name.strip()
    if not name or name in {".", ".."}:
        raise PlayerImageUploadError("Dateiname fehlt")
    return name[:255]


def validate_player_image(
    filename: str,
    content_type: str | None,
    content: bytes,
) -> ValidatedPlayerImage:
    name = _safe_original_name(filename)
    if not content:
        raise PlayerImageUploadError(f"{name}: Datei ist leer")
    if len(content) > MAX_PLAYER_IMAGE_BYTES:
        raise PlayerImageUploadError(f"{name}: maximal 20 MiB pro Bild erlaubt")

    extension = Path(name).suffix.lower()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as probe:
                image_format = probe.format
                frame_count = getattr(probe, "n_frames", 1)
                width, height = probe.size
                probe.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombWarning) as exc:
        raise PlayerImageUploadError(f"{name}: keine technisch lesbare Bilddatei") from exc

    if image_format not in _FORMATS:
        raise PlayerImageUploadError(f"{name}: nur JPG, PNG und WebP sind erlaubt")
    extensions, mime_types, canonical_extension, canonical_mime = _FORMATS[image_format]
    if extension not in extensions:
        raise PlayerImageUploadError(f"{name}: Dateiendung passt nicht zum Bildinhalt")
    if (content_type or "").lower() not in mime_types:
        raise PlayerImageUploadError(f"{name}: MIME-Type passt nicht zum Bildinhalt")
    if frame_count != 1:
        raise PlayerImageUploadError(f"{name}: animierte Bilder sind nicht erlaubt")
    if min(width, height) < MIN_PLAYER_IMAGE_EDGE:
        raise PlayerImageUploadError(
            f"{name}: Mindestgröße ist {MIN_PLAYER_IMAGE_EDGE} Pixel je Bildkante"
        )
    if max(width, height) > MAX_PLAYER_IMAGE_EDGE:
        raise PlayerImageUploadError(
            f"{name}: maximale Bildkante ist {MAX_PLAYER_IMAGE_EDGE} Pixel"
        )

    player_name = re.sub(r"[_-]+", " ", Path(name).stem).strip()[:160]
    return ValidatedPlayerImage(
        original_filename=name,
        player_name=player_name,
        content=content,
        mime_type=canonical_mime,
        extension=canonical_extension,
        width=width,
        height=height,
        checksum=hashlib.sha256(content).hexdigest(),
    )


def store_player_image(
    upload_root: Path,
    team_id: str,
    image: ValidatedPlayerImage,
) -> tuple[str, Path]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", team_id):
        raise PlayerImageUploadError("Ungültige Mannschafts-ID")
    root = Path(upload_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise PlayerImageUploadError("Upload-Wurzel darf kein symbolischer Link sein")
    folder = root / "player-images" / team_id
    folder.mkdir(parents=True, exist_ok=True)
    folder = folder.resolve()
    if not folder.is_relative_to(root) or folder.is_symlink():
        raise PlayerImageUploadError("Unsicherer Upload-Zielordner")

    target = folder / f"{uuid4().hex}{image.extension}"
    relative = target.relative_to(root).as_posix()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o640)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(image.content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return relative, target
