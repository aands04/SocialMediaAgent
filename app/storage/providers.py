from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO

from app.config import Settings


class ObjectStorageError(RuntimeError):
    pass


class ObjectStorageProvider(ABC):
    name: str
    bucket: str

    @abstractmethod
    def put(self, object_key: str, body: bytes | BinaryIO, mime_type: str) -> None: ...

    @abstractmethod
    def get(self, object_key: str) -> bytes: ...

    @abstractmethod
    def head(self, object_key: str) -> dict: ...

    @abstractmethod
    def delete(self, object_key: str) -> None: ...

    @abstractmethod
    def list(self, prefix: str) -> list[dict]: ...

    @abstractmethod
    def presign_put(self, object_key: str, mime_type: str, expires_in: int) -> str: ...

    @abstractmethod
    def presign_get(self, object_key: str, expires_in: int) -> str: ...


def _safe_key(object_key: str) -> str:
    clean = object_key.strip().replace("\\", "/")
    if not clean or clean.startswith("/") or ".." in clean.split("/"):
        raise ObjectStorageError("Unsicherer Objektschlüssel")
    return clean


class LocalObjectStorageProvider(ObjectStorageProvider):
    name = "local"

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.bucket = "local-private"

    def _path(self, object_key: str) -> Path:
        path = (self.root / _safe_key(object_key)).resolve()
        if not path.is_relative_to(self.root) or path.is_symlink():
            raise ObjectStorageError("Objektpfad verlässt den privaten Speicher")
        return path

    def put(self, object_key: str, body: bytes | BinaryIO, mime_type: str) -> None:
        path = self._path(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = body if isinstance(body, bytes) else body.read()
        path.write_bytes(payload)
        path.with_suffix(path.suffix + ".content-type").write_text(mime_type, encoding="utf-8")

    def get(self, object_key: str) -> bytes:
        path = self._path(object_key)
        if not path.is_file():
            raise ObjectStorageError("Objekt ist nicht vorhanden")
        return path.read_bytes()

    def head(self, object_key: str) -> dict:
        path = self._path(object_key)
        if not path.is_file():
            raise ObjectStorageError("Objekt ist nicht vorhanden")
        mime_path = path.with_suffix(path.suffix + ".content-type")
        return {
            "size": path.stat().st_size,
            "content_type": mime_path.read_text(encoding="utf-8") if mime_path.exists() else None,
        }

    def delete(self, object_key: str) -> None:
        path = self._path(object_key)
        path.unlink(missing_ok=True)
        path.with_suffix(path.suffix + ".content-type").unlink(missing_ok=True)

    def list(self, prefix: str) -> list[dict]:
        base = self._path(_safe_key(prefix))
        if not base.exists():
            return []
        return [
            {
                "key": path.relative_to(self.root).as_posix(),
                "size": path.stat().st_size,
            }
            for path in base.rglob("*")
            if path.is_file() and not path.name.endswith(".content-type") and not path.is_symlink()
        ]

    def presign_put(self, object_key: str, mime_type: str, expires_in: int) -> str:
        raise ObjectStorageError("Lokale Uploads verwenden den geschützten Upload-Endpunkt")

    def presign_get(self, object_key: str, expires_in: int) -> str:
        raise ObjectStorageError("Lokale Objekte werden nicht direkt öffentlich signiert")


class SmbImportProvider(ObjectStorageProvider):
    """Read-only adapter for an existing SMB mount used as an import source."""

    name = "smb-import"
    bucket = "smb-read-only"

    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_dir():
            raise ObjectStorageError("SMB-Importwurzel ist nicht vorhanden")

    def _path(self, object_key: str) -> Path:
        path = (self.root / _safe_key(object_key)).resolve()
        if not path.is_relative_to(self.root) or path.is_symlink():
            raise ObjectStorageError("Importpfad verlässt die SMB-Importwurzel")
        return path

    def put(self, object_key: str, body: bytes | BinaryIO, mime_type: str) -> None:
        raise ObjectStorageError("SMB ist ausschließlich eine lesende Importquelle")

    def get(self, object_key: str) -> bytes:
        path = self._path(object_key)
        if not path.is_file():
            raise ObjectStorageError("Importobjekt ist nicht vorhanden")
        return path.read_bytes()

    def head(self, object_key: str) -> dict:
        path = self._path(object_key)
        if not path.is_file():
            raise ObjectStorageError("Importobjekt ist nicht vorhanden")
        return {"size": path.stat().st_size, "content_type": None}

    def delete(self, object_key: str) -> None:
        raise ObjectStorageError("SMB-Importobjekte dürfen nicht gelöscht werden")

    def list(self, prefix: str) -> list[dict]:
        base = self._path(prefix)
        if not base.exists():
            return []
        if base.is_file():
            paths = [base]
        else:
            paths = base.rglob("*")
        return [
            {"key": path.relative_to(self.root).as_posix(), "size": path.stat().st_size}
            for path in paths
            if path.is_file() and not path.is_symlink()
        ]

    def presign_put(self, object_key: str, mime_type: str, expires_in: int) -> str:
        raise ObjectStorageError("SMB unterstützt keine direkten Uploads")

    def presign_get(self, object_key: str, expires_in: int) -> str:
        raise ObjectStorageError("SMB-Importobjekte werden nicht öffentlich signiert")


class S3CompatibleStorageProvider(ObjectStorageProvider):
    name = "s3"

    def __init__(
        self,
        *,
        endpoint_url: str,
        region: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
    ):
        import boto3

        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )

    def put(self, object_key: str, body: bytes | BinaryIO, mime_type: str) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=_safe_key(object_key),
            Body=body,
            ContentType=mime_type,
        )

    def get(self, object_key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=_safe_key(object_key))["Body"].read()

    def head(self, object_key: str) -> dict:
        result = self.client.head_object(Bucket=self.bucket, Key=_safe_key(object_key))
        return {"size": int(result["ContentLength"]), "content_type": result.get("ContentType")}

    def delete(self, object_key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=_safe_key(object_key))

    def list(self, prefix: str) -> list[dict]:
        result: list[dict] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=_safe_key(prefix)):
            result.extend(
                {"key": item["Key"], "size": int(item["Size"])}
                for item in page.get("Contents", [])
            )
        return result

    def presign_put(self, object_key: str, mime_type: str, expires_in: int) -> str:
        return self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": _safe_key(object_key), "ContentType": mime_type},
            ExpiresIn=expires_in,
        )

    def presign_get(self, object_key: str, expires_in: int) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": _safe_key(object_key)},
            ExpiresIn=expires_in,
        )


class CloudflareR2StorageProvider(S3CompatibleStorageProvider):
    name = "cloudflare-r2"


class HetznerObjectStorageProvider(S3CompatibleStorageProvider):
    name = "hetzner-object-storage"


def build_object_storage_provider(settings: Settings) -> ObjectStorageProvider:
    provider_name = settings.object_storage_provider.casefold()
    if provider_name == "local":
        return LocalObjectStorageProvider(settings.upload_root / "private-objects")
    required = {
        "endpoint_url": settings.s3_endpoint_url,
        "bucket": settings.s3_bucket,
        "access_key_id": settings.s3_access_key_id,
        "secret_access_key": settings.s3_secret_access_key,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ObjectStorageError("S3-Konfiguration unvollständig: " + ", ".join(missing))
    cls = {
        "s3": S3CompatibleStorageProvider,
        "cloudflare-r2": CloudflareR2StorageProvider,
        "hetzner": HetznerObjectStorageProvider,
        "hetzner-object-storage": HetznerObjectStorageProvider,
    }.get(provider_name)
    if cls is None:
        raise ObjectStorageError("Unbekannter Objektspeicher-Provider")
    return cls(region=settings.s3_region, **required)  # type: ignore[arg-type]


def build_smb_import_provider(settings: Settings) -> SmbImportProvider:
    return SmbImportProvider(settings.media_root)
