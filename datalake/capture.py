"""Scheduled capture loop: Kalshi order-book snapshots + Coinbase 60s candles.

Every ``--interval`` seconds (default 60, aligned to wall-clock minute
boundaries) this fetches:

1. the order-book snapshot of the currently active BTC 15-minute Kalshi
   market (public API, no auth), and
2. the latest *closed* Coinbase 60-second BTC-USD candle (public API),

and appends both as JSON lines to an hourly buffer file::

    datalake/buffer/2026-10-07T09Z.jsonl

API errors never crash the loop: each fetch is retried with exponential
backoff + jitter, and after ``max_retries`` the iteration is skipped with a
logged error. SIGINT/SIGTERM shut the loop down cleanly after the current
iteration.

Run:

    python -m datalake.capture --iterations 5     # demo: 5 captures, then exit
    python -m datalake.capture                    # run forever (systemd/cron)
    DATALAKE_LOG_FORMAT=console python -m datalake.capture --once
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import signal
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datalake.config import DatalakeSettings, get_settings
from datalake.logging import configure_logging, get_logger
from kalshi_btc15m_bot.kalshi_client import KalshiApiError, KalshiClient
from kalshi_btc15m_bot.price_feed import CoinbasePriceFeed, PriceFeedError

UTC = dt.timezone.utc
log = get_logger(__name__)

_shutdown = False


def _on_signal(signum, frame):  # noqa: ANN001, ANN202
    global _shutdown
    _shutdown = True
    log.info("shutdown_requested", signal=signum)


def with_retries(fn: Callable[[], Any], *, what: str, settings: DatalakeSettings) -> Optional[Any]:
    """Run ``fn`` with exponential backoff; return None after max_retries.

    Never raises for API/transport errors: the capture loop must survive
    upstream outages and simply skip the iteration.
    """
    delay = settings.backoff_base_seconds
    for attempt in range(1, settings.max_retries + 1):
        try:
            return fn()
        except (KalshiApiError, PriceFeedError, OSError, ValueError) as exc:
            log.warning(
                "fetch_failed",
                what=what,
                attempt=attempt,
                max_retries=settings.max_retries,
                error=str(exc)[:200],
            )
            if attempt >= settings.max_retries:
                log.error("fetch_gave_up", what=what, attempts=attempt)
                return None
            time.sleep(delay * (0.5 + random.random()))
            delay *= 2
    return None


def _parse_ts(value: str) -> Optional[dt.datetime]:
    try:
        ts = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def pick_active_market(markets: List[Dict[str, Any]], now: dt.datetime) -> Optional[Dict[str, Any]]:
    """Choose the BTC-15m market that is live right now.

    Only markets whose own ``status`` is active/open are eligible -- the
    API's ``status=open`` filter is eventually consistent and can return a
    just-closed market during rollover. Prefers the market whose
    [open_time, close_time] contains ``now``; falls back to the eligible
    market with the earliest close_time. Returns None when nothing is
    eligible (the caller skips the iteration).
    """
    eligible = [m for m in markets if str(m.get("status", "")).lower() in ("open", "active")]
    if not eligible:
        return None
    containing = []
    for m in eligible:
        o = _parse_ts(str(m.get("open_time") or ""))
        c = _parse_ts(str(m.get("close_time") or ""))
        if o is not None and c is not None and o <= now <= c:
            containing.append((o, m))
    if containing:
        containing.sort(key=lambda t: t[0])
        return containing[0][1]
    dated = []
    for m in eligible:
        c = _parse_ts(str(m.get("close_time") or ""))
        if c is not None:
            dated.append((c, m))
    if dated:
        dated.sort(key=lambda t: t[0])
        return dated[0][1]
    return None


def _dollars_to_cents(value: Any) -> Optional[int]:
    try:
        return int(round(float(str(value)) * 100))
    except (TypeError, ValueError):
        return None


def _ladder(levels: Any) -> List[Tuple[int, float]]:
    """Normalize ``[[price_dollars, size], ...]`` to ``[(cents, size)]``."""
    out: List[Tuple[int, float]] = []
    for row in levels or []:
        try:
            price_c = int(round(float(str(row[0])) * 100))
            size = float(str(row[1]))
        except (TypeError, ValueError, IndexError):
            continue
        out.append((price_c, size))
    return out


def build_snapshot(
    market: Dict[str, Any],
    orderbook: Dict[str, Any],
    captured_at: dt.datetime,
) -> Dict[str, Any]:
    """Flatten a Kalshi market + orderbook into the v1.0 snapshot contract."""
    ob = orderbook.get("orderbook_fp") or orderbook.get("orderbook") or {}
    yes_ladder = _ladder(ob.get("yes_dollars"))
    no_ladder = _ladder(ob.get("no_dollars"))

    yes_bid = max((p for p, _ in yes_ladder), default=None)
    no_bid = max((p for p, _ in no_ladder), default=None)
    # Complement rule (same as backtest/fill_models.ladder_from_orderbook):
    # buying YES lifts NO bids at y -> YES ask = 100 - y.
    yes_ask = 100 - no_bid if no_bid is not None else None
    no_ask = 100 - yes_bid if yes_bid is not None else None
    spread = (yes_ask - yes_bid) if (yes_ask is not None and yes_bid is not None) else None

    strike = None
    for key in ("floor_strike", "cap_strike", "strike"):
        raw = market.get(key)
        if raw is None:
            continue
        try:
            strike = float(str(raw))  # Kalshi strikes are quoted in dollars
            break
        except (TypeError, ValueError):
            continue

    return {
        "record_type": "snapshot",
        "captured_at": captured_at.isoformat(),
        "feed_ts": captured_at.isoformat(),
        "market_ticker": str(market.get("ticker") or orderbook.get("market_ticker") or ""),
        "strike": strike,
        "status": str(market.get("status") or ""),
        "yes_bid_cents": yes_bid,
        "yes_ask_cents": yes_ask,
        "no_bid_cents": no_bid,
        "no_ask_cents": no_ask,
        "spread_cents": spread,
        "n_levels_yes": len(yes_ladder),
        "n_levels_no": len(no_ladder),
        "ladder_yes_json": json.dumps(yes_ladder),
        "ladder_no_json": json.dumps(no_ladder),
    }


def fetch_latest_closed_candle(
    feed: CoinbasePriceFeed, now: dt.datetime
) -> Optional[Dict[str, Any]]:
    """Return the latest fully-closed 60s candle as a v1.0 candle record."""
    start = now - dt.timedelta(minutes=5)
    candles = feed.get_candles(start=start, end=now, granularity_seconds=60)
    closed = [c for c in candles if c.ts + dt.timedelta(seconds=60) <= now]
    if not closed:
        return None
    c = closed[-1]
    captured = now
    return {
        "record_type": "candle",
        "captured_at": captured.isoformat(),
        "feed_ts": c.ts.isoformat(),
        "candle_start": c.ts.isoformat(),
        "open": c.open,
        "high": c.high,
        "low": c.low,
        "close": c.close,
        "volume": c.volume,
    }


def buffer_path(buffer_dir: str, captured_at: dt.datetime) -> str:
    hour = captured_at.strftime("%Y-%m-%dT%HZ")
    return os.path.join(buffer_dir, f"{hour}.jsonl")


def append_records(path: str, records: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def capture_once(
    settings: DatalakeSettings, client: KalshiClient, feed: CoinbasePriceFeed
) -> Dict[str, int]:
    """One capture iteration. Returns counts; never raises on API errors."""
    now = dt.datetime.now(UTC)
    stats = {"snapshots": 0, "candles": 0, "skipped": 0}

    data = with_retries(
        lambda: client.get_markets(
            series_ticker=settings.kalshi_series_ticker, status="open", limit=100
        ),
        what="kalshi_markets",
        settings=settings,
    )
    if not data:
        stats["skipped"] += 1
        return stats
    market = pick_active_market(data.get("markets", []) or [], now)
    if market is None:
        log.error("no_active_market", series=settings.kalshi_series_ticker)
        stats["skipped"] += 1
        return stats

    ticker = str(market.get("ticker") or "")
    orderbook = with_retries(
        lambda: client.get_orderbook(ticker), what=f"kalshi_orderbook:{ticker}", settings=settings
    )
    if not orderbook:
        stats["skipped"] += 1
        return stats

    records = [build_snapshot(market, orderbook, now)]
    stats["snapshots"] = 1

    candle = with_retries(
        lambda: fetch_latest_closed_candle(feed, now), what="coinbase_candle", settings=settings
    )
    if candle is None:
        log.warning("no_closed_candle", product=settings.coinbase_product_id)
    else:
        records.append(candle)
        stats["candles"] = 1

    path = buffer_path(settings.buffer_dir, now)
    append_records(path, records)
    log.info(
        "captured",
        ticker=ticker,
        buffer=os.path.basename(path),
        snapshots=stats["snapshots"],
        candles=stats["candles"],
    )
    return stats


def _sleep_until_next_boundary(interval_seconds: int) -> None:
    now = time.time()
    nxt = (now // interval_seconds + 1) * interval_seconds
    delay = nxt - now
    if delay > 0:
        time.sleep(delay)


def run_loop(settings: DatalakeSettings, iterations: int) -> int:
    client = KalshiClient(
        base_url=settings.kalshi_base_url, timeout_seconds=settings.request_timeout_seconds
    )
    feed = CoinbasePriceFeed(
        product_id=settings.coinbase_product_id, timeout_seconds=settings.request_timeout_seconds
    )
    total = {"snapshots": 0, "candles": 0, "skipped": 0}
    done = 0
    log.info(
        "capture_started",
        interval=settings.capture_interval_seconds,
        iterations=iterations or "infinite",
        buffer_dir=settings.buffer_dir,
    )
    while not _shutdown:
        started = time.time()
        try:
            stats = capture_once(settings, client, feed)
        except Exception as exc:  # last-resort guard: the loop must not die
            log.error("iteration_crashed", error=str(exc)[:300])
            stats = {"snapshots": 0, "candles": 0, "skipped": 1}
        for k in total:
            total[k] += stats.get(k, 0)
        done += 1
        if iterations and done >= iterations:
            break
        if _shutdown:
            break
        # Keep cadence even if the iteration itself took a while.
        elapsed = time.time() - started
        wait = settings.capture_interval_seconds - elapsed
        if wait > 0:
            # Sleep in 1s slices so shutdown is responsive.
            end = time.time() + wait
            while time.time() < end and not _shutdown:
                time.sleep(min(1.0, end - time.time()))
    log.info("capture_finished", iterations_done=done, **total)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Capture Kalshi BTC-15m order-book snapshots + Coinbase 60s candles "
        "into the local buffer every --interval seconds."
    )
    ap.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="number of captures then exit (default: DATALAKE_CAPTURE_ITERATIONS or run forever)",
    )
    ap.add_argument("--once", action="store_true", help="capture exactly one iteration and exit")
    ap.add_argument(
        "--interval",
        type=int,
        default=None,
        help="seconds between captures (default: settings.capture_interval_seconds)",
    )
    ap.add_argument("--buffer-dir", default=None, help="override settings.buffer_dir")
    ap.add_argument("--series-ticker", default=None, help="override Kalshi series ticker")
    ap.add_argument("--product-id", default=None, help="override Coinbase product id")
    ap.add_argument("--kalshi-base-url", default=None, help="override Kalshi API base URL")
    ap.add_argument("--log-format", choices=["json", "console"], default=None)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.iterations is not None and args.iterations < 0:
        build_parser().error("--iterations must be >= 0")
    if args.interval is not None and args.interval < 10:
        build_parser().error("--interval must be >= 10 seconds")

    settings = get_settings()
    if args.iterations is not None:
        settings.capture_iterations = args.iterations
    if args.once:
        settings.capture_iterations = 1
    if args.interval is not None:
        settings.capture_interval_seconds = args.interval
    if args.buffer_dir:
        settings.buffer_dir = args.buffer_dir
    if args.series_ticker:
        settings.kalshi_series_ticker = args.series_ticker
    if args.product_id:
        settings.coinbase_product_id = args.product_id
    if args.kalshi_base_url:
        settings.kalshi_base_url = args.kalshi_base_url
    if args.log_format:
        settings.log_format = args.log_format

    configure_logging(settings.log_format, settings.log_level)
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    return run_loop(settings, settings.capture_iterations)


if __name__ == "__main__":
    raise SystemExit(main())
