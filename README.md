# Kalshi BTC 15-Minute Trading Bot

## What it does

The bot trades Kalshi 15-minute BTC markets (for example the KXBTC15M series)
with a fixed, rule-based strategy. It ships in paper mode: nothing is sent to
Kalshi unless you switch `mode` to `live` and add API credentials.

- Signal. Once per window it computes a 14-period RSI from 60-second Coinbase
  BTC-USD candles over a 120-minute lookback. RSI above 55 is bullish, below
  45 bearish, anything in between neutral.
- Range estimate. A 14-period ATR proxy over the same candles gives an
  expected price range for the window, scaled by the square root of the
  number of candles in the window. It is logged with each decision; the
  strike choice itself is by spot price.
- Strike and side. Bullish windows pick the open market whose strike is
  closest above the current spot and buy Yes; bearish windows pick the closest
  strike below spot and buy No; neutral windows pick the closest strike and
  take Yes when the spot is at or above it, No otherwise (the configured side
  only applies when no strike parses from the market).
- Execution. Limit orders are placed inside the spread and walked up to a cap,
  within a per-window USD budget that includes fees. At most one filled buy
  per window, no selling and no early exit; positions are held to settlement.
- Paper trading. In paper mode no orders leave the machine. Fills are
  simulated deterministically against the live order-book snapshot, fees are
  estimated with Kalshi's published fee model, and every window, filled or not,
  is logged with cumulative PnL to a local SQLite database and a JSONL log.

To try it for real, switch `mode` to `live`, point the config at the Kalshi
API, and supply your API credentials. For safe testing there is also Kalshi's
demo API (the default base URL in `config.example.yaml`).

A bot that trades Kalshi BTC 15-minute markets. I built it to run a fixed strategy with hard limits, so it cannot do anything I did not tell it to do.

How it works:

- It only trades one Kalshi series ticker (for example `KXBTC15M`).
- Each 15-minute window starts before close. At the start of the window it computes the signal once and locks it: BTC spot price, RSI, a volatility based range estimate, and which strike and side to take.
- It enters with limit orders inside the spread, canceling and replacing as needed.
- At most one filled buy per window. No flipping, no early exit. It holds to settlement.
- Every window is logged, filled or not, and cumulative PnL is tracked across runs.

## Quickstart

Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create your config:

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` and set your series ticker, `paper` or `live` mode, the Kalshi API base URL, and your API key id and private key path (needed for live trading, not for paper).

Run it:

```bash
python -m kalshi_btc15m_bot.run --config config.yaml
```

## Outputs

- SQLite database (`bot_state.sqlite`): windows, positions, cumulative PnL.
- JSONL window log (`window_logs.jsonl`).

## Validation

The `backtest/` package is the evaluation layer for this bot: a historical
backtester, an expected-value gate, a threshold sensitivity report, and a
fill-model comparison. The trading logic itself is untouched; the harness
replays history through the bot's own decision functions and measures what
comes out.

- **Backtester** (`python -m backtest.backtester --days 21 --config config.example.yaml`).
  Replays recorded Coinbase 60-second candles over 15-minute windows,
  settles each window against the actual BTC move, and reports hit rate vs
  implied probability by regime, profit factor, max drawdown, and
  fee-inclusive PnL. **Read the ASSUMPTIONS block at the top of
  `backtest/backtester.py` first**: historical Kalshi order books and trade
  prices are unavailable, so entry prices are proxied by a
  moneyness/volatility model, strikes come from a synthetic round-number
  grid, and fills are assumed complete at the proxied price. A 21-day run
  on the config defaults is in `backtest/reports/backtest_report.json`.
- **EV gate** (`python -m backtest.ev_gate`). The expected-value check the
  live bot can enable: only enter when
  `implied_prob < estimated_win_prob - margin`, with the win rate measured
  per regime from backtest reports or paper window logs. It ships default
  off (pass-through), so enabling it is a deliberate choice, documented in
  `backtest/ev_gate.py`.
- **Threshold sensitivity** (`python -m backtest.threshold_sensitivity`).
  Grid report over RSI thresholds and periods. The 55/45 thresholds are
  starting points under evaluation; the report shows the surface around
  them (`backtest/reports/threshold_report.json`).
- **Fill models** (`python -m backtest.fill_models --demo`, or `--jsonl
  window_logs.jsonl`). The paper simulator's optimistic fill next to a
  conservative model with queue position, partial fills, and adverse
  selection, reporting paper PnL under both assumptions side by side.

What the backtest can and cannot show: it can show whether the decision
logic behaves consistently over history, how realized hit rates compare to
the implied probabilities the strategy pays, and how sensitive results are
to the RSI thresholds. It cannot show real Kalshi profitability. Entry
prices, spreads, queue position, and fills are modeled, not measured, so
every PnL number is conditional on the stated assumptions -- a starting
point for risk decisions, not an expected return.

## Notes

- Uses the Kalshi Trade API v2: market data from `/trade-api/v2/markets` and order books, orders through `/trade-api/v2/portfolio/orders`.
- Auth is RSA-PSS signatures with `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, and `KALSHI-ACCESS-TIMESTAMP` headers.
- Fees are estimated before each trade with Kalshi's published fee model. Taker is `ceil(0.07 * C * P * (1-P) * 100) / 100`, maker is `ceil(0.0175 * C * P * (1-P) * 100) / 100`, with P in dollars.
- BTC spot and candles come from Coinbase public endpoints by default.

## Safety

The bot enforces a per-window USD budget including fees, one filled entry per window, and no sells, hedges, or flips. If you run it live, test against the demo API first.
