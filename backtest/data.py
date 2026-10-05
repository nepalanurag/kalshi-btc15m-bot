"""Historical candle data for the backtester.

Fetches 60-second BTC-USD candles from Coinbase's public Exchange API and
keeps a local CSV cache under ``backtest/data/`` so reruns don't hit the
network. The cache directory is gitignored; the reports under
``backtest/reports/`` record which date range they were built from.

Coinbase returns at most 300 candles per request, so long ranges are fetched
in chunks with a small pause between requests.
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import time
from typing import List, Tuple

import requests

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

MAX_CANDLES_PER_REQUEST = 300
REQUEST_PAUSE_SECONDS = 0.25


def _cache_path(product_id: str, start: dt.datetime, end: dt.datetime) -> str:
    slug = product_id.replace("-", "").lower()
    s = start.strftime("%Y%m%d")
    e = end.strftime("%Y%m%d")
    return os.path.join(DATA_DIR, f"{slug}_60s_{s}_{e}.csv")


def fetch_candles(
    product_id: str,
    start: dt.datetime,
    end: dt.datetime,
    granularity_seconds: int = 60,
    use_cache: bool = True,
    session: "requests.Session | None" = None,
) -> List[Tuple[dt.datetime, float, float, float, float, float]]:
    """Return (ts, open, high, low, close, volume) tuples, oldest first.

    Uses the on-disk cache when ``use_cache`` is True and a covering file
    exists; otherwise fetches from Coinbase and writes the cache.
    """
    start = start.astimezone(dt.timezone.utc)
    end = end.astimezone(dt.timezone.utc)

    path = _cache_path(product_id, start, end)
    if use_cache and os.path.exists(path):
        return _read_cache(path)

    sess = session or requests.Session()
    chunk = dt.timedelta(seconds=MAX_CANDLES_PER_REQUEST * granularity_seconds)
    out: List[Tuple[dt.datetime, float, float, float, float, float]] = []

    cur = start
    while cur < end:
        nxt = min(cur + chunk, end)
        params = {
            "start": cur.isoformat().replace("+00:00", "Z"),
            "end": nxt.isoformat().replace("+00:00", "Z"),
            "granularity": int(granularity_seconds),
        }
        url = f"https://api.exchange.coinbase.com/products/{product_id}/candles"
        r = sess.get(url, params=params, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Coinbase candles request failed: {r.status_code} {r.text[:200]}")
        for row in r.json():
            # Coinbase row: [time, low, high, open, close, volume]
            t, low, high, opn, cls, vol = row
            ts = dt.datetime.fromtimestamp(int(t), tz=dt.timezone.utc)
            out.append((ts, float(opn), float(high), float(low), float(cls), float(vol)))
        time.sleep(REQUEST_PAUSE_SECONDS)
        cur = nxt

    out.sort(key=lambda c: c[0])
    _write_cache(path, out)
    return out


def _write_cache(path: str, candles) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for ts, o, h, l, c, v in candles:
            w.writerow([ts.isoformat(), o, h, l, c, v])


def _read_cache(path: str):
    out = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            ts = dt.datetime.fromisoformat(row["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            out.append(
                (
                    ts,
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    float(row["volume"]),
                )
            )
    return out
