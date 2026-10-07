"""Market-data lake for the kalshi-btc15m-bot trading research.

Captures Kalshi BTC 15-minute order-book snapshots and Coinbase 60-second
candles on a schedule, validates them against versioned data contracts,
stores them as date/hour-partitioned Parquet, and serves monitoring and
research artifacts (dashboard, AI audit, adversarial stress scenarios).

Data contract version: see ``DATA_CONTRACT_VERSION`` below and
``datalake/schemas.py``.
"""

DATA_CONTRACT_VERSION = "1.0"

__all__ = ["DATA_CONTRACT_VERSION"]
