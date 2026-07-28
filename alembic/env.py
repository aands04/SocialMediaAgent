from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app import models  # noqa: F401 -- registers canonical metadata
from app.config import get_settings
from app.db import Base

config=context.config
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
if config.config_file_name: fileConfig(config.config_file_name)
target_metadata=Base.metadata

def offline():
    context.configure(url=config.get_main_option("sqlalchemy.url"),target_metadata=target_metadata,literal_binds=True)
    with context.begin_transaction(): context.run_migrations()

def online():
    supplied=config.attributes.get("connection")
    if supplied is not None:
        context.configure(connection=supplied,target_metadata=target_metadata)
        with context.begin_transaction(): context.run_migrations()
        return
    with engine_from_config(config.get_section(config.config_ini_section),prefix="sqlalchemy.",poolclass=pool.NullPool).connect() as connection:
        context.configure(connection=connection,target_metadata=target_metadata)
        with context.begin_transaction(): context.run_migrations()

offline() if context.is_offline_mode() else online()
