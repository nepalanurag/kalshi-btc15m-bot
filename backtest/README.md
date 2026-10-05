# Validation harness

Evaluation layer for the kalshi-btc15m-bot. The bot's trading logic is
untouched; these modules replay history, measure edge, and stress the
assumptions behind paper fills.

## Modules

- `backtester.py` -- historical replay over recorded Coinbase 60s candles.
  Reads the bot's own decision functions (`strategy`, `indicators`),
  settles each 15-minute window against the actual BTC move, and reports
  hit rate vs implied probability by regime, profit factor, max drawdown,
  and fee-inclusive PnL. **Read the ASSUMPTIONS block at the top of the
  file before trusting any number.**
- `ev_gate.py` -- the expected-value gate the live bot can enable:
  `implied_prob < estimated_win_prob - margin`, measured per regime from
  backtest reports or paper window logs. Default off (pass-through), so
  wiring it in changes nothing until explicitly enabled.
- `threshold_sensitivity.py` -- grid report over RSI thresholds and
  periods. The 55/45 thresholds are treated as starting points under
  evaluation; the report shows the surface around them.
- `fill_models.py` -- optimistic fill (reference re-implementation of the
  paper simulator) vs conservative fill (queue position, partial fills,
  adverse selection). Reports paper PnL under both assumptions side by
  side.
- `data.py` -- Coinbase candle fetch + local CSV cache (`backtest/data/`,
  gitignored).

## Quickstart

```bash
pip install -r requirements.txt   # requests, pyyaml

# Backtest the last 21 days (config thresholds) and write the report
python -m backtest.backtester --days 21 --config config.example.yaml

# What the EV gate would do on those results (illustration; gate is off by default)
python -m backtest.ev_gate

# Sensitivity surface around the 55/45 starting points
python -m backtest.threshold_sensitivity --days 21 --config config.example.yaml

# Fill-model comparison (mechanism illustration on synthetic books)
python -m backtest.fill_models --demo

# ...or on real paper entries from a window log (depth synthesized from top-of-book)
python -m backtest.fill_models --jsonl window_logs.jsonl
```

Reports land in `backtest/reports/` as JSON.

## What the backtest can and cannot show

It can show whether the decision logic behaves consistently over history,
how realized hit rates compare to the implied probabilities the strategy
pays (calibration by regime), and how sensitive results are to the RSI
thresholds. It cannot show real Kalshi profitability: historical Kalshi
order books and trade prices are unavailable, so entry prices, spreads,
queue position, and fills are modeled, not measured. Every PnL number is
conditional on the assumptions documented at the top of `backtester.py`.
