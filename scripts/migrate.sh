#!/bin/sh
set -eu
# PostgreSQL advisory lock prevents concurrent schema migrations across deployments.
python - <<'PY'
from sqlalchemy import create_engine, text
from alembic import command
from alembic.config import Config
from app.config import get_settings
engine=create_engine(get_settings().database_url,pool_pre_ping=True)
with engine.connect() as connection:
    if connection.dialect.name == "postgresql":
        connection.execute(text("SELECT pg_advisory_lock(724619381)"))
        connection.commit()
    try:
        cfg=Config("alembic.ini")
        cfg.attributes["connection"]=connection
        command.upgrade(cfg,"head")
    finally:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_unlock(724619381)"))
            connection.commit()
PY
