from pathlib import Path

from app.media.storage import LocalStorageProvider, StorageError

try:
    LocalStorageProvider(Path("/app/external-media")).resolve("../etc/passwd")
except StorageError:
    raise SystemExit(0) from None
raise SystemExit("Path Traversal wurde nicht blockiert")
