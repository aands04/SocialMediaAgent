import hashlib
import os
import re
import stat
import warnings
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from app.teams.service import team_media_prefix

MAX_PLAYER_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PLAYER_IMAGE_FILES = 25
MAX_PLAYER_IMAGE_ARCHIVE_BYTES = 500 * 1024 * 1024
MAX_PLAYER_IMAGE_ARCHIVE_ENTRIES = 1000
MAX_ZIP_COMPRESSION_RATIO = 200
MIN_PLAYER_IMAGE_EDGE = 320
MAX_PLAYER_IMAGE_EDGE = 8000

_ZIP_MIME_TYPES = {
    "application/zip",
    "application/x-zip-compressed",
    "application/octet-stream",
}
_ZIP_IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

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


def iter_player_images_from_zip(
    filename: str,
    content_type: str | None,
    source: BinaryIO,
) -> Iterator[ValidatedPlayerImage]:
    archive_name = _safe_original_name(filename)
    if Path(archive_name).suffix.lower() != ".zip":
        raise PlayerImageUploadError("Das Bildarchiv muss die Dateiendung .zip besitzen")
    if (content_type or "").lower() not in _ZIP_MIME_TYPES:
        raise PlayerImageUploadError("MIME-Type des ZIP-Archivs ist nicht erlaubt")

    source.seek(0, os.SEEK_END)
    archive_size = source.tell()
    source.seek(0)
    if not archive_size:
        raise PlayerImageUploadError("ZIP-Archiv ist leer")
    if archive_size > MAX_PLAYER_IMAGE_ARCHIVE_BYTES:
        raise PlayerImageUploadError("ZIP-Archiv darf maximal 500 MiB groß sein")

    try:
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_PLAYER_IMAGE_ARCHIVE_ENTRIES:
                raise PlayerImageUploadError("ZIP-Archiv enthält zu viele Einträge")

            images = 0
            unpacked_size = 0
            for entry in entries:
                raw_name = entry.filename.replace("\\", "/")
                archive_path = PurePosixPath(raw_name)
                if archive_path.is_absolute() or ".." in archive_path.parts:
                    raise PlayerImageUploadError(f"{entry.filename}: unsicherer Pfad im ZIP-Archiv")
                if entry.is_dir():
                    continue
                if "__MACOSX" in archive_path.parts or archive_path.name in {
                    ".DS_Store",
                    "Thumbs.db",
                }:
                    continue
                if stat.S_ISLNK(entry.external_attr >> 16):
                    raise PlayerImageUploadError(
                        f"{entry.filename}: symbolische Links sind im ZIP-Archiv nicht erlaubt"
                    )
                if entry.flag_bits & 0x1:
                    raise PlayerImageUploadError(
                        f"{entry.filename}: verschlüsselte ZIP-Einträge sind nicht erlaubt"
                    )
                if entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise PlayerImageUploadError(
                        f"{entry.filename}: nicht unterstützte ZIP-Kompression"
                    )

                extension = archive_path.suffix.lower()
                mime_type = _ZIP_IMAGE_MIME_TYPES.get(extension)
                if mime_type is None:
                    raise PlayerImageUploadError(
                        f"{entry.filename}: ZIP-Archiv darf nur JPG, PNG und WebP enthalten"
                    )
                images += 1
                if images > MAX_PLAYER_IMAGE_FILES:
                    raise PlayerImageUploadError(
                        f"ZIP-Archiv darf höchstens {MAX_PLAYER_IMAGE_FILES} Bilder enthalten"
                    )
                if entry.file_size > MAX_PLAYER_IMAGE_BYTES:
                    raise PlayerImageUploadError(
                        f"{entry.filename}: maximal 20 MiB pro Bild erlaubt"
                    )
                unpacked_size += entry.file_size
                if unpacked_size > MAX_PLAYER_IMAGE_FILES * MAX_PLAYER_IMAGE_BYTES:
                    raise PlayerImageUploadError(
                        "Entpackte Bilder überschreiten die erlaubte Gesamtgröße"
                    )
                if entry.file_size and (
                    not entry.compress_size
                    or entry.file_size / entry.compress_size > MAX_ZIP_COMPRESSION_RATIO
                ):
                    raise PlayerImageUploadError(
                        f"{entry.filename}: verdächtiges ZIP-Kompressionsverhältnis"
                    )

                with archive.open(entry) as handle:
                    content = handle.read(MAX_PLAYER_IMAGE_BYTES + 1)
                yield validate_player_image(
                    archive_path.name,
                    mime_type,
                    content,
                )
            if not images:
                raise PlayerImageUploadError("ZIP-Archiv enthält keine unterstützten Bilder")
    except PlayerImageUploadError:
        raise
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
        raise PlayerImageUploadError("ZIP-Archiv ist beschädigt oder nicht lesbar") from exc


def store_player_image(
    upload_root: Path,
    team_id: str,
    image: ValidatedPlayerImage,
    *,
    club_id: str | None = None,
    team_slug: str | None = None,
) -> tuple[str, Path]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", team_id):
        raise PlayerImageUploadError("Ungültige Mannschafts-ID")
    root = Path(upload_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise PlayerImageUploadError("Upload-Wurzel darf kein symbolischer Link sein")
    if club_id:
        folder = root / team_media_prefix(club_id, team_id, team_slug or team_id) / "players"
    else:
        # Existing integrations and old tests may still use the legacy helper
        # signature. New dashboard uploads always pass a club_id.
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


def move_uploaded_media_to_team(
    upload_root: Path,
    relative_path: str,
    *,
    club_id: str,
    team_id: str,
    team_slug: str,
) -> tuple[str, Path]:
    """Move an uploaded media object into another team namespace safely."""

    if not re.fullmatch(r"[A-Za-z0-9_-]+", team_id):
        raise PlayerImageUploadError("Ungültige Mannschafts-ID")
    root = Path(upload_root).resolve()
    source = (root / relative_path).resolve()
    if not source.is_relative_to(root) or not source.is_file() or source.is_symlink():
        raise PlayerImageUploadError("Die Mediendatei ist nicht sicher verschiebbar")
    folder = (root / team_media_prefix(club_id, team_id, team_slug) / "players").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if not folder.is_relative_to(root) or folder.is_symlink():
        raise PlayerImageUploadError("Unsicherer Zielordner")
    suffix = source.suffix.lower()
    target = folder / f"{uuid4().hex}{suffix}"
    source.replace(target)
    return target.relative_to(root).as_posix(), target
