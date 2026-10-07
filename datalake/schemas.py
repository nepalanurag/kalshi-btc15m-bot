"""Versioned data contracts for the market-data lake.

``DATA_CONTRACT_VERSION`` is stamped into every partition manifest and every
scenario file. Bump it (and note the change in the README changelog) whenever
a field is added, removed, or redefined.

Two record types flow through the lake:

* ``snapshot`` -- one Kalshi order-book snapshot for the active BTC 15m market.
* ``candle``   -- one closed Coinbase 60-second candle.

Column-level invariants live in the pandera schemas below. Cross-column
invariants (``yes_bid <= yes_ask``, ``high >= low``) are enforced by
``validate.run_cross_checks`` so the failure messages name the rule.
"""

from __future__ import annotations

from pandera.pandas import Check, Column, DataFrameSchema

from datalake import DATA_CONTRACT_VERSION

__all__ = [
    "DATA_CONTRACT_VERSION",
    "SNAPSHOT_SCHEMA",
    "CANDLE_SCHEMA",
    "SNAPSHOT_COLUMNS",
    "CANDLE_COLUMNS",
]

_TICKER_RE = r"^KXBTC15M-"

SNAPSHOT_COLUMNS = [
    "record_type",
    "captured_at",
    "feed_ts",
    "market_ticker",
    "strike",
    "status",
    "yes_bid_cents",
    "yes_ask_cents",
    "no_bid_cents",
    "no_ask_cents",
    "spread_cents",
    "n_levels_yes",
    "n_levels_no",
    "ladder_yes_json",
    "ladder_no_json",
]

SNAPSHOT_SCHEMA = DataFrameSchema(
    {
        "record_type": Column(str, Check.equal_to("snapshot"), nullable=False),
        "captured_at": Column("datetime64[ns, UTC]", coerce=True, nullable=False),
        "feed_ts": Column("datetime64[ns, UTC]", coerce=True, nullable=False),
        "market_ticker": Column(str, Check.str_matches(_TICKER_RE), nullable=False),
        "strike": Column(float, Check.gt(0), coerce=True, nullable=False),
        "status": Column(str, Check.isin(["active", "open"]), nullable=False),
        "yes_bid_cents": Column(float, Check.in_range(0, 100), coerce=True, nullable=True),
        "yes_ask_cents": Column(float, Check.in_range(0, 100), coerce=True, nullable=True),
        "no_bid_cents": Column(float, Check.in_range(0, 100), coerce=True, nullable=True),
        "no_ask_cents": Column(float, Check.in_range(0, 100), coerce=True, nullable=True),
        "spread_cents": Column(float, Check.ge(0), coerce=True, nullable=True),
        "n_levels_yes": Column(int, Check.ge(0), coerce=True, nullable=False),
        "n_levels_no": Column(int, Check.ge(0), coerce=True, nullable=False),
        "ladder_yes_json": Column(str, nullable=False),
        "ladder_no_json": Column(str, nullable=False),
    },
    strict=False,
    name=f"kalshi_snapshot_v{DATA_CONTRACT_VERSION}",
)

CANDLE_COLUMNS = [
    "record_type",
    "captured_at",
    "feed_ts",
    "candle_start",
    "open",
    "high",
    "low",
    "close",
    "volume",
]

CANDLE_SCHEMA = DataFrameSchema(
    {
        "record_type": Column(str, Check.equal_to("candle"), nullable=False),
        "captured_at": Column("datetime64[ns, UTC]", coerce=True, nullable=False),
        "feed_ts": Column("datetime64[ns, UTC]", coerce=True, nullable=False),
        "candle_start": Column("datetime64[ns, UTC]", coerce=True, nullable=False),
        "open": Column(float, Check.gt(0), coerce=True, nullable=False),
        "high": Column(float, Check.gt(0), coerce=True, nullable=False),
        "low": Column(float, Check.gt(0), coerce=True, nullable=False),
        "close": Column(float, Check.gt(0), coerce=True, nullable=False),
        "volume": Column(float, Check.ge(0), coerce=True, nullable=False),
    },
    strict=False,
    name=f"coinbase_candle_v{DATA_CONTRACT_VERSION}",
)
