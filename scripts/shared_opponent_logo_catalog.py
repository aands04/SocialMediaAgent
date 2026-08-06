"""Idempotently copy existing verified opponent logos into the global catalog."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.logos.service import LogoValidationError, publish_shared_opponent_logo
from app.models import LogoAsset
from app.tenancy.state import system_scope


def _source_bytes(source: LogoAsset, upload_root: Path) -> bytes:
    root = Path(upload_root).resolve()
    relative = Path(source.original_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise LogoValidationError("Logo-Pfad ist ungültig")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
        raise LogoValidationError("Logo-Datei ist nicht sicher verfügbar")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != source.checksum:
        raise LogoValidationError("Logo-Datei wurde nachträglich verändert")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bestehende Gegnerlogos in den systemweiten, verifizierten Katalog kopieren"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Kopien tatsächlich anlegen; ohne diese Option erfolgt nur eine Vorprüfung",
    )
    args = parser.parse_args()
    settings = get_settings()
    checked = created = existing = failed = 0

    with system_scope("systemweiten Gegnerlogo-Katalog abgleichen"), SessionLocal() as db:
        sources = list(
            db.scalars(
                select(LogoAsset)
                .where(
                    LogoAsset.logo_type == "opponent",
                    LogoAsset.active.is_(True),
                    LogoAsset.archived_at.is_(None),
                )
                .order_by(LogoAsset.created_at, LogoAsset.id)
            )
        )
        for source in sources:
            checked += 1
            try:
                data = _source_bytes(source, settings.upload_root)
                if not args.apply:
                    continue
                _, was_created = publish_shared_opponent_logo(
                    db,
                    upload_root=settings.upload_root,
                    source=source,
                    data=data,
                )
                db.commit()
                created += int(was_created)
                existing += int(not was_created)
            except (LogoValidationError, OSError) as exc:
                db.rollback()
                failed += 1
                print(f"FEHLER {source.id}: {exc}")

    mode = "angewendet" if args.apply else "nur geprüft"
    print(
        f"Gegnerlogo-Katalog {mode}: geprüft={checked}, neu={created}, "
        f"bereits_vorhanden={existing}, fehler={failed}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
