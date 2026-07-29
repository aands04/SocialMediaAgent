from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    environment: str = "development"
    database_url: str = "sqlite:///./data/app.db"
    session_secret: str = "development-only-change-me"
    media_storage_type: str = "local"
    media_root: Path = Path("data/external-media")
    generated_root: Path = Path("data/generated")
    openai_model: str = "gpt-5-mini"
    openai_image_model: str = "gpt-image-2"
    openai_image_quality: str = "medium"
    openai_api_key: str | None = None
    meta_graph_version: str = "v23.0"
    meta_access_token: str | None = None
    publisher_mode: str = "dry-run"
    global_publish_enabled: bool = False
    timezone: str = "Europe/Berlin"
    session_max_age: int = 3600
    max_publish_attempts: int = 5
    fussball_live_test_enabled: bool = False
    provider_snapshot_root: Path = Path("data/provider-snapshots")
    log_root: Path = Path("data/logs")
    backup_root: Path = Path("data/backups")
    text_generator_mode: str = "mock"
    image_generator_mode: str = "playwright"

@lru_cache
def get_settings() -> Settings:
    return Settings()
