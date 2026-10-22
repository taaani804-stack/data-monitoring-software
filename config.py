"""Scanner (not strategy) settings, overridable through environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Mapping, Tuple

from models import ConfigError

DEFAULT_MAX_AGE_MINUTES = {"15m": 30, "1h": 420}


def _int_env(env: Mapping[str, str], name: str, default: int, minimum: int = 0) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"Environment variable {name} must be an integer.")
    if value < minimum:
        raise ConfigError(f"Environment variable {name} must be >= {minimum}.")
    return value


@dataclass(frozen=True)
class ScannerConfig:
    top_n: int = 150
    timeframes: Tuple[str, ...] = ("1h",)
    candle_limit: int = 500  # MEXC default/max-safe page size
    close_buffer_seconds: int = 5  # safety margin after a candle's end before it counts as closed
    state_path: str = "state/sent_signals.json"
    retention_days: int = 14
    request_delay_seconds: float = 0.1  # politeness delay between kline requests
    max_age_minutes: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_MAX_AGE_MINUTES))

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "ScannerConfig":
        max_age = dict(DEFAULT_MAX_AGE_MINUTES)
        for tf in max_age:
            max_age[tf] = _int_env(env, f"MAX_SIGNAL_AGE_MINUTES_{tf.upper()}", max_age[tf], 1)
        return cls(
            top_n=_int_env(env, "TOP_N_SYMBOLS", 150, 1),
            candle_limit=min(_int_env(env, "CANDLE_LIMIT", 500, 100), 1000),
            close_buffer_seconds=_int_env(env, "CANDLE_CLOSE_BUFFER_SECONDS", 5, 0),
            state_path=env.get("STATE_PATH", "state/sent_signals.json") or "state/sent_signals.json",
            retention_days=_int_env(env, "STATE_RETENTION_DAYS", 14, 1),
            max_age_minutes=max_age,
        )
