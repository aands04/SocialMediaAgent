from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext


def _load_migration(filename: str, module_name: str):
    path = Path("alembic/versions") / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_games_tenant_identity_migration_is_reversible() -> None:
    migration = _load_migration("0025a_games_tenant_identity.py", "games_tenant_identity_0025a")
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE games (id VARCHAR(36) PRIMARY KEY, club_id VARCHAR(36) NOT NULL)"
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        constraints = sa.inspect(connection).get_unique_constraints("games")
        assert any(
            tuple(constraint.get("column_names") or ()) == ("id", "club_id")
            for constraint in constraints
        )

        with Operations.context(context):
            migration.downgrade()

        constraints = sa.inspect(connection).get_unique_constraints("games")
        assert not any(
            tuple(constraint.get("column_names") or ()) == ("id", "club_id")
            for constraint in constraints
        )


def test_live_center_migration_depends_on_game_tenant_identity() -> None:
    migration = _load_migration("0026_live_center.py", "live_center_0026")

    assert migration.down_revision == "0025a"


def test_prompt_dispatch_image_references_migration_is_reversible() -> None:
    migration = _load_migration(
        "0031_prompt_dispatch_image_references.py",
        "prompt_dispatch_image_references_0031",
    )
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE ai_prompt_dispatches (id VARCHAR(36) PRIMARY KEY)"
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        columns = {
            item["name"]: item
            for item in sa.inspect(connection).get_columns("ai_prompt_dispatches")
        }
        assert "reference_images" in columns
        assert columns["reference_images"]["nullable"] is False

        connection.exec_driver_sql(
            "INSERT INTO ai_prompt_dispatches (id) VALUES ('legacy-dispatch')"
        )
        stored = connection.exec_driver_sql(
            "SELECT reference_images FROM ai_prompt_dispatches WHERE id = 'legacy-dispatch'"
        ).scalar_one()
        assert stored == "[]"

        with Operations.context(context):
            migration.downgrade()

        column_names = {
            item["name"]
            for item in sa.inspect(connection).get_columns("ai_prompt_dispatches")
        }
        assert "reference_images" not in column_names
