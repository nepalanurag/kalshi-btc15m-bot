"""Live-monitoring dashboard data generator.

Reads the lake (and, when present, the backtester's report and the bot's
paper trade log) and writes:

* ``dashboard/dashboard.json`` -- every computed metric, machine-readable.
* ``dashboard/index.html``      -- a static page with the same numbers.

Metrics:

* **coverage** -- partitions present, time range, snapshot/candle counts.
* **microstructure** -- mean/median spread (cents), mean top-of-book depth,
  share of snapshots with an empty side of the book.
* **regime attribution** -- from ``backtest/reports/backtest_report.json``:
  hit rate, PnL, profit factor per RSI regime (bullish/bearish/neutral).
* **fills & slippage** -- from a bot paper-trades CSV (``--trades-csv``):
  fill rate, and paper price vs the lake mid-price at the nearest snapshot
  ("realized-vs-paper" once live fills exist; labeled paper-vs-mid until then).

Every section degrades gracefully: a missing input produces a labeled
"no data" note, never an invented number and never a crash.

Run:

    python -m datalake.dashboard
    python -m datalake.dashboard --trades-csv /path/to/trades.csv
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pyarrow.parquet as pq

from datalake import DATA_CONTRACT_VERSION
from datalake.config import get_settings
from datalake.logging import configure_logging, get_logger

UTC = dt.timezone.utc
log = get_logger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BACKTEST_REPORT = os.path.join(REPO_ROOT, "backtest", "reports", "backtest_report.json")


# --------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------


def list_partitions(lake_dir: str) -> List[str]:
    """Return sorted partition dirs like lake/date=.../hour=... ."""
    out = []
    if not os.path.isdir(lake_dir):
        return out
    for date_dir in sorted(os.listdir(lake_dir)):
        if not date_dir.startswith("date="):
            continue
        for hour_dir in sorted(os.listdir(os.path.join(lake_dir, date_dir))):
            if hour_dir.startswith("hour="):
                out.append(os.path.join(lake_dir, date_dir, hour_dir))
    return out


def read_partition_frames(partition: str) -> Dict[str, pd.DataFrame]:
    frames: Dict[str, pd.DataFrame] = {}
    for name in ("snapshots", "candles"):
        path = os.path.join(partition, f"{name}.parquet")
        frames[name] = pq.read_table(path).to_pandas() if os.path.exists(path) else pd.DataFrame()
    manifest_path = os.path.join(partition, "manifest.json")
    frames["manifest"] = {}
    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            frames["manifest"] = json.load(f)
    return frames


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def coverage_metrics(partitions: List[str]) -> Dict[str, Any]:
    if not partitions:
        return {"status": "no_data", "note": "no lake partitions found"}
    snaps_n = candles_n = 0
    tmin: Optional[pd.Timestamp] = None
    tmax: Optional[pd.Timestamp] = None
    for p in partitions:
        for name in ("snapshots", "candles"):
            path = os.path.join(p, f"{name}.parquet")
            if not os.path.exists(path):
                continue
            df = pq.read_table(path, columns=["feed_ts"]).to_pandas()
            if name == "snapshots":
                snaps_n += len(df)
            else:
                candles_n += len(df)
            if not df.empty:
                ts = pd.to_datetime(df["feed_ts"], utc=True)
                tmin = ts.min() if tmin is None else min(tmin, ts.min())
                tmax = ts.max() if tmax is None else max(tmax, ts.max())
    return {
        "status": "ok",
        "partitions": len(partitions),
        "partition_list": [os.path.relpath(p) for p in partitions],
        "snapshots": snaps_n,
        "candles": candles_n,
        "time_range": {
            "min": tmin.isoformat() if tmin is not None else None,
            "max": tmax.isoformat() if tmax is not None else None,
        },
    }


def microstructure_metrics(partitions: List[str]) -> Dict[str, Any]:
    spreads: List[float] = []
    depths: List[float] = []
    empty_side = 0
    n = 0
    for p in partitions:
        path = os.path.join(p, "snapshots.parquet")
        if not os.path.exists(path):
            continue
        df = pq.read_table(
            path,
            columns=[
                "spread_cents",
                "n_levels_yes",
                "n_levels_no",
                "yes_bid_cents",
                "yes_ask_cents",
            ],
        ).to_pandas()
        n += len(df)
        spreads.extend(df["spread_cents"].dropna().tolist())
        depths.extend((df["n_levels_yes"] + df["n_levels_no"]).tolist())
        empty_side += int((df["yes_bid_cents"].isna() | df["yes_ask_cents"].isna()).sum())
    if n == 0:
        return {"status": "no_data", "note": "no snapshots in lake"}
    s = pd.Series(spreads)
    d = pd.Series(depths)
    return {
        "status": "ok",
        "snapshots": n,
        "spread_cents": {
            "mean": round(float(s.mean()), 2),
            "median": round(float(s.median()), 2),
            "p95": round(float(s.quantile(0.95)), 2),
            "max": round(float(s.max()), 2),
        },
        "book_depth_levels": {
            "mean": round(float(d.mean()), 1),
            "median": round(float(d.median()), 1),
        },
        "empty_side_share": round(empty_side / n, 4),
    }


def regime_metrics(backtest_report: str) -> Dict[str, Any]:
    if not os.path.exists(backtest_report):
        return {"status": "no_data", "note": f"backtest report not found: {backtest_report}"}
    with open(backtest_report) as f:
        rep = json.load(f)
    by_regime = rep.get("by_regime", {})
    out: Dict[str, Any] = {"status": "ok", "regimes": {}}
    for regime in ("bullish", "bearish", "neutral"):
        r = by_regime.get(regime)
        if not r:
            continue
        out["regimes"][regime] = {
            "traded_windows": r.get("traded_windows"),
            "hit_rate": r.get("hit_rate"),
            "avg_implied_prob": r.get("avg_implied_prob"),
            "pnl_dollars": r.get("pnl_dollars"),
            "profit_factor": r.get("profit_factor"),
            "calibration_gap": (
                round(r["hit_rate"] - r["avg_implied_prob"], 4)
                if r.get("hit_rate") is not None and r.get("avg_implied_prob") is not None
                else None
            ),
        }
    out["totals"] = {
        "windows_traded": rep.get("windows_traded"),
        "total_pnl_dollars": rep.get("total_pnl_dollars"),
        "profit_factor": rep.get("profit_factor"),
        "max_drawdown_dollars": rep.get("max_drawdown_dollars"),
        "data_range": rep.get("data_range", {}).get("start"),
    }
    return out


def fill_metrics(trades_csv: Optional[str], partitions: List[str]) -> Dict[str, Any]:
    if not trades_csv or not os.path.exists(trades_csv):
        return {
            "status": "no_data",
            "note": "no paper-trades CSV supplied (--trades-csv); "
            "fill rate and slippage need bot trade logs",
        }
    df = pd.read_csv(trades_csv)
    if df.empty:
        return {"status": "no_data", "note": "trades CSV is empty"}
    n = len(df)
    out: Dict[str, Any] = {
        "status": "ok",
        "trades": n,
        "modes": df["mode"].value_counts().to_dict() if "mode" in df.columns else {},
        "avg_contracts": (
            round(float(df["contracts"].mean()), 2) if "contracts" in df.columns else None
        ),
        "total_pnl_cents": int(df["pnl_cents"].sum()) if "pnl_cents" in df.columns else None,
    }
    # Paper price vs lake mid at nearest snapshot: realized-vs-paper slippage
    # once live fills exist; until then this compares paper fills to the mid.
    if "time" in df.columns and "price_cents" in df.columns and partitions:
        snaps = []
        for p in partitions:
            path = os.path.join(p, "snapshots.parquet")
            if os.path.exists(path):
                snaps.append(
                    pq.read_table(
                        path, columns=["feed_ts", "yes_bid_cents", "yes_ask_cents"]
                    ).to_pandas()
                )
        if snaps:
            book = pd.concat(snaps, ignore_index=True)
            book["mid"] = (
                pd.to_numeric(book["yes_bid_cents"], errors="coerce")
                + pd.to_numeric(book["yes_ask_cents"], errors="coerce")
            ) / 2
            book = book.dropna(subset=["mid"]).sort_values("feed_ts")
            book_ts = pd.to_datetime(book["feed_ts"], utc=True).values.astype("int64")
            diffs = []
            for _, row in df.iterrows():
                try:
                    t = pd.Timestamp(row["time"], tz="UTC").value
                except (ValueError, TypeError):
                    continue
                j = int(abs(book_ts - t).argmin())
                nearest_age_s = abs(book_ts[j] - t) / 1e9
                if nearest_age_s <= 600:  # 10-minute join window
                    diffs.append(float(row["price_cents"]) - float(book["mid"].iloc[j]))
            if diffs:
                s = pd.Series(diffs)
                out["paper_vs_mid_slippage_cents"] = {
                    "n_joined": len(diffs),
                    "mean": round(float(s.mean()), 2),
                    "median": round(float(s.median()), 2),
                    "note": "paper fill price minus lake mid-price at nearest snapshot; "
                    "positive = paid above mid",
                }
            else:
                out["paper_vs_mid_slippage_cents"] = {
                    "note": "no trade timestamps within 10 min of a lake snapshot"
                }
    return out


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------


def _kv_table(rows: List[tuple]) -> str:
    cells = "".join(
        f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>" for k, v in rows
    )
    return f"<table>{cells}</table>"


def render_html(bundle: Dict[str, Any]) -> str:
    def section(title: str, body: str) -> str:
        return f"<section><h2>{html.escape(title)}</h2>{body}</section>"

    cov = bundle["coverage"]
    if cov["status"] == "ok":
        cov_body = _kv_table(
            [
                ("partitions", cov["partitions"]),
                ("snapshots", cov["snapshots"]),
                ("candles", cov["candles"]),
                ("from", cov["time_range"]["min"]),
                ("to", cov["time_range"]["max"]),
            ]
        )
    else:
        cov_body = f"<p class='nodata'>{html.escape(cov['note'])}</p>"

    ms = bundle["microstructure"]
    if ms["status"] == "ok":
        cov_body2 = _kv_table(
            [
                ("mean spread (c)", ms["spread_cents"]["mean"]),
                ("median spread (c)", ms["spread_cents"]["median"]),
                ("p95 spread (c)", ms["spread_cents"]["p95"]),
                ("mean book depth (levels)", ms["book_depth_levels"]["mean"]),
                ("empty-side share", ms["empty_side_share"]),
            ]
        )
    else:
        cov_body2 = f"<p class='nodata'>{html.escape(ms['note'])}</p>"

    rg = bundle["regime_attribution"]
    if rg["status"] == "ok" and rg["regimes"]:
        hdr = (
            "<tr><th>regime</th><th>windows</th><th>hit rate</th>"
            "<th>avg implied</th><th>calibration gap</th><th>PnL $</th>"
            "<th>profit factor</th></tr>"
        )
        rows = ""
        for regime, r in rg["regimes"].items():
            cells = "".join(
                f"<td>{html.escape(str(r.get(k, 'n/a')))}</td>"
                for k in [
                    "traded_windows",
                    "hit_rate",
                    "avg_implied_prob",
                    "calibration_gap",
                    "pnl_dollars",
                    "profit_factor",
                ]
            )
            rows += f"<tr><td>{html.escape(regime)}</td>{cells}</tr>"
        rg_body = (
            f"<table>{hdr}{rows}</table>"
            f"<p>Totals: {html.escape(str(rg['totals']['windows_traded']))} windows, "
            f"PnL ${html.escape(str(rg['totals']['total_pnl_dollars']))}, "
            f"max drawdown ${html.escape(str(rg['totals']['max_drawdown_dollars']))}.</p>"
            f"<p class='note'>From the backtest harness "
            f"(backtest/reports/backtest_report.json); PnL is under the harness's "
            f"documented assumptions, not a live-trading claim.</p>"
        )
    else:
        rg_body = f"<p class='nodata'>{html.escape(rg.get('note', 'no regime data'))}</p>"

    fl = bundle["fills"]
    if fl["status"] == "ok":
        fl_body = _kv_table([(k, v) for k, v in fl.items() if k != "status"])
    else:
        fl_body = f"<p class='nodata'>{html.escape(fl['note'])}</p>"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>kalshi-btc15m-bot &mdash; market-data lake dashboard</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 860px; margin: 2rem auto;
       padding: 0 1rem; color: #1a1a1a; }}
h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.1rem; margin-top: 2rem;
    border-bottom: 1px solid #ddd; padding-bottom: .3rem; }}
table {{ border-collapse: collapse; margin: .5rem 0; }}
th, td {{ border: 1px solid #ddd; padding: .35rem .7rem; text-align: left;
          font-size: .9rem; }}
th {{ background: #f5f5f5; }}
.nodata {{ color: #777; font-style: italic; }}
.note {{ color: #555; font-size: .85rem; }}
footer {{ margin-top: 3rem; color: #888; font-size: .8rem; }}
</style></head>
<body>
<h1>Market-data lake dashboard</h1>
<p class="note">Generated {html.escape(bundle['generated_at'])} from
datalake contract v{html.escape(bundle['contract_version'])}.</p>
{section("Coverage", cov_body)}
{section("Microstructure (order-book snapshots)", cov_body2)}
{section("PnL attribution by RSI regime (backtest)", rg_body)}
{section("Fills & slippage", fl_body)}
<footer>kalshi-btc15m-bot market-data lake. Static page regenerated by
<code>python -m datalake.dashboard</code>.</footer>
</body></html>
"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_dashboard(
    lake_dir: str, backtest_report: str, trades_csv: Optional[str]
) -> Dict[str, Any]:
    partitions = list_partitions(lake_dir)
    bundle = {
        "contract_version": DATA_CONTRACT_VERSION,
        "generated_at": dt.datetime.now(UTC).isoformat(),
        "coverage": coverage_metrics(partitions),
        "microstructure": microstructure_metrics(partitions),
        "regime_attribution": regime_metrics(backtest_report),
        "fills": fill_metrics(trades_csv, partitions),
    }
    log.info("dashboard_built", partitions=len(partitions))
    return bundle


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Generate the monitoring dashboard (dashboard.json + index.html) "
        "from the lake."
    )
    ap.add_argument("--lake-dir", default=None)
    ap.add_argument(
        "--backtest-report", default=None, help="default: backtest/reports/backtest_report.json"
    )
    ap.add_argument(
        "--trades-csv", default=None, help="bot paper-trades CSV for the fill-rate section"
    )
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--log-format", choices=["json", "console"], default=None)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    if args.lake_dir:
        settings.lake_dir = args.lake_dir
    if args.out_dir:
        settings.dashboard_dir = args.out_dir
    if args.log_format:
        settings.log_format = args.log_format
    configure_logging(settings.log_format, settings.log_level)

    bundle = build_dashboard(
        settings.lake_dir,
        args.backtest_report or DEFAULT_BACKTEST_REPORT,
        args.trades_csv,
    )
    os.makedirs(settings.dashboard_dir, exist_ok=True)
    json_path = os.path.join(settings.dashboard_dir, "dashboard.json")
    html_path = os.path.join(settings.dashboard_dir, "index.html")
    with open(json_path, "w") as f:
        json.dump(bundle, f, indent=2)
    with open(html_path, "w") as f:
        f.write(render_html(bundle))
    log.info("dashboard_written", json_path=json_path, html_path=html_path)
    print(f"wrote {json_path}\nwrote {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
