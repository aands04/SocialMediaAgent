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
    upload_root: Path = Path("data/uploads")
    openai_model: str = "gpt-5-mini"
    openai_image_model: str = "gpt-image-2"
    openai_image_tool_model: str = "gpt-5.4-mini"
    openai_image_quality: str = "medium"
    openai_api_key: str | None = None
    meta_graph_version: str = "v23.0"
    meta_access_token: str | None = None
    meta_test_enabled: bool = False
    meta_test_publish_enabled: bool = False
    meta_production_enabled: bool = False
    meta_scheduler_enabled: bool = False
    meta_automatic_publish_enabled: bool = False
    meta_connection_max_age_seconds: int = 86400
    meta_connection_check_interval_seconds: int = 43200
    meta_container_poll_interval_seconds: int = 30
    meta_container_max_wait_seconds: int = 900
    meta_scheduler_batch_size: int = 5
    meta_app_id: str | None = None
    meta_app_secret: str | None = None
    meta_facebook_app_id: str | None = None
    meta_facebook_app_secret: str | None = None
    meta_facebook_oauth_redirect_uri: str | None = None
    meta_whatsapp_configuration_id: str | None = None
    meta_webhook_verify_token: str | None = None
    # Facebook und WhatsApp sind reguläre Plattformfunktionen. Die Schalter
    # bleiben ausschließlich als betriebliche, plattformweite Pause erhalten.
    facebook_channel_enabled: bool = True
    whatsapp_channel_enabled: bool = True
    live_center_enabled: bool = True
    live_event_ai_parsing_enabled: bool = False
    live_event_ai_model: str = "gpt-5-mini"
    live_event_reporter_rate_limit_per_minute: int = 12
    live_event_game_window_before_minutes: int = 180
    live_event_game_window_after_minutes: int = 420
    live_event_active_game_ttl_minutes: int = 480
    meta_token_encryption_key: str | None = None
    meta_token_key_version: str = "v1"
    meta_oauth_redirect_uri: str | None = None
    meta_public_base_url: str | None = None
    meta_oauth_state_ttl_seconds: int = 600
    meta_media_grant_ttl_seconds: int = 3600
    meta_confirmation_ttl_seconds: int = 300
    meta_http_timeout_seconds: float = 20.0
    publisher_mode: str = "dry-run"
    global_publish_enabled: bool = False
    timezone: str = "Europe/Berlin"
    session_max_age: int = 3600
    app_public_base_url: str | None = None
    password_reset_enabled: bool = False
    password_reset_token_ttl_seconds: int = 1800
    password_reset_request_cooldown_seconds: int = 60
    email_change_token_ttl_seconds: int = 1800
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_use_ssl: bool = False
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_email: str | None = None
    smtp_from_name: str = "Vereinszentrale"
    smtp_timeout_seconds: float = 10.0
    max_publish_attempts: int = 5
    fussball_live_test_enabled: bool = False
    fussball_automatic_sync_enabled: bool = False
    automatic_post_generation_enabled: bool = False
    fussball_sync_interval_seconds: int = 1800
    fussball_result_poll_interval_seconds: int = 300
    fussball_sync_error_backoff_seconds: int = 300
    fussball_sync_batch_size: int = 2
    fussball_sync_lease_seconds: int = 1800
    fussball_result_min_age_minutes: int = 120
    fussball_result_stability_seconds: int = 600
    fussball_result_max_age_hours: int = 48
    fussball_decode_obfuscated_results: bool = True
    provider_snapshot_root: Path = Path("data/provider-snapshots")
    log_root: Path = Path("data/logs")
    backup_root: Path = Path("data/backups")
    text_generator_mode: str = "mock"
    image_generator_mode: str = "playwright"
    multi_tenant_enabled: bool = False
    self_registration_enabled: bool = False
    billing_enabled: bool = False
    platform_timezone: str = "UTC"
    object_storage_provider: str = "local"
    s3_endpoint_url: str | None = None
    s3_region: str = "auto"
    s3_bucket: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_presign_ttl_seconds: int = 900
    publishing_object_ttl_seconds: int = 7200
    initial_club_id: str | None = None
    initial_club_name: str | None = None
    initial_club_short_name: str | None = None
    initial_club_slug: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
