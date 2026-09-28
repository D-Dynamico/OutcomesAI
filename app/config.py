# All tunables, read from the environment. Defaults are the values in docs/design.md.
# Durations are floats so tests can compress an outage into seconds without changing logic
# (design section 7, "Time is controllable").
import os
from dataclasses import dataclass, fields
from typing import Mapping


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    database_url: str = "postgresql://outcomes:outcomes@db:5432/outcomes"
    db_pool_size: int = 10

    # Design section 5: lease > AI timeout + margin
    lease_seconds: float = 60
    ai_timeout_seconds: float = 30
    retry_budget: int = 5
    backoff_base_seconds: float = 10

    # Design section 5: circuit breaker
    breaker_window: int = 20
    breaker_threshold: int = 11
    breaker_lookback_minutes: float = 10
    breaker_cooldown_seconds: float = 30
    probe_deadline_seconds: float = 60

    # Design section 5: SLA
    sla_seconds: float = 10

    # Design section 3: body size limit
    max_body_bytes: int = 1_048_576

    # Worker process shape (DECISIONS.md D9)
    worker_poll_seconds: float = 0.5
    worker_concurrency: int = 4

    mock_ai_url: str = "http://mock-ai:8001"

    # Test-only hooks stay inert unless this is set (app/hooks.py)
    test_hooks: bool = False
    crash_at: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        values = {}
        for f in fields(cls):
            raw = env.get(f.name.upper())
            if raw is None:
                continue
            try:
                if f.type is bool:
                    values[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
                elif f.type is int:
                    values[f.name] = int(raw)
                elif f.type is float:
                    values[f.name] = float(raw)
                else:
                    values[f.name] = raw
            except ValueError as e:
                raise ConfigError(f"{f.name.upper()} is not a valid {f.type.__name__}") from e
        config = cls(**values)
        config.validate()
        return config

    def validate(self) -> None:
        for name in ("lease_seconds", "ai_timeout_seconds", "backoff_base_seconds",
                     "breaker_lookback_minutes", "breaker_cooldown_seconds",
                     "probe_deadline_seconds", "sla_seconds", "worker_poll_seconds"):
            if getattr(self, name) <= 0:
                raise ConfigError(f"{name.upper()} must be positive")
        for name in ("retry_budget", "breaker_window", "breaker_threshold",
                     "max_body_bytes", "worker_concurrency", "db_pool_size"):
            if getattr(self, name) < 1:
                raise ConfigError(f"{name.upper()} must be at least 1")
        # Design section 5: a lease shorter than the AI timeout gets live workers reclaimed
        # and pays for the same version twice. Same reasoning for the probe deadline.
        if self.lease_seconds <= self.ai_timeout_seconds:
            raise ConfigError("LEASE_SECONDS must be greater than AI_TIMEOUT_SECONDS")
        if self.probe_deadline_seconds <= self.ai_timeout_seconds:
            raise ConfigError("PROBE_DEADLINE_SECONDS must be greater than AI_TIMEOUT_SECONDS")
        if self.breaker_threshold > self.breaker_window:
            raise ConfigError("BREAKER_THRESHOLD cannot exceed BREAKER_WINDOW")

    def validate_worker(self) -> None:
        # DECISIONS.md D9: one connection per worker loop, plus one for the process itself
        if self.db_pool_size < self.worker_concurrency + 1:
            raise ConfigError("DB_POOL_SIZE must be at least WORKER_CONCURRENCY + 1")
