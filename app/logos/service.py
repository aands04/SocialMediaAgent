import hashlib
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.games.identity import normalize_team_name, opponent_for_game
from app.models import LogoAsset

MAX_LOGO_SIZE = 5 * 1024 * 1024
MIN_DIMENSION = 32
MAX_DIMENSION = 4096
ALLOWED_SUFFIXES = {".png": "image/png", ".webp": "image/webp"}
COMPOSITOR_VERSION = "verified-logo-compositor-v1"


class LogoValidationError(ValueError):
    pass


def normalize_club_name(value: str) -> str:
    return normalize_team_name(value)


def opponent_name(game, team) -> str:
    return opponent_for_game(game, team)


def _inspect_image(data: bytes, suffix: str, content_type: str | None) -> tuple[str, int, int]:
    if not data:
        raise LogoValidationError("Die Logo-Datei ist leer.")
    if len(data) > MAX_LOGO_SIZE:
        raise LogoValidationError("Das Logo ist größer als 5 MiB.")
    expected = ALLOWED_SUFFIXES.get(suffix.lower())
    if not expected:
        raise LogoValidationError("Im MVP sind ausschließlich PNG und WebP erlaubt.")
    if content_type != expected:
        raise LogoValidationError("Dateiendung und übermittelter MIME-Type passen nicht zusammen.")
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()
            actual = {"PNG": "image/png", "WEBP": "image/webp"}.get(image.format)
            width, height = image.size
            alpha = image.convert("RGBA").getchannel("A").getextrema()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise LogoValidationError("Die Datei ist kein technisch lesbares PNG-/WebP-Bild.") from exc
    if actual != expected:
        raise LogoValidationError("Der tatsächliche Bildtyp stimmt nicht mit der Datei überein.")
    if alpha[1] == 0:
        raise LogoValidationError("Das Logo ist vollständig transparent.")
    if not (MIN_DIMENSION <= width <= MAX_DIMENSION and MIN_DIMENSION <= height <= MAX_DIMENSION):
        raise LogoValidationError(
            f"Logo-Abmessungen müssen zwischen {MIN_DIMENSION} und {MAX_DIMENSION} Pixel liegen."
        )
    return actual, width, height


def _safe_write(path: Path, data: bytes, root: Path) -> None:
    root = root.resolve()
    path = path.resolve()
    if not path.is_relative_to(root):
        raise LogoValidationError("Logo-Speicherpfad liegt außerhalb des Upload-Verzeichnisses.")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise LogoValidationError("Symbolische Links sind im Logo-Speicher nicht zulässig.")
    with path.open("xb") as handle:
        handle.write(data)


def _create_render_derivative(original: Path, target: Path, root: Path) -> None:
    with Image.open(original) as source:
        source.load()
        logo = source.convert("RGBA")
        logo.thumbnail((448, 448), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        canvas.alpha_composite(logo, ((512 - logo.width) // 2, (512 - logo.height) // 2))
        buffer = BytesIO()
        canvas.save(buffer, "PNG", optimize=True)
    _safe_write(target, buffer.getvalue(), root)


def store_logo(
    db: Session,
    *,
    upload_root: Path,
    logo_type: str,
    team_id: str | None,
    display_name: str,
    original_filename: str,
    content_type: str | None,
    data: bytes,
    uploaded_by: str,
) -> tuple[LogoAsset, bool]:
    if logo_type not in {"team", "opponent"}:
        raise LogoValidationError("Unbekannte Logoart.")
    suffix = Path(original_filename).suffix.lower()
    mime_type, width, height = _inspect_image(data, suffix, content_type)
    checksum = hashlib.sha256(data).hexdigest()
    duplicate_query = select(LogoAsset).where(
        LogoAsset.logo_type == logo_type,
        LogoAsset.checksum == checksum,
    )
    if logo_type == "team":
        duplicate_query = duplicate_query.where(LogoAsset.team_id == team_id)
    duplicate = db.scalar(duplicate_query)
    if duplicate:
        return duplicate, False
    normalized = normalize_club_name(display_name)
    if not normalized:
        raise LogoValidationError("Ein sichtbarer Vereinsname ist erforderlich.")
    latest = db.scalar(
        select(func.max(LogoAsset.version)).where(
            LogoAsset.logo_type == logo_type,
            LogoAsset.team_id == team_id,
            LogoAsset.normalized_name == normalized,
        )
    )
    version = int(latest or 0) + 1
    token = uuid4().hex
    folder = "teams" if logo_type == "team" else "opponents"
    relative_original = Path("logos") / folder / f"{token}{suffix}"
    relative_render = Path("logos") / folder / f"{token}-render.png"
    root = Path(upload_root).resolve()
    original = root / relative_original
    render = root / relative_render
    _safe_write(original, data, root)
    try:
        _create_render_derivative(original, render, root)
    except Exception:
        original.unlink(missing_ok=True)
        raise
    asset = LogoAsset(
        logo_type=logo_type,
        team_id=team_id,
        display_name=display_name.strip(),
        normalized_name=normalized,
        original_path=str(relative_original.as_posix()),
        render_path=str(relative_render.as_posix()),
        original_filename=Path(original_filename).name,
        mime_type=mime_type,
        size=len(data),
        width=width,
        height=height,
        checksum=checksum,
        version=version,
        active=True,
        uploaded_by=uploaded_by,
    )
    try:
        with db.begin_nested():
            db.add(asset)
            db.flush()
    except IntegrityError:
        original.unlink(missing_ok=True)
        render.unlink(missing_ok=True)
        duplicate = db.scalar(duplicate_query)
        if duplicate:
            return duplicate, False
        raise
    return asset, True


def snapshot(asset: LogoAsset | None) -> dict | None:
    if not asset:
        return None
    return {
        "id": asset.id,
        "type": asset.logo_type,
        "name": asset.display_name,
        "version": asset.version,
        "checksum": asset.checksum,
        "path": asset.original_path,
        "render_path": asset.render_path,
        "verified": True,
    }


def frozen_logo_set(db: Session, game, team) -> dict:
    team_logo = db.get(LogoAsset, team.logo_asset_id) if team.logo_asset_id else None
    opponent_logo = db.get(LogoAsset, game.opponent_logo_id) if game.opponent_logo_id else None
    opponent = opponent_name(game, team)
    return {
        "team": snapshot(team_logo),
        "opponent": snapshot(opponent_logo)
        or {"fallback": True, "name": opponent, "verified": False},
        "opponent_name": opponent,
        "compositor": {"version": COMPOSITOR_VERSION},
    }


def validate_frozen_logo(db: Session, item: dict | None, expected_type: str) -> LogoAsset | None:
    if not item or item.get("fallback"):
        return None
    asset = db.get(LogoAsset, item.get("id"))
    if (
        not asset
        or asset.logo_type != expected_type
        or not asset.active
        or asset.archived_at is not None
        or asset.version != item.get("version")
        or asset.checksum != item.get("checksum")
    ):
        raise LogoValidationError(
            "Eine eingefrorene Logodatei ist nicht mehr unverändert und aktiv verfügbar."
        )
    return asset


def validate_frozen_file(asset: LogoAsset | None, upload_root: Path) -> None:
    if not asset:
        return
    root = Path(upload_root).resolve()
    relative = Path(asset.original_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise LogoValidationError("Eingefrorener Logo-Pfad ist ungültig.")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
        raise LogoValidationError("Eingefrorene Logo-Datei fehlt oder ist nicht sicher lesbar.")
    if hashlib.sha256(path.read_bytes()).hexdigest() != asset.checksum:
        raise LogoValidationError("Eingefrorene Logo-Datei wurde nachträglich verändert.")


class LogoCompositor:
    positions = {
        "feed": {
            "team": (52, 55, 190, 190),
            "opponent": (838, 55, 190, 190),
            "fallback": (700, 72, 328, 150),
        },
        "story": {
            "team": (58, 92, 210, 210),
            "opponent": (812, 92, 210, 210),
            "fallback": (650, 118, 372, 160),
        },
    }

    def __init__(self, upload_root: Path):
        self.upload_root = Path(upload_root).resolve()

    def _logo(self, item: dict) -> Image.Image:
        relative = Path(str(item["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise LogoValidationError("Ungültiger Logo-Dateipfad.")
        path = (self.upload_root / relative).resolve()
        if not path.is_relative_to(self.upload_root) or path.is_symlink():
            raise LogoValidationError("Logo-Dateipfad verlässt den erlaubten Upload-Bereich.")
        if not path.is_file():
            raise LogoValidationError("Eingefrorene Logo-Datei fehlt.")
        if hashlib.sha256(path.read_bytes()).hexdigest() != item.get("checksum"):
            raise LogoValidationError("Eingefrorene Logo-Datei wurde nachträglich verändert.")
        with Image.open(path) as image:
            image.load()
            return image.convert("RGBA")

    @staticmethod
    def _fit_logo(
        image: Image.Image, box: tuple[int, int, int, int]
    ) -> tuple[Image.Image, tuple[int, int]]:
        x, y, width, height = box
        fitted = ImageOps.contain(image, (width, height), Image.Resampling.LANCZOS)
        return fitted, (x + (width - fitted.width) // 2, y + (height - fitted.height) // 2)

    def compose(
        self,
        *,
        base_path: Path,
        output_path: Path,
        kind: str,
        logos: dict,
    ) -> dict:
        if kind not in self.positions:
            raise LogoValidationError("Unbekanntes Medienformat für Logo-Komposition.")
        with Image.open(base_path) as source:
            source.load()
            canvas = source.convert("RGBA")
        layout = self.positions[kind]
        draw = ImageDraw.Draw(canvas, "RGBA")
        for key in ("team", "opponent"):
            item = logos.get(key)
            if item and not item.get("fallback"):
                logo = self._logo(item)
                fitted, position = self._fit_logo(logo, layout[key])
                padding = 12
                draw.rounded_rectangle(
                    (
                        position[0] - padding,
                        position[1] - padding,
                        position[0] + fitted.width + padding,
                        position[1] + fitted.height + padding,
                    ),
                    radius=18,
                    fill=(7, 15, 35, 185),
                    outline=(255, 255, 255, 205),
                    width=3,
                )
                canvas.alpha_composite(fitted, position)
        opponent = logos.get("opponent") or {}
        if opponent.get("fallback"):
            x, y, width, height = layout["fallback"]
            draw.rounded_rectangle((x, y, x + width, y + height), 18, fill=(7, 15, 35, 220))
            name = str(opponent.get("name") or "Gegner")
            font = ImageFont.load_default(size=28)
            wrapped = "\n".join([name[i : i + 20] for i in range(0, min(len(name), 60), 20)])
            draw.multiline_text(
                (x + width // 2, y + height // 2),
                wrapped,
                fill="white",
                font=font,
                anchor="mm",
                align="center",
                spacing=6,
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.convert("RGB").save(output_path, "PNG", optimize=True)
        return {
            "version": COMPOSITOR_VERSION,
            "positions": layout,
            "scale_mode": "contain",
        }
