# Kalshi BTC 15-Minute Trading Bot

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

## Notes

- Uses the Kalshi Trade API v2: market data from `/trade-api/v2/markets` and order books, orders through `/trade-api/v2/portfolio/orders`.
- Auth is RSA-PSS signatures with `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, and `KALSHI-ACCESS-TIMESTAMP` headers.
- Fees are estimated before each trade with Kalshi's published fee model. Taker is `ceil(0.07 * C * P * (1-P) * 100) / 100`, maker is `ceil(0.0175 * C * P * (1-P) * 100) / 100`, with P in dollars.
- BTC spot and candles come from Coinbase public endpoints by default.

## Safety

The bot enforces a per-window USD budget including fees, one filled entry per window, and no sells, hedges, or flips. If you run it live, test against the demo API first.
