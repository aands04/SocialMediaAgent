from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import MediaAsset

ALLOWED={".jpg",".jpeg",".png",".webp",".woff2",".ttf"}
class StorageError(ValueError): pass
class StorageProvider(ABC):
    def __init__(self,root:Path): self.root=root.resolve()
    def resolve(self,relative:str)->Path:
        candidate=Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts: raise StorageError("Nur relative, sichere Pfade sind erlaubt")
        result=(self.root/candidate).resolve()
        if result!=self.root and self.root not in result.parents: raise StorageError("Pfad verlässt Medienwurzel")
        if result.exists() and result.is_symlink(): raise StorageError("Symbolische Links sind nicht erlaubt")
        return result
    def validate_file(self,relative:str)->Path:
        path=self.resolve(relative)
        if path.suffix.lower() not in ALLOWED or not path.is_file(): raise StorageError("Ungültige oder fehlende Datei")
        return path
    @abstractmethod
    def available(self)->bool: ...
class LocalStorageProvider(StorageProvider):
    def available(self)->bool: return self.root.is_dir()
class SmbStorageProvider(LocalStorageProvider):
    """Sicherer Zugriff auf ein vom Host eingebundenes SMB-Volume; keine Credentials."""


def media_asset_path(asset: MediaAsset, media_root: Path, upload_root: Path) -> Path:
    """Resolve a media asset below its declared storage root without following escapes."""
    roots = {
        "external": Path(media_root).resolve(),
        "upload": Path(upload_root).resolve(),
    }
    root = roots.get(asset.storage_kind)
    if root is None:
        raise StorageError("Unbekannte Medienquelle")
    relative = Path(asset.relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise StorageError("Nur relative, sichere Medienpfade sind erlaubt")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path.is_symlink():
        raise StorageError("Medienpfad verlässt den erlaubten Speicherbereich")
    if not path.is_file():
        raise StorageError("Mediendatei ist nicht verfügbar")
    return path
