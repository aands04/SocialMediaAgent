"""SaaS storage/usage ledgers, prompt overrides and billing preparation.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa

from alembic import op
from app.db import Base

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


TABLES = (
    "club_prompt_overrides",
    "storage_objects",
    "storage_ledger_entries",
    "usage_ledger_entries",
    "prompt_test_runs",
    "registration_intents",
    "club_subscriptions",
)


def _names(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _report(bind, status: str, created: list[str]) -> None:
    payload = {
        "revision": revision,
        "status": status,
        "created_tables": created,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    exists = bind.execute(
        sa.text("SELECT COUNT(*) FROM tenant_migration_reports WHERE migration_revision=:revision"),
        {"revision": revision},
    ).scalar_one()
    if exists:
        return
    bind.execute(
        sa.text(
            "INSERT INTO tenant_migration_reports "
            "(id,migration_revision,club_id,status,report,checksum,created_at) "
            "VALUES (:id,:revision,NULL,:status,:report,:checksum,:created_at)"
        ),
        {
            "id": str(uuid5(NAMESPACE_URL, "social-media-agent:migration:0017")),
            "revision": revision,
            "status": status,
            "report": serialized,
            "checksum": hashlib.sha256(serialized.encode()).hexdigest(),
            "created_at": datetime.now(timezone.utc),
        },
    )


def upgrade() -> None:
    bind = op.get_bind()
    existing = _names(bind)
    created: list[str] = []
    for name in TABLES:
        if name in existing:
            continue
        # Keep the canonical definition in the SQLAlchemy model.  This is the
        # same compatibility strategy already used by the historical 0001
        # migration, but unlike 0001 it is restricted to this fixed table list.
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)
        created.append(name)
    _report(bind, "created" if created else "fresh_schema_preexisting", created)


def downgrade() -> None:
    bind = op.get_bind()
    status = bind.execute(
        sa.text("SELECT status FROM tenant_migration_reports WHERE migration_revision=:revision"),
        {"revision": revision},
    ).scalar_one_or_none()
    if status == "fresh_schema_preexisting":
        return
    for name in reversed(TABLES):
        if name in _names(bind):
            op.drop_table(name)
    bind.execute(
        sa.text("DELETE FROM tenant_migration_reports WHERE migration_revision=:revision"),
        {"revision": revision},
    )
