import pytest

from app.config import Settings
from app.worker import _automatic_scheduler_enabled, _validate_worker_environment


def _settings(**overrides) -> Settings:
    values = {
        "environment": "staging",
        "publisher_mode": "dry-run",
        "global_publish_enabled": False,
        "meta_scheduler_enabled": False,
        "meta_automatic_publish_enabled": False,
        "meta_production_enabled": False,
        "meta_test_enabled": False,
        "meta_access_token": None,
    }
    values.update(overrides)
    return Settings(**values)


def test_staging_remains_hard_dry_run():
    settings = _settings()

    assert _validate_worker_environment(settings) == "dry-run"
    assert _automatic_scheduler_enabled(settings) is False


def test_meta_test_forbids_automatic_scheduler():
    settings = _settings(
        environment="meta-test",
        publisher_mode="instagram",
        meta_test_enabled=True,
        meta_scheduler_enabled=True,
        meta_automatic_publish_enabled=True,
    )

    with pytest.raises(RuntimeError, match="Meta-Test verboten"):
        _validate_worker_environment(settings)


def test_production_can_start_paused():
    settings = _settings(
        environment="production",
        publisher_mode="instagram",
        meta_production_enabled=True,
    )

    assert _validate_worker_environment(settings) == "production-paused"
    assert _automatic_scheduler_enabled(settings) is False


def test_production_rejects_partially_enabled_gates():
    settings = _settings(
        environment="production",
        publisher_mode="instagram",
        meta_production_enabled=True,
        global_publish_enabled=True,
    )

    with pytest.raises(RuntimeError, match="allen drei Gates"):
        _validate_worker_environment(settings)


def test_production_enables_scheduler_only_with_every_gate():
    settings = _settings(
        environment="production",
        publisher_mode="instagram",
        meta_production_enabled=True,
        global_publish_enabled=True,
        meta_scheduler_enabled=True,
        meta_automatic_publish_enabled=True,
    )

    assert _validate_worker_environment(settings) == "automatic-instagram"
    assert _automatic_scheduler_enabled(settings) is True


@pytest.mark.parametrize(
    ("unsafe_setting", "value"),
    [
        ("meta_test_publish_enabled", True),
        ("meta_access_token", "legacy-global-token"),
    ],
)
def test_production_rejects_meta_test_or_legacy_credentials(unsafe_setting, value):
    settings = _settings(
        environment="production",
        publisher_mode="instagram",
        meta_production_enabled=True,
        global_publish_enabled=True,
        meta_scheduler_enabled=True,
        meta_automatic_publish_enabled=True,
        **{unsafe_setting: value},
    )

    with pytest.raises(RuntimeError):
        _validate_worker_environment(settings)
    assert _automatic_scheduler_enabled(settings) is False
