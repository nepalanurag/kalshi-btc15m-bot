"""Validation harness for the kalshi-btc15m-bot.

The modules here are the evaluation layer a production trading system needs:
historical backtesting, an expected-value gate, parameter sensitivity
reporting, and fill-model comparison. The live/paper bot logic is untouched;
this package only *reads* the bot's decision functions and logs.

Modules:
    data                  - Coinbase candle fetch + local cache.
    backtester            - Historical replay over recorded candles.
    ev_gate               - Expected-value pre-trade check (default off).
    threshold_sensitivity - Grid report over RSI thresholds and periods.
    fill_models           - Optimistic vs conservative paper-fill models.
"""
