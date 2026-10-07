"""Configuration for the market-data lake.

All settings come from environment variables prefixed with ``DATALAKE_``
(e.g. ``DATALAKE_CAPTURE_INTERVAL_SECONDS=30``); CLI flags override the
settings object per-command. ``datalake/config.py`` is the single place
where defaults live.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatalakeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DATALAKE_")

    # --- sources ---------------------------------------------------------
    kalshi_base_url: str = "https://api.elections.kalshi.com"
    kalshi_series_ticker: str = "KXBTC15M"
    coinbase_product_id: str = "BTC-USD"

    # --- capture ---------------------------------------------------------
    capture_interval_seconds: int = 60
    capture_iterations: int = 0  # 0 = run forever
    request_timeout_seconds: float = 15.0
    max_retries: int = 5
    backoff_base_seconds: float = 2.0

    # --- layout ----------------------------------------------------------
    buffer_dir: str = "datalake/buffer"
    validation_dir: str = "datalake/validation"
    lake_dir: str = "datalake/lake"
    scenarios_dir: str = "datalake/scenarios"
    dashboard_dir: str = "datalake/dashboard"

    # --- validation ------------------------------------------------------
    staleness_threshold_seconds: int = 180
    # Max |snapshot_time - nearest_candle_start|. Covers the 60s capture
    # cadence (a snapshot can land up to ~120s after the latest closed
    # candle) plus one minute of upstream lag.
    alignment_tolerance_seconds: int = 150

    # --- logging ---------------------------------------------------------
    log_format: str = "json"  # "json" | "console"
    log_level: str = "INFO"

    @field_validator("capture_interval_seconds")
    @classmethod
    def _interval_sane(cls, v: int) -> int:
        if v < 10:
            raise ValueError("capture_interval_seconds must be >= 10 (API politeness)")
        return v

    @field_validator("staleness_threshold_seconds", "alignment_tolerance_seconds")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("thresholds must be positive")
        return v

    @field_validator("log_format")
    @classmethod
    def _log_format_known(cls, v: str) -> str:
        if v not in ("json", "console"):
            raise ValueError("log_format must be 'json' or 'console'")
        return v


def get_settings() -> DatalakeSettings:
    return DatalakeSettings()
