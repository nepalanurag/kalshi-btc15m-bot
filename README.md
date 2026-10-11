# Kalshi BTC 15-Minute Trading Bot

A research bot for Kalshi 15-minute BTC markets (the KXBTC15M series). It
runs a fixed, rule-based RSI strategy in paper mode: nothing leaves the
machine, fills are simulated against the live order-book snapshot, and
every window is logged with cumulative PnL. I built it to test whether a
simple RSI rule can find an edge in these markets, with hard limits so it
cannot do anything I did not tell it to do. This is research, not a
money-maker: the honest results are below.

## What the backtest shows

21-day backtest (2026-09-14 to 2026-10-05; 2,016 windows evaluated, 1,793
traded), from `backtest/reports/backtest_report.json`:

| Metric | Value |
|---|---|
| Total PnL (fee-inclusive, modeled fills) | -$2,386.62 |
| Profit factor | 0.786 |
| Max drawdown | $2,768.26 |
| Fees paid | ~$715 |

Hit rates vs the implied probability the strategy paid:

| Regime | Windows | Hit rate | Avg implied prob |
|---|---|---|---|
| Bullish | 665 | 30.8% | 33.8% |
| Bearish | 617 | 27.4% | 34.1% |
| Neutral | 511 | 55.0% | 54.9% |

In bullish and bearish regimes the strategy wins less often than the price
implies. The threshold grid (72 combos around the defaults, in
`backtest/reports/threshold_report.json`) also loses everywhere: the best
setting (RSI period 21, thresholds 60/40) still loses $914.88 with profit
factor 0.885.

What this cannot show: real Kalshi profitability. Historical order books
and trade prices are unavailable, so entry prices are proxied by a
moneyness/volatility model, strikes come from a synthetic round-number
grid, and fills are assumed complete at the proxied price. Every PnL
number is conditional on the ASSUMPTIONS block at the top of
`backtest/backtester.py`. It shows the decision logic behaves consistently
over history, not an expected return.

## How it works

- **Signal.** Once per window it computes a 14-period RSI from 60-second
  Coinbase BTC-USD candles over a 120-minute lookback. RSI above 55 is
  bullish, below 45 bearish, anything in between neutral.
- **Strike and side.** Bullish windows take the open market whose strike is
  closest above the current spot and buy Yes; bearish windows take the
  closest strike below spot and buy No; neutral windows take the closest
  strike, Yes if spot is at or above it, No otherwise.
- **Execution.** Limit orders are placed inside the spread and walked up to
  a cap, within a per-window USD budget ($10 including fees), skipping
  entries above 60c or when the spread exceeds 10c. At most one filled buy
  per window, no selling and no early exit; positions are held to
  settlement.
- **Paper trading.** In paper mode no orders leave the machine. Fills are
  simulated deterministically against the live order-book snapshot, fees
  are estimated with Kalshi's published fee model, and every window,
  filled or not, is logged with cumulative PnL to a local SQLite database
  (`bot_state.sqlite`) and a JSONL log (`window_logs.jsonl`).
- **EV gate** (`backtest/ev_gate.py`). An optional expected-value check:
  only enter when the implied probability is below the regime's estimated
  win probability minus a margin. Ships default off, since enabling it is a
  deliberate choice.
- **Fill-model comparison** (`backtest/fill_models.py`). The paper
  simulator's optimistic fill next to a conservative model with queue
  position, partial fills, and adverse selection, so paper PnL is reported
  under both assumptions side by side.

The code has a live path (RSA-PSS auth against the Kalshi Trade API v2),
but this project is paper-trading research. The config defaults to
`mode: paper` against Kalshi's demo API.

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml   # edit series ticker, paper|live
python -m kalshi_btc15m_bot.run --config config.yaml
```

Run the backtest harness:

```bash
python -m backtest.backtester --days 21 --config config.example.yaml
python -m backtest.ev_gate
python -m backtest.threshold_sensitivity
python -m backtest.fill_models --demo
```

## Market-data lake

The `datalake/` package is the market-data infrastructure behind this
research: a scheduled capture loop records the Kalshi BTC-15m order-book
snapshot and the Coinbase 60s candle every minute (both public APIs, no
keys), validates each batch against versioned data contracts (Pandera
schemas, staleness and cross-feed alignment checks, quarantine on
failure), and stores validated hours as date/hour-partitioned Parquet with
a per-partition manifest, DVC-tracked. A dashboard generator, an LLM
red-team audit of the backtest harness, and adversarial scenario stress
tests round it out. Details and run instructions are in
`datalake/README.md`:

```bash
pip install -r datalake/requirements.txt   # pinned
python -m datalake.capture --iterations 5  # capture
python -m datalake.validate --all          # validate
python -m datalake.store --all             # store
python -m datalake.dashboard               # dashboard
python -m datalake.ai_audit --dry-run      # audit (checklist, no API)
pytest datalake/tests -q                   # tests
```

## Notes

- Market data comes from the Kalshi Trade API v2
  (`/trade-api/v2/markets` and order books); orders go through
  `/trade-api/v2/portfolio/orders`.
- Fees are estimated before each trade with Kalshi's published model:
  taker is `ceil(0.07 * C * P * (1-P) * 100) / 100`, maker is
  `ceil(0.0175 * C * P * (1-P) * 100) / 100`, with P in dollars.
- BTC spot and candles come from Coinbase public endpoints by default.

## Safety

The bot enforces a per-window USD budget including fees, one filled entry
per window, and no sells, hedges, or flips. If you run the live path, test
against the demo API first.
