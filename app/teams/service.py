from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Team

_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9_-]+")
_TEAM_MEDIA_FOLDERS = ("players", "logos", "backgrounds", "imports")


def slugify_team_name(value: str) -> str:
    """Return a readable, path-safe technical slug for a team name."""

    normalized = unicodedata.normalize("NFKD", (value or "").replace("ß", "ss"))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    return (slug or "mannschaft")[:80].rstrip("-")


def unique_team_slug(db: Session, club_id: str, value: str) -> str:
    """Create a tenant-local slug without requiring it from the user."""

    base = slugify_team_name(value)
    used = set(db.scalars(select(Team.slug).where(Team.club_id == club_id)).all())
    if base not in used:
        return base
    for number in range(2, 10_000):
        suffix = f"-{number}"
        candidate = f"{base[: 80 - len(suffix)].rstrip('-')}{suffix}"
        if candidate not in used:
            return candidate
    raise ValueError(
        "Für diese Mannschaft konnte keine eindeutige technische Kennung erzeugt werden"
    )


def derived_team_short_name(internal_name: str, display_name: str) -> str:
    """Keep the legacy field populated without making users maintain it."""

    value = (internal_name or display_name or "Mannschaft").strip()
    return value[:30]


def team_media_prefix(club_id: str, team_id: str, slug: str) -> Path:
    """Build the immutable tenant/team namespace used for new managed media."""

    if not _SAFE_COMPONENT.fullmatch(club_id or ""):
        raise ValueError("Ungültige Vereins-ID für den Medienbereich")
    if not _SAFE_COMPONENT.fullmatch(team_id or ""):
        raise ValueError("Ungültige Mannschafts-ID für den Medienbereich")
    safe_slug = slugify_team_name(slug)
    return Path("clubs") / club_id / "teams" / f"{team_id}-{safe_slug}"


def ensure_team_media_namespace(
    upload_root: Path,
    *,
    club_id: str,
    team_id: str,
    slug: str,
) -> str:
    """Create the managed local namespace and return the legacy import subdir.

    S3-compatible storage uses the same immutable club UUID principle. These
    local folders serve dashboard uploads and the optional local/SMB import
    compatibility layer; users never need to enter a path themselves.
    """

    root = Path(upload_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("Upload-Wurzel darf kein symbolischer Link sein")

    relative = team_media_prefix(club_id, team_id, slug)
    namespace = (root / relative).resolve()
    if not namespace.is_relative_to(root):
        raise ValueError("Unsicherer Mannschafts-Medienbereich")
    if namespace.exists() and namespace.is_symlink():
        raise ValueError("Mannschafts-Medienbereich darf kein symbolischer Link sein")
    namespace.mkdir(parents=True, exist_ok=True)

    for folder_name in _TEAM_MEDIA_FOLDERS:
        folder = namespace / folder_name
        if folder.exists() and folder.is_symlink():
            raise ValueError("Medien-Unterordner darf kein symbolischer Link sein")
        folder.mkdir(parents=True, exist_ok=True)
        if not folder.resolve().is_relative_to(root):
            raise ValueError("Unsicherer Medien-Unterordner")

    return (relative / "imports").as_posix()
