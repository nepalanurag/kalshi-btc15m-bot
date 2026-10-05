"""Parameter sensitivity report for the kalshi-btc15m-bot (validation hardening).

Sweeps the RSI thresholds and RSI period over a grid and reports how
backtest PnL and hit rate move. The current 55/45 thresholds and 14-period
RSI are treated here as *starting points under evaluation*: this report
shows the surface around them so the choice can be revisited with evidence
instead of habit.

Run:

    python -m backtest.threshold_sensitivity --days 21 --config config.example.yaml

Writes ``backtest/reports/threshold_report.json`` and prints the ranked
table. Reuses ``backtest.backtester`` (same assumptions, same harness);
each grid point is one full backtest replay.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

from .backtester import (
    BacktestParams,
    load_params_from_config,
    run_backtest,
)
from .data import fetch_candles

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

BULLISH_GRID = [50, 52, 55, 58, 60]
BEARISH_GRID = [40, 42, 45, 48, 50]
PERIOD_GRID = [10, 14, 21]


def grid_points():
    pts = []
    for period in PERIOD_GRID:
        for bull in BULLISH_GRID:
            for bear in BEARISH_GRID:
                if bear < bull:
                    pts.append((period, bull, bear))
    return pts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="RSI threshold/period sensitivity report.")
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--end", default=None, help="end date YYYY-MM-DD (default: today, UTC)")
    ap.add_argument("--config", default="config.example.yaml")
    ap.add_argument("--strike-step-usd", type=float, default=100.0)
    ap.add_argument("--report", default=os.path.join(REPORTS_DIR, "threshold_report.json"))
    args = ap.parse_args(argv)

    end = (
        dt.datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
        if args.end
        else dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    )
    base = load_params_from_config(args.config)
    base.strike_step_usd = args.strike_step_usd

    start = end - dt.timedelta(days=args.days)
    fetch_start = start - dt.timedelta(minutes=base.lookback_minutes + 60)
    fetch_end = end + dt.timedelta(minutes=20)
    print(f"fetching {base.product_id} 60s candles {fetch_start.date()} -> {fetch_end.date()} ...")
    candles = fetch_candles(base.product_id, fetch_start, fetch_end)

    points = grid_points()
    print(f"running {len(points)} grid points over {args.days} days ...")
    rows = []
    for i, (period, bull, bear) in enumerate(points):
        p = BacktestParams(
            rsi_period=period,
            bullish_rsi=bull,
            bearish_rsi=bear,
            neutral_fallback_side=base.neutral_fallback_side,
            lookback_minutes=base.lookback_minutes,
            usd_budget=base.usd_budget,
            entry_max_cents=base.entry_max_cents,
            strike_step_usd=base.strike_step_usd,
            product_id=base.product_id,
        )
        rep = run_backtest(candles, p, start, end, collect_trades=False)
        row = {
            "rsi_period": period,
            "bullish_rsi": bull,
            "bearish_rsi": bear,
            "windows_traded": rep["windows_traded"],
            "total_pnl_dollars": rep["total_pnl_dollars"],
            "profit_factor": rep["profit_factor"],
            "max_drawdown_dollars": rep["max_drawdown_dollars"],
            "hit_rate_by_regime": {
                r: v["hit_rate"] for r, v in rep["by_regime"].items()
            },
        }
        rows.append(row)
        if (i + 1) % 12 == 0:
            print(f"  ... {i + 1}/{len(points)}")

    rows.sort(key=lambda r: (r["total_pnl_dollars"] is not None, r["total_pnl_dollars"]))

    report = {
        "data_range": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "candles": len(candles),
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
        "grid": {
            "rsi_period": PERIOD_GRID,
            "bullish_rsi": BULLISH_GRID,
            "bearish_rsi": BEARISH_GRID,
        },
        "note": ("Thresholds 55/45 and period 14 are starting points under "
                 "evaluation. This table shows how backtest PnL moves around them "
                 "under the harness assumptions in backtester.py."),
        "results": rows,
    }
    with open(args.report, "w") as f:
        json.dump(report, f, indent=2)

    print(f"\n{'period':>6} {'bull':>4} {'bear':>4} {'n':>5} {'pnl_$':>9} "
          f"{'pf':>6} {'maxdd_$':>8}   (sorted worst -> best)")
    for r in rows:
        pf = f"{r['profit_factor']:.2f}" if r["profit_factor"] is not None else "n/a"
        print(f"{r['rsi_period']:>6} {r['bullish_rsi']:>4} {r['bearish_rsi']:>4} "
              f"{r['windows_traded']:>5} {r['total_pnl_dollars']:>9.2f} "
              f"{pf:>6} {r['max_drawdown_dollars']:>8.2f}")
    # Highlight the current starting point.
    cur = [r for r in rows
           if r["rsi_period"] == 14 and r["bullish_rsi"] == 55 and r["bearish_rsi"] == 45]
    if cur:
        c = cur[0]
        print(f"\nstarting point (14 / 55 / 45): PnL ${c['total_pnl_dollars']:.2f}, "
              f"rank {rows.index(c) + 1} of {len(rows)} by PnL")
    print(f"report written to {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
