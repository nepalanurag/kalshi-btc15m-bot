from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Literal, Optional

import yaml


Mode = Literal["paper", "live"]


@dataclass(frozen=True)
class KalshiConfig:
    base_url: str
    api_key_id: Optional[str]
    private_key_pem_path: Optional[str]
    subaccount: int = 0


@dataclass(frozen=True)
class StorageConfig:
    sqlite_path: str
    window_log_jsonl: str


@dataclass(frozen=True)
class StrategyConfig:
    rsi_period: int
    bullish_rsi: float
    bearish_rsi: float
    neutral_fallback_side: Literal["yes", "no"]

    atr_period: int
    candle_granularity_seconds: int
    history_lookback_minutes: int


@dataclass(frozen=True)
class ExecutionConfig:
    window_minutes: int
    min_seconds_to_close: int
    entry_max_cents: int
    spread_max_cents: int
    usd_budget: float

    tick_size_cents: int
    attempt_sleep_seconds: float
    wait_for_fill_seconds: float
    walk_step_cents: int
    max_walk_cents: int
    order_expiration_buffer_seconds: int


@dataclass(frozen=True)
class PriceFeedConfig:
    provider: str
    product_id: str


@dataclass(frozen=True)
class BotConfig:
    mode: Mode
    series_ticker: str
    storage: StorageConfig
    kalshi: KalshiConfig
    strategy: StrategyConfig
    execution: ExecutionConfig
    price_feed: PriceFeedConfig


def load_config(path: str) -> BotConfig:
    data: Dict[str, Any] = yaml.safe_load(Path(path).read_text())

    mode: Mode = data["mode"]
    series_ticker: str = data["series_ticker"]

    storage = data["storage"]
    kalshi = data["kalshi"]
    strat = data["strategy"]
    exe = data["execution"]
    pf = data["price_feed"]

    return BotConfig(
        mode=mode,
        series_ticker=series_ticker,
        storage=StorageConfig(
            sqlite_path=str(storage["sqlite_path"]),
            window_log_jsonl=str(storage["window_log_jsonl"]),
        ),
        kalshi=KalshiConfig(
            base_url=str(kalshi["base_url"]).rstrip("/"),
            api_key_id=kalshi.get("api_key_id"),
            private_key_pem_path=kalshi.get("private_key_pem_path"),
            subaccount=int(kalshi.get("subaccount", 0)),
        ),
        strategy=StrategyConfig(
            rsi_period=int(strat["rsi_period"]),
            bullish_rsi=float(strat["bullish_rsi"]),
            bearish_rsi=float(strat["bearish_rsi"]),
            neutral_fallback_side=str(strat["neutral_fallback_side"]).lower(),
            atr_period=int(strat["atr_period"]),
            candle_granularity_seconds=int(strat["candle_granularity_seconds"]),
            history_lookback_minutes=int(strat["history_lookback_minutes"]),
        ),
        execution=ExecutionConfig(
            window_minutes=int(exe["window_minutes"]),
            min_seconds_to_close=int(exe["min_seconds_to_close"]),
            entry_max_cents=int(exe["entry_max_cents"]),
            spread_max_cents=int(exe["spread_max_cents"]),
            usd_budget=float(exe["usd_budget"]),
            tick_size_cents=int(exe["tick_size_cents"]),
            attempt_sleep_seconds=float(exe["attempt_sleep_seconds"]),
            wait_for_fill_seconds=float(exe["wait_for_fill_seconds"]),
            walk_step_cents=int(exe["walk_step_cents"]),
            max_walk_cents=int(exe["max_walk_cents"]),
            order_expiration_buffer_seconds=int(exe.get("order_expiration_buffer_seconds", 3)),
        ),
        price_feed=PriceFeedConfig(
            provider=str(pf.get("provider", "coinbase")),
            product_id=str(pf.get("product_id", "BTC-USD")),
        ),
    )
