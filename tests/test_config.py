import pytest

from app.config import Config, ConfigError


def test_defaults_match_design():
    c = Config.from_env({})
    assert c.lease_seconds == 60
    assert c.ai_timeout_seconds == 30
    assert c.retry_budget == 5
    assert c.backoff_base_seconds == 10
    assert c.breaker_window == 20
    assert c.breaker_threshold == 11
    assert c.breaker_lookback_minutes == 10
    assert c.breaker_cooldown_seconds == 30
    assert c.probe_deadline_seconds == 60
    assert c.sla_seconds == 10
    assert c.max_body_bytes == 1_048_576
    assert c.worker_poll_seconds == 0.5
    assert c.worker_concurrency == 4
    assert c.test_hooks is False


def test_env_overrides_and_types():
    c = Config.from_env({
        "LEASE_SECONDS": "1.5", "AI_TIMEOUT_SECONDS": "0.5", "PROBE_DEADLINE_SECONDS": "1.5",
        "RETRY_BUDGET": "3", "TEST_HOOKS": "true", "DATABASE_URL": "postgresql://x/y",
    })
    assert c.lease_seconds == 1.5
    assert c.ai_timeout_seconds == 0.5
    assert c.retry_budget == 3
    assert c.test_hooks is True
    assert c.database_url == "postgresql://x/y"


def test_invalid_number_is_rejected():
    with pytest.raises(ConfigError):
        Config.from_env({"RETRY_BUDGET": "five"})


@pytest.mark.parametrize("env", [
    {"LEASE_SECONDS": "30"},                   # lease must exceed the AI timeout (section 5)
    {"PROBE_DEADLINE_SECONDS": "30"},
    {"BREAKER_THRESHOLD": "21"},
    {"SLA_SECONDS": "0"},
    {"WORKER_CONCURRENCY": "0"},
])
def test_unsafe_settings_are_rejected(env):
    with pytest.raises(ConfigError):
        Config.from_env(env)


def test_worker_pool_must_cover_concurrency():
    Config.from_env({"WORKER_CONCURRENCY": "4", "DB_POOL_SIZE": "5"}).validate_worker()
    with pytest.raises(ConfigError):
        Config.from_env({"WORKER_CONCURRENCY": "4", "DB_POOL_SIZE": "4"}).validate_worker()
