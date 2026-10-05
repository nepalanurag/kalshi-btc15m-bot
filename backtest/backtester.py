"""Historical backtester for the kalshi-btc15m-bot (validation harness).

Replays recorded Coinbase 60-second candles through the bot's own decision
code (``strategy.rsi_regime`` / ``strategy.choose_side`` /
``indicators.compute_rsi``) and settles each 15-minute window against the
actual BTC price move. The live bot and its paper-trading behavior are
untouched; this module only replays history through the same decision
functions.

Run:

    python -m backtest.backtester --days 21 --config config.example.yaml

which writes ``backtest/reports/backtest_report.json`` and prints a
per-regime summary table.

====================================================================
EXPLICIT ASSUMPTIONS (read before trusting any number below)
====================================================================
Historical Kalshi order-book snapshots and historical Kalshi trade prices
are not available, so the backtest cannot replay what the market actually
charged. Everything about price and fills is modeled, and the results are
conditional on these assumptions:

1. Entry price proxy. The price the bot would pay is proxied by a
   moneyness/vol model: the window close is treated as normal with mean =
   spot and stdev = 60s-candle log-return stdev over the lookback, scaled by
   sqrt(15). P(win) for the chosen side is the normal CDF of strike
   moneyness, rounded to whole cents. This is a *stand-in for the Kalshi
   ask*, not a measured Kalshi price. Fee-inclusive PnL moves with it; see
   threshold_sensitivity.py for how results shift with parameters.

2. Fill model. Every entered window is assumed filled in full at the
   proxied price, one fill per window, sized by the bot's own
   ``max_affordable_contracts`` budget math with the bot's own taker-fee
   model (``util.fee_cents_taker``). No slippage beyond fees, no queue
   position, no partial fills, no adverse selection. That is the optimistic
   case; ``fill_models.py`` builds the conservative counterpart and reports
   paper PnL under both.

3. Strike grid. Real strike selection reads live Kalshi markets. History has
   none, so strikes are taken from a synthetic round-number grid
   (``--strike-step-usd``, default 100): bullish takes the nearest grid
   level at or above spot, bearish the nearest at or below, neutral the
   nearest. Kalshi BTC-15M strikes sit on round levels, so the grid is a
   reasonable stand-in, but a different grid changes which windows trade.

4. Windows. 15-minute windows aligned to quarter hours (:00, :15, :30, :45),
   decision at window open, settlement at window close from the last
   60-second candle close. A Yes position wins when close >= strike, a No
   position when close < strike. Positions are always held to settlement,
   matching the bot (no early exit).

5. Closed candles only. The backtest uses fully-formed 60-second candles at
   decision time (no lookahead). The live loop reads candles as they stream
   and may include the in-progress candle; this is a stated difference in
   the harness, not a change to the bot.

6. Entry gates mirrored from the live bot: skip windows where the proxied
   ask exceeds ``entry_max_cents`` (the live "ask_too_high" gate) and where
   the budget cannot afford one contract. There is no spread gate in the
   backtest because historical spreads are unobservable.

What the backtest CAN show: whether the decision logic is consistent over
history, hit rate vs the proxied implied probability by regime
(calibration), profit factor, and max drawdown under the stated assumptions.

What it CANNOT show: actual Kalshi profitability. Entry prices, spreads,
queue position, and fills are modeled, not measured. Treat every PnL number
as "under the harness assumptions", never as an expected return.
====================================================================
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import statistics
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

from kalshi_btc15m_bot.indicators import compute_rsi
from kalshi_btc15m_bot.strategy import choose_side, rsi_regime
from kalshi_btc15m_bot.util import fee_cents_taker, max_affordable_contracts

from .data import fetch_candles

try:
    import yaml
except ImportError:  # config file is optional; CLI flags cover everything
    yaml = None

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

WINDOW_MINUTES = 15


# --------------------------------------------------------------------------
# Signal helpers
# --------------------------------------------------------------------------

def rsi_series(closes: List[float], period: int) -> List[Optional[float]]:
    """RSI value at every index, reproducing ``indicators.compute_rsi``.

    ``compute_rsi`` seeds Wilder smoothing with the simple mean of the first
    ``period`` gains/losses and then smooths over all deltas; this returns
    the same value ``compute_rsi(closes[:i+1], period)`` would give, in O(n).
    """
    out: List[Optional[float]] = [None] * len(closes)
    if period <= 0 or len(closes) < period + 1:
        return out
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def rsi_of(ag: float, al: float) -> float:
        if al == 0:
            return 100.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = rsi_of(avg_gain, avg_loss)
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = rsi_of(avg_gain, avg_loss)
    return out


def window_sigma(closes: List[float]) -> Optional[float]:
    """15-minute price stdev in USD, from 60s log returns (None if degenerate)."""
    if len(closes) < 3:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 2:
        return None
    sd = statistics.stdev(rets)
    if sd <= 0:
        return None
    return closes[-1] * sd * math.sqrt(WINDOW_MINUTES)


def implied_win_prob(spot: float, strike: float, sigma_w: float) -> float:
    """P(chosen direction wins) under N(spot, sigma_w); the ask-price proxy."""
    if sigma_w <= 0:
        return 0.5
    # P(close >= strike) for Yes; the No case is handled by the caller via 1 - p.
    z = (strike - spot) / sigma_w
    p_yes = 0.5 * math.erfc(z / math.sqrt(2.0))
    return min(max(p_yes, 0.01), 0.99)


def synthetic_strike(spot: float, regime: str, step_usd: float) -> float:
    """Nearest round-grid strike above/below spot (Assumption 3)."""
    if regime == "bullish":
        return math.ceil(spot / step_usd) * step_usd
    if regime == "bearish":
        return math.floor(spot / step_usd) * step_usd
    # neutral: nearest grid level
    return round(spot / step_usd) * step_usd


# --------------------------------------------------------------------------
# Backtest core
# --------------------------------------------------------------------------

class BacktestParams:
    def __init__(
        self,
        rsi_period: int = 14,
        bullish_rsi: float = 55.0,
        bearish_rsi: float = 45.0,
        neutral_fallback_side: str = "yes",
        lookback_minutes: int = 120,
        usd_budget: float = 10.0,
        entry_max_cents: int = 60,
        strike_step_usd: float = 100.0,
        product_id: str = "BTC-USD",
    ):
        self.rsi_period = rsi_period
        self.bullish_rsi = bullish_rsi
        self.bearish_rsi = bearish_rsi
        self.neutral_fallback_side = neutral_fallback_side
        self.lookback_minutes = lookback_minutes
        self.usd_budget = usd_budget
        self.entry_max_cents = entry_max_cents
        self.strike_step_usd = strike_step_usd
        self.product_id = product_id

    def as_dict(self) -> Dict:
        return {
            "rsi_period": self.rsi_period,
            "bullish_rsi": self.bullish_rsi,
            "bearish_rsi": self.bearish_rsi,
            "neutral_fallback_side": self.neutral_fallback_side,
            "lookback_minutes": self.lookback_minutes,
            "usd_budget": self.usd_budget,
            "entry_max_cents": self.entry_max_cents,
            "strike_step_usd": self.strike_step_usd,
            "product_id": self.product_id,
        }


def iter_window_starts(start: dt.datetime, end: dt.datetime):
    """Quarter-hour window starts in [start, end)."""
    cur = start.replace(minute=(start.minute // 15) * 15, second=0, microsecond=0)
    if cur < start:
        cur += dt.timedelta(minutes=15)
    step = dt.timedelta(minutes=WINDOW_MINUTES)
    while cur + step <= end:
        yield cur
        cur += step


def _new_regime_agg() -> Dict:
    return {"windows": 0, "wins": 0, "pnl_cents": 0, "gross_profit_cents": 0,
            "gross_loss_cents": 0, "implied_sum": 0.0, "fees_cents": 0}


def _finalize_regimes(per_regime: Dict[str, Dict]) -> Dict:
    regimes = {}
    for regime, r in per_regime.items():
        n = r["windows"]
        pf = (r["gross_profit_cents"] / r["gross_loss_cents"]) if r["gross_loss_cents"] > 0 else None
        regimes[regime] = {
            "traded_windows": n,
            "hit_rate": round(r["wins"] / n, 4) if n else None,
            "avg_implied_prob": round(r["implied_sum"] / n, 4) if n else None,
            "pnl_cents": r["pnl_cents"],
            "pnl_dollars": round(r["pnl_cents"] / 100, 2),
            "fees_cents": r["fees_cents"],
            "profit_factor": round(pf, 3) if pf is not None else None,
        }
    return regimes


def run_backtest(
    candles: List[Tuple[dt.datetime, float, float, float, float, float]],
    params: BacktestParams,
    start: dt.datetime,
    end: dt.datetime,
    collect_trades: bool = True,
) -> Dict:
    """Replay windows over ``candles``; return the full result dict."""
    import bisect

    ts = [c[0] for c in candles]
    closes = [c[4] for c in candles]
    rsi_vals = rsi_series(closes, params.rsi_period)
    budget_cents = int(round(params.usd_budget * 100))
    lookback = dt.timedelta(minutes=params.lookback_minutes)
    window_td = dt.timedelta(minutes=WINDOW_MINUTES)

    trades: List[Dict] = []
    per_regime: Dict[str, Dict] = {}
    skipped = {"no_signal": 0, "ask_too_high": 0, "cannot_afford": 0, "no_settle_candle": 0}
    n_windows = 0

    # Drawdown tracking on the traded equity curve (chronological).
    equity = 0
    peak = 0
    max_dd = 0

    for w_start in iter_window_starts(start, end):
        n_windows += 1
        w_end = w_start + window_td

        # Decision snapshot: closed candles strictly before window open,
        # within the lookback (Assumption 5). Index arithmetic via bisect.
        i_end = bisect.bisect_left(ts, w_start)
        i_start = bisect.bisect_left(ts, w_start - lookback)
        if i_end - i_start < params.rsi_period + 1:
            skipped["no_signal"] += 1
            continue
        rsi = rsi_vals[i_end - 1]
        if rsi is None:
            skipped["no_signal"] += 1
            continue

        spot = closes[i_end - 1]
        regime = rsi_regime(rsi, params.bullish_rsi, params.bearish_rsi)
        strike = synthetic_strike(spot, regime, params.strike_step_usd)
        side = choose_side(
            spot=spot,
            rsi=rsi,
            bullish_th=params.bullish_rsi,
            bearish_th=params.bearish_rsi,
            strike=strike,
            neutral_fallback_side=params.neutral_fallback_side,
        )

        sigma_w = window_sigma(closes[i_start:i_end])
        if sigma_w is None:
            skipped["no_signal"] += 1
            continue
        p_yes = implied_win_prob(spot, strike, sigma_w)
        p_win = p_yes if side == "yes" else 1.0 - p_yes
        price_cents = int(round(p_win * 100))
        price_cents = max(1, min(99, price_cents))

        if price_cents > params.entry_max_cents:  # mirrors live ask_too_high gate
            skipped["ask_too_high"] += 1
            continue
        count = max_affordable_contracts(budget_cents, price_cents, "taker")
        if count < 1:
            skipped["cannot_afford"] += 1
            continue

        # Settlement: last closed candle of the window (Assumption 4).
        s_end = bisect.bisect_left(ts, w_end)
        s_start = bisect.bisect_left(ts, w_start)
        if s_end <= s_start:
            skipped["no_settle_candle"] += 1
            continue
        settle_close = closes[s_end - 1]
        win = (settle_close >= strike) if side == "yes" else (settle_close < strike)

        cost_cents = count * price_cents
        fee_cents = fee_cents_taker(count, price_cents)
        payoff_cents = count * 100 if win else 0
        pnl_cents = payoff_cents - cost_cents - fee_cents

        r = per_regime.setdefault(regime, _new_regime_agg())
        r["windows"] += 1
        r["wins"] += 1 if win else 0
        r["pnl_cents"] += pnl_cents
        r["fees_cents"] += fee_cents
        if pnl_cents >= 0:
            r["gross_profit_cents"] += pnl_cents
        else:
            r["gross_loss_cents"] += -pnl_cents
        r["implied_sum"] += p_win

        equity += pnl_cents
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

        if collect_trades:
            trades.append(
                {
                    "window_start": w_start.isoformat(),
                    "regime": regime,
                    "side": side,
                    "rsi": round(rsi, 2),
                    "spot": round(spot, 2),
                    "strike": strike,
                    "implied_prob": round(p_win, 4),
                    "price_cents": price_cents,
                    "count": count,
                    "cost_cents": cost_cents,
                    "fee_cents": fee_cents,
                    "win": bool(win),
                    "pnl_cents": pnl_cents,
                }
            )

    regimes = _finalize_regimes(per_regime)
    total_pnl = sum(r["pnl_cents"] for r in per_regime.values())
    gross_profit = sum(r["gross_profit_cents"] for r in per_regime.values())
    gross_loss = sum(r["gross_loss_cents"] for r in per_regime.values())

    report = {
        "params": params.as_dict(),
        "windows_evaluated": n_windows,
        "windows_traded": sum(r["windows"] for r in per_regime.values()),
        "skipped": skipped,
        "by_regime": regimes,
        "total_pnl_cents": total_pnl,
        "total_pnl_dollars": round(total_pnl / 100, 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else None,
        "max_drawdown_cents": max_dd,
        "max_drawdown_dollars": round(max_dd / 100, 2),
    }
    if collect_trades:
        report["trades"] = trades
    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def load_params_from_config(path: str) -> BacktestParams:
    p = BacktestParams()
    if not path or yaml is None:
        return p
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    strat = cfg.get("strategy", {})
    ex = cfg.get("execution", {})
    pf = cfg.get("price_feed", {})
    p.rsi_period = int(strat.get("rsi_period", p.rsi_period))
    p.bullish_rsi = float(strat.get("bullish_rsi", p.bullish_rsi))
    p.bearish_rsi = float(strat.get("bearish_rsi", p.bearish_rsi))
    p.neutral_fallback_side = str(strat.get("neutral_fallback_side", p.neutral_fallback_side))
    p.lookback_minutes = int(strat.get("history_lookback_minutes", p.lookback_minutes))
    p.usd_budget = float(ex.get("usd_budget", p.usd_budget))
    p.entry_max_cents = int(ex.get("entry_max_cents", p.entry_max_cents))
    p.product_id = str(pf.get("product_id", p.product_id))
    return p


def print_summary(report: Dict) -> None:
    print(f"windows evaluated: {report['windows_evaluated']}   "
          f"traded: {report['windows_traded']}   skipped: {report['skipped']}")
    print(f"{'regime':<9} {'n':>5} {'hit_rate':>9} {'avg_implied':>11} "
          f"{'pnl_$':>9} {'profit_factor':>13}")
    for regime in ("bullish", "bearish", "neutral"):
        r = report["by_regime"].get(regime)
        if not r:
            continue
        pf = f"{r['profit_factor']:.3f}" if r["profit_factor"] is not None else "n/a"
        print(f"{regime:<9} {r['traded_windows']:>5} {r['hit_rate']:>9.3f} "
              f"{r['avg_implied_prob']:>11.3f} {r['pnl_dollars']:>9.2f} {pf:>13}")
    pf = report["profit_factor"]
    print(f"total PnL: ${report['total_pnl_dollars']:.2f}   "
          f"profit factor: {pf if pf is None else f'{pf:.3f}'}   "
          f"max drawdown: ${report['max_drawdown_dollars']:.2f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Historical backtester (validation harness).")
    ap.add_argument("--days", type=int, default=21, help="lookback in days ending at --end")
    ap.add_argument("--end", default=None, help="end date YYYY-MM-DD (default: today, UTC)")
    ap.add_argument("--config", default="config.example.yaml", help="bot config yaml (strategy/execution params)")
    ap.add_argument("--strike-step-usd", type=float, default=100.0)
    ap.add_argument("--report", default=os.path.join(REPORTS_DIR, "backtest_report.json"))
    ap.add_argument("--bullish", type=float, default=None, help="override bullish RSI threshold")
    ap.add_argument("--bearish", type=float, default=None, help="override bearish RSI threshold")
    ap.add_argument("--rsi-period", type=int, default=None, help="override RSI period")
    args = ap.parse_args(argv)

    end = (
        dt.datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
        if args.end
        else dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    )
    # Pad the fetch so the last window has a settle candle and the first
    # window has a full lookback.
    params = load_params_from_config(args.config)
    if args.bullish is not None:
        params.bullish_rsi = args.bullish
    if args.bearish is not None:
        params.bearish_rsi = args.bearish
    if args.rsi_period is not None:
        params.rsi_period = args.rsi_period
    params.strike_step_usd = args.strike_step_usd

    start = end - dt.timedelta(days=args.days)
    fetch_start = start - dt.timedelta(minutes=params.lookback_minutes + 60)
    fetch_end = end + dt.timedelta(minutes=WINDOW_MINUTES + 5)
    print(f"fetching {params.product_id} 60s candles {fetch_start.date()} -> {fetch_end.date()} ...")
    candles = fetch_candles(params.product_id, fetch_start, fetch_end)

    report = run_backtest(candles, params, start, end)
    report["data_range"] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "candles": len(candles),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    with open(args.report, "w") as f:
        json.dump(report, f, indent=2)
    print_summary(report)
    print(f"report written to {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
