"""Read-only preflight for migrating a legacy installation to revision 0016+."""

from __future__ import annotations

import json
import os
import re
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import create_engine, inspect, text

from app.config import get_settings

TENANT_TABLES = (
    "users",
    "instagram_pages",
    "teams",
    "games",
    "logo_assets",
    "media_assets",
    "story_rules",
    "posts",
    "publication_jobs",
    "generation_jobs",
    "audit_logs",
    "provider_snapshots",
)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:100]


def main() -> int:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        counts = {
            name: int(connection.execute(text(f'SELECT count(*) FROM "{name}"')).scalar_one())
            for name in TENANT_TABLES
            if name in tables
        }
        names: set[str] = set()
        for table in ("teams", "instagram_pages"):
            if table not in tables:
                continue
            columns = {column["name"] for column in inspector.get_columns(table)}
            if "club" not in columns:
                continue
            names.update(
                str(value).strip()
                for value in connection.execute(
                    text(f'SELECT DISTINCT club FROM "{table}" WHERE club IS NOT NULL')
                ).scalars()
                if str(value).strip()
            )

        configured_name = (os.getenv("INITIAL_CLUB_NAME") or "").strip()
        planned_name = configured_name or (next(iter(names)) if len(names) == 1 else None)
        planned_slug = (os.getenv("INITIAL_CLUB_SLUG") or _slug(planned_name or "")).strip()
        configured_id = (os.getenv("INITIAL_CLUB_ID") or "").strip()
        errors: list[str] = []
        if any(counts.values()) and not planned_name:
            errors.append("Bestandsdaten können keinem eindeutigen Vereinsnamen zugeordnet werden")
        if len({name.casefold() for name in names}) > 1 and not configured_name:
            errors.append("Mehrere Vereinsnamen gefunden; explizite Zuordnung erforderlich")
        if configured_name and any(name.casefold() != configured_name.casefold() for name in names):
            errors.append("INITIAL_CLUB_NAME widerspricht mindestens einem vorhandenen Vereinsnamen")
        try:
            planned_id = (
                str(UUID(configured_id))
                if configured_id
                else str(uuid5(NAMESPACE_URL, f"social-media-agent:club:{planned_slug}"))
                if planned_slug
                else None
            )
        except ValueError:
            planned_id = None
            errors.append("INITIAL_CLUB_ID ist keine gültige UUID")

        report = {
            "status": "blocked" if errors else "ready",
            "database_dialect": connection.dialect.name,
            "found_club_names": sorted(names, key=str.casefold),
            "row_counts": counts,
            "planned_initial_club": {
                "id": planned_id,
                "name": planned_name,
                "short_name": (os.getenv("INITIAL_CLUB_SHORT_NAME") or "").strip() or None,
                "slug": planned_slug or None,
            },
            "errors": errors,
            "note": "Nur Vorprüfung; es wurden keine Daten verändert.",
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
