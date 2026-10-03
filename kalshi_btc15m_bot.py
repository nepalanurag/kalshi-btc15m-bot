
"""
kalshi_btc15m_bot.py

Deterministic trading bot for Kalshi BTC 15-minute markets (e.g., series KXBTC15M).

Key rules:
- Exactly one decision per 15-minute close group, computed at window start (15 minutes pre-close).
- At most one filled BUY total per window; hold to settlement; no flipping; no early exit.
- Paper and live modes.

References:
- Kalshi authenticated request signing: timestamp + HTTP_METHOD + path (without query params),
  RSA-PSS SHA256, base64-encoded signature. See docs.
- Orderbook bid/ask reciprocity: only bids returned; asks derived from opposite side bids.

This script is provided as-is; test in demo & paper mode before live use.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as dt
import json
import logging
import math
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# ----------------------------
# Utilities
# ----------------------------

UTC = dt.timezone.utc


def utcnow() -> dt.datetime:
    return dt.datetime.now(tz=UTC)


def parse_iso8601_z(ts: str) -> dt.datetime:
    """Parse ISO8601 timestamps like '2023-11-07T05:31:56Z' into tz-aware datetime (UTC)."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return dt.datetime.fromisoformat(ts)


def clamp_int(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, x))


def sleep_with_heartbeat(total_seconds: float, heartbeat_fn, step_seconds: float = 5.0) -> None:
    """
    Sleep in small increments so we can do periodic work (e.g., settlement checks).
    """
    end = time.time() + max(0.0, total_seconds)
    while True:
        now = time.time()
        if now >= end:
            return
        heartbeat_fn()
        time.sleep(min(step_seconds, end - now))


# ----------------------------
# Config
# ----------------------------

@dataclasses.dataclass(frozen=True)
class BotConfig:
    # Kalshi
    series_ticker: str = "KXBTC15M"
    env: str = "prod"  # "demo" or "prod"
    base_url: str = "https://api.elections.kalshi.com"  # demo: https://demo-api.kalshi.co
    api_key_id: Optional[str] = None
    private_key_path: Optional[str] = None
    subaccount: int = 0  # 0 for primary

    # Mode
    mode: str = "paper"  # "paper" or "live"

    # Window timing
    window_seconds: int = 15 * 60
    min_seconds_to_close: int = 45  # do not enter if below this threshold
    window_start_grace_seconds: int = 300  # allow slight lateness at start

    # Market discovery
    # How far ahead (in seconds) to search for upcoming close times in the configured series.
    # Must be large enough to reliably include the next 1-2 close groups.
    discovery_horizon_seconds: int = 6 * 60 * 60

    # Indicators
    candle_interval_seconds: int = 60
    rsi_period: int = 14
    rsi_bull_threshold: float = 60.0
    rsi_bear_threshold: float = 40.0
    vol_lookback_minutes: int = 90  # for volatility estimate

    # Volatility model
    vol_min_sigma_1m: float = 0.0  # floor in log-return space (1m)
    drift_per_min: float = 0.0  # deterministic drift in log-return space per minute (usually 0)
    use_ev_filter: bool = True
    min_edge_cents: float = 1.0  # require at least this "edge" vs ask (in cents) to attempt

    # Entry constraints
    entry_max_cents: int = 60
    spread_max_cents: int = 10
    min_best_bid_cents: int = 1  # require bid >= 1 to post inside spread
    min_best_ask_cents: int = 1  # require ask >= 1

    # Budget
    usd_budget: float = 10.0  # max spend per window including fees
    max_contracts_per_window: int = 5000  # additional hard cap

    # Fees (Kalshi fee model coefficients; update if exchange changes)
    fee_rate_taker: float = 0.07
    fee_rate_maker: float = 0.0175  # conservative: some markets may have 0 maker fees

    # Execution
    poll_interval_seconds: float = 1.0
    fill_wait_seconds: float = 1.5
    max_price_walk_ticks: int = 20  # max times we step price upward per window
    default_neutral_side: str = "yes"  # if strike is unavailable

    # Logging/DB
    db_path: str = "kalshi_btc15m_bot.sqlite"
    http_timeout_seconds: float = 10.0

    # BTC data source
    # "auto" is recommended; it tries multiple public data sources and sticks to the first that works.
    btc_provider: str = "auto"  # "auto", "coinbase", "kraken", "bitstamp", "coingecko", "binance"

    @staticmethod
    def from_json(path: str) -> "BotConfig":
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # Allow overriding base_url automatically by env if not explicitly provided
        env = raw.get("env", "prod")
        base_url = raw.get("base_url")
        if not base_url:
            base_url = "https://demo-api.kalshi.co" if env == "demo" else "https://api.elections.kalshi.com"
        raw["env"] = env
        raw["base_url"] = base_url
        return BotConfig(**raw)


# ----------------------------
# BTC data provider (public exchange data)
# ----------------------------

class BTCDataProvider:
    def get_spot(self) -> float:
        raise NotImplementedError

    def get_recent_ohlc(self, interval_seconds: int, lookback_minutes: int) -> List[Dict[str, float]]:
        """
        Return list of candles, each: {"open":..., "high":..., "low":..., "close":..., "ts":...}
        Ordered from oldest -> newest.
        """
        raise NotImplementedError


class BinanceBTCProvider(BTCDataProvider):
    """
    Uses Binance public endpoints:
    - Spot: /api/v3/ticker/price?symbol=BTCUSDT
    - Candles: /api/v3/klines?symbol=BTCUSDT&interval=1m&limit=...
    """

    def __init__(self, timeout: float = 10.0):
        self._session = requests.Session()
        self._timeout = timeout

    def get_spot(self) -> float:
        url = "https://api.binance.com/api/v3/ticker/price"
        r = self._session.get(url, params={"symbol": "BTCUSDT"}, timeout=self._timeout)
        r.raise_for_status()
        data = r.json()
        return float(data["price"])

    def get_recent_ohlc(self, interval_seconds: int, lookback_minutes: int) -> List[Dict[str, float]]:
        # Binance intervals are discrete strings
        interval_map = {
            60: "1m",
            180: "3m",
            300: "5m",
            900: "15m",
        }
        interval = interval_map.get(interval_seconds)
        if not interval:
            raise ValueError(f"Unsupported interval_seconds for Binance: {interval_seconds}")

        limit = min(1000, max(30, lookback_minutes * 60 // interval_seconds + 5))
        url = "https://api.binance.com/api/v3/klines"
        r = self._session.get(
            url,
            params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
            timeout=self._timeout,
        )
        r.raise_for_status()
        klines = r.json()
        candles: List[Dict[str, float]] = []
        for k in klines:
            # [ open time, open, high, low, close, volume, close time, ... ]
            candles.append(
                {
                    "ts": float(k[0]) / 1000.0,
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                }
            )
        candles.sort(key=lambda c: c["ts"])
        return candles


class CoinbaseBTCProvider(BTCDataProvider):
    """Coinbase Exchange (public) BTC-USD ticker + candles."""

    def __init__(self, timeout: float = 10.0):
        self._session = requests.Session()
        self._timeout = timeout
        self._base = "https://api.exchange.coinbase.com"

    def get_spot(self) -> float:
        r = self._session.get(f"{self._base}/products/BTC-USD/ticker", timeout=self._timeout)
        r.raise_for_status()
        data = r.json()
        return float(data["price"])

    def get_recent_ohlc(self, interval_seconds: int, lookback_minutes: int) -> List[Dict[str, float]]:
        if interval_seconds not in (60, 300, 900, 3600):
            raise ValueError(f"Unsupported interval_seconds for Coinbase: {interval_seconds}")

        # Coinbase limit is typically 300 candles per request.
        limit = min(300, max(30, lookback_minutes * 60 // interval_seconds + 5))
        params = {"granularity": int(interval_seconds)}
        r = self._session.get(f"{self._base}/products/BTC-USD/candles", params=params, timeout=self._timeout)
        r.raise_for_status()
        arr = r.json()  # [[time, low, high, open, close, volume], ...]

        # Coinbase returns newest-first.
        candles: List[Dict[str, float]] = []
        for row in arr[:limit]:
            ts, low, high, open_, close, _vol = row
            candles.append(
                {
                    "ts": float(ts),
                    "open": float(open_),
                    "high": float(high),
                    "low": float(low),
                    "close": float(close),
                }
            )
        candles.sort(key=lambda c: c["ts"])
        return candles


class KrakenBTCProvider(BTCDataProvider):
    """Kraken public XBTUSD ticker + OHLC."""

    def __init__(self, timeout: float = 10.0):
        self._session = requests.Session()
        self._timeout = timeout
        self._base = "https://api.kraken.com/0/public"

    def get_spot(self) -> float:
        r = self._session.get(f"{self._base}/Ticker", params={"pair": "XBTUSD"}, timeout=self._timeout)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(f"Kraken error: {data['error']}")
        result = data.get("result") or {}
        # Result key varies (e.g., XXBTZUSD)
        pair_key = next(iter(result.keys()))
        last = result[pair_key]["c"][0]
        return float(last)

    def get_recent_ohlc(self, interval_seconds: int, lookback_minutes: int) -> List[Dict[str, float]]:
        if interval_seconds % 60 != 0:
            raise ValueError(f"Unsupported interval_seconds for Kraken: {interval_seconds}")
        interval_min = int(interval_seconds // 60)
        # Kraken interval must be one of allowed values; common ones include 1,5,15,60.
        if interval_min not in (1, 5, 15, 60, 240, 1440):
            raise ValueError(f"Unsupported interval_minutes for Kraken: {interval_min}")

        limit = max(30, lookback_minutes // interval_min + 5)
        r = self._session.get(
            f"{self._base}/OHLC",
            params={"pair": "XBTUSD", "interval": interval_min},
            timeout=self._timeout,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(f"Kraken error: {data['error']}")
        result = data.get("result") or {}
        pair_key = next(k for k in result.keys() if k != "last")
        rows = result[pair_key]
        candles: List[Dict[str, float]] = []
        for row in rows[-limit:]:
            # [time, open, high, low, close, vwap, volume, count]
            ts, open_, high, low, close, *_ = row
            candles.append(
                {
                    "ts": float(ts),
                    "open": float(open_),
                    "high": float(high),
                    "low": float(low),
                    "close": float(close),
                }
            )
        candles.sort(key=lambda c: c["ts"])
        return candles


class BitstampBTCProvider(BTCDataProvider):
    """Bitstamp public BTCUSD ticker + OHLC."""

    def __init__(self, timeout: float = 10.0):
        self._session = requests.Session()
        self._timeout = timeout
        self._base = "https://www.bitstamp.net/api/v2"

    def get_spot(self) -> float:
        r = self._session.get(f"{self._base}/ticker/btcusd/", timeout=self._timeout)
        r.raise_for_status()
        data = r.json()
        return float(data["last"])

    def get_recent_ohlc(self, interval_seconds: int, lookback_minutes: int) -> List[Dict[str, float]]:
        if interval_seconds not in (60, 180, 300, 900, 3600):
            raise ValueError(f"Unsupported interval_seconds for Bitstamp: {interval_seconds}")
        limit = max(30, lookback_minutes * 60 // interval_seconds + 5)
        params = {"step": int(interval_seconds), "limit": int(limit)}
        r = self._session.get(f"{self._base}/ohlc/btcusd/", params=params, timeout=self._timeout)
        r.raise_for_status()
        data = r.json()
        rows = (data.get("data") or {}).get("ohlc") or []
        candles: List[Dict[str, float]] = []
        for row in rows:
            candles.append(
                {
                    "ts": float(row["timestamp"]),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                }
            )
        candles.sort(key=lambda c: c["ts"])
        return candles


class CoinGeckoBTCProvider(BTCDataProvider):
    """CoinGecko public spot + OHLC (typically 5-minute granularity)."""

    def __init__(self, timeout: float = 10.0):
        self._session = requests.Session()
        self._timeout = timeout
        self._base = "https://api.coingecko.com/api/v3"

    def get_spot(self) -> float:
        r = self._session.get(
            f"{self._base}/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd"},
            timeout=self._timeout,
        )
        r.raise_for_status()
        data = r.json()
        return float(data["bitcoin"]["usd"])

    def get_recent_ohlc(self, interval_seconds: int, lookback_minutes: int) -> List[Dict[str, float]]:
        # CoinGecko free OHLC endpoint returns fixed buckets (often 5m).
        if interval_seconds != 300:
            raise ValueError("CoinGecko OHLC supports 300s (5m) only in this bot")
        # days=1 gives last day, bucketed.
        r = self._session.get(
            f"{self._base}/coins/bitcoin/ohlc",
            params={"vs_currency": "usd", "days": 1},
            timeout=self._timeout,
        )
        r.raise_for_status()
        arr = r.json()  # [[ts_ms, open, high, low, close], ...]
        candles: List[Dict[str, float]] = []
        for ts_ms, open_, high, low, close in arr:
            candles.append(
                {
                    "ts": float(ts_ms) / 1000.0,
                    "open": float(open_),
                    "high": float(high),
                    "low": float(low),
                    "close": float(close),
                }
            )
        # Keep only the last N candles needed.
        need = max(30, lookback_minutes * 60 // interval_seconds + 5)
        candles.sort(key=lambda c: c["ts"])
        return candles[-need:]


class AutoBTCProvider(BTCDataProvider):
    """Try multiple BTC providers and stick to the first one that works."""

    def __init__(self, providers: List[Tuple[str, BTCDataProvider]]):
        if not providers:
            raise ValueError("AutoBTCProvider requires at least one provider")
        self._providers = providers
        self._active_idx = 0

    @property
    def active_name(self) -> str:
        return self._providers[self._active_idx][0]

    def _try(self, fn_name: str, fn, *args, **kwargs):
        last_err: Optional[Exception] = None
        # First try active provider, then rotate.
        for offset in range(len(self._providers)):
            idx = (self._active_idx + offset) % len(self._providers)
            name, prov = self._providers[idx]
            try:
                out = fn(prov, *args, **kwargs)
                self._active_idx = idx
                return out
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError(f"All BTC providers failed for {fn_name}. Last error: {last_err}")

    def get_spot(self) -> float:
        return float(self._try("get_spot", lambda p: p.get_spot()))

    def get_recent_ohlc(self, interval_seconds: int, lookback_minutes: int) -> List[Dict[str, float]]:
        return list(
            self._try(
                "get_recent_ohlc",
                lambda p, i, l: p.get_recent_ohlc(i, l),
                interval_seconds,
                lookback_minutes,
            )
        )


def build_btc_provider(cfg: BotConfig) -> BTCDataProvider:
    name = (cfg.btc_provider or "auto").lower().strip()
    timeout = cfg.http_timeout_seconds

    if name == "binance":
        return BinanceBTCProvider(timeout=timeout)
    if name == "coinbase":
        return CoinbaseBTCProvider(timeout=timeout)
    if name == "kraken":
        return KrakenBTCProvider(timeout=timeout)
    if name == "bitstamp":
        return BitstampBTCProvider(timeout=timeout)
    if name == "coingecko":
        return CoinGeckoBTCProvider(timeout=timeout)
    if name == "auto":
        # Order matters: prefer providers with broad access + good 1m candles.
        provs: List[Tuple[str, BTCDataProvider]] = [
            ("coinbase", CoinbaseBTCProvider(timeout=timeout)),
            ("kraken", KrakenBTCProvider(timeout=timeout)),
            ("bitstamp", BitstampBTCProvider(timeout=timeout)),
            ("coingecko", CoinGeckoBTCProvider(timeout=timeout)),
            ("binance", BinanceBTCProvider(timeout=timeout)),
        ]
        return AutoBTCProvider(provs)

    raise ValueError(f"Unknown btc_provider: {cfg.btc_provider}")


# ----------------------------
# Indicators
# ----------------------------

def compute_rsi_wilder(closes: List[float], period: int) -> Optional[float]:
    """
    Standard RSI (Wilder's smoothing).
    Returns None if insufficient data.
    """
    if period <= 0:
        raise ValueError("RSI period must be positive")
    if len(closes) < period + 1:
        return None

    gains = []
    losses = []
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def compute_log_return_sigma_per_min(closes: List[float]) -> Optional[float]:
    """
    Compute stdev of 1-step log returns for the provided close series.
    Returns sigma (log-return stdev) per candle step.
    """
    if len(closes) < 3:
        return None
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0 or closes[i] <= 0:
            continue
        rets.append(math.log(closes[i] / closes[i - 1]))
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var)


def compute_atr(candles: List[Dict[str, float]], period: int) -> Optional[float]:
    """Average True Range over last period candles; returns ATR in price units."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    trs = trs[-period:]
    return sum(trs) / period if trs else None


# Normal CDF without scipy
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def estimate_probability_yes(
    *,
    strike_type: Optional[str],
    floor_strike: Optional[float],
    cap_strike: Optional[float],
    spot: float,
    sigma_1m: float,
    minutes_to_close: float,
    mu_per_min: float,
) -> Optional[float]:
    """
    Estimate P(YES) with a lognormal model:
      ln(S_T / S_0) ~ N(mu*T, (sigma_1m*sqrt(T))^2)
    Returns None if we can't model the strike.
    """
    if spot <= 0 or minutes_to_close <= 0:
        return None
    if not strike_type:
        return None

    # Safeguards against 0 volatility
    sigma_T = max(1e-12, sigma_1m * math.sqrt(minutes_to_close))
    mu_T = mu_per_min * minutes_to_close

    def z_for_level(level: float) -> float:
        if level <= 0:
            return float("inf")
        return (math.log(level / spot) - mu_T) / sigma_T

    st = strike_type.lower()

    # Note: Kalshi uses 'strike_type' values like 'greater', 'between', etc.
    if st in ("greater", "greater_or_equal"):
        if floor_strike is None:
            return None
        z = z_for_level(float(floor_strike))
        return 1.0 - norm_cdf(z)

    if st in ("less", "less_or_equal"):
        # For "less", Kalshi often uses cap_strike (max expiration value yielding YES).
        level = cap_strike if cap_strike is not None else floor_strike
        if level is None:
            return None
        z = z_for_level(float(level))
        return norm_cdf(z)

    if st == "between":
        if floor_strike is None or cap_strike is None:
            return None
        z_lo = z_for_level(float(floor_strike))
        z_hi = z_for_level(float(cap_strike))
        return max(0.0, min(1.0, norm_cdf(z_hi) - norm_cdf(z_lo)))

    # Unknown / custom / structured
    return None


# ----------------------------
# Kalshi API client
# ----------------------------

class KalshiSigner:
    def __init__(self, api_key_id: str, private_key_path: str):
        self.api_key_id = api_key_id
        with open(private_key_path, "rb") as f:
            # cryptography no longer requires an explicit backend argument.
            self.private_key = serialization.load_pem_private_key(f.read(), password=None)

    def sign(self, timestamp_ms: str, method: str, path: str) -> str:
        # Strip query params per docs
        path_no_q = path.split("?")[0]
        message = f"{timestamp_ms}{method.upper()}{path_no_q}".encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")


class KalshiClient:
    def __init__(self, base_url: str, timeout: float = 10.0, signer: Optional[KalshiSigner] = None, subaccount: int = 0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.signer = signer
        self.subaccount = subaccount
        self.session = requests.Session()

    def _headers(self, method: str, path: str, auth: bool) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if auth:
            if not self.signer:
                raise RuntimeError("Authenticated request attempted without signer")
            ts = str(int(time.time() * 1000))
            sig = self.signer.sign(ts, method, path)
            headers.update(
                {
                    "KALSHI-ACCESS-KEY": self.signer.api_key_id,
                    "KALSHI-ACCESS-SIGNATURE": sig,
                    "KALSHI-ACCESS-TIMESTAMP": ts,
                }
            )
        return headers

    def get(self, path: str, params: Optional[Dict[str, Any]] = None, auth: bool = False) -> Dict[str, Any]:
        url = self.base_url + path
        r = self.session.get(url, params=params, headers=self._headers("GET", path, auth), timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, payload: Dict[str, Any], auth: bool = True) -> Dict[str, Any]:
        url = self.base_url + path
        headers = self._headers("POST", path, auth)
        headers["Content-Type"] = "application/json"
        r = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def delete(self, path: str, params: Optional[Dict[str, Any]] = None, auth: bool = True) -> Dict[str, Any]:
        url = self.base_url + path
        r = self.session.delete(url, params=params, headers=self._headers("DELETE", path, auth), timeout=self.timeout)
        r.raise_for_status()
        # Cancel order returns JSON; some DELETE endpoints may return empty
        if r.text.strip():
            return r.json()
        return {}

    # ---- Domain helpers ----

    def list_markets_by_close_range(
        self,
        *,
        series_ticker: str,
        min_close_ts: int,
        max_close_ts: int,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """List markets in a series whose close_time falls within [min_close_ts, max_close_ts].

        Uses: GET /trade-api/v2/markets?series_ticker=...&min_close_ts=...&max_close_ts=...

        Per Kalshi docs, min_close_ts/max_close_ts are compatible with an *empty* status filter.
        So we do NOT pass status here; that allows unopened/open/paused markets to be returned.
        """

        markets: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {
                "series_ticker": series_ticker,
                "min_close_ts": int(min_close_ts),
                "max_close_ts": int(max_close_ts),
                "limit": int(limit),
            }
            if cursor:
                params["cursor"] = cursor
            data = self.get("/trade-api/v2/markets", params=params, auth=False)
            batch = data.get("markets", [])
            markets.extend(batch)
            cursor = data.get("cursor") or data.get("next_cursor")
            if not cursor:
                break
        return markets

    def list_open_markets_in_series(self, series_ticker: str, limit: int = 1000) -> List[Dict[str, Any]]:
        """
        Fetch markets for a series. Uses cursor pagination if present.
        Endpoint: GET /trade-api/v2/markets?series_ticker=...&status=open
        """
        markets: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            params = {"series_ticker": series_ticker, "status": "open", "limit": limit}
            if cursor:
                params["cursor"] = cursor
            data = self.get("/trade-api/v2/markets", params=params, auth=False)
            batch = data.get("markets", [])
            markets.extend(batch)
            cursor = data.get("cursor") or data.get("next_cursor")  # be defensive
            if not cursor:
                break
        return markets

    def get_market(self, ticker: str) -> Dict[str, Any]:
        data = self.get(f"/trade-api/v2/markets/{ticker}", auth=False)
        return data["market"]

    def get_orderbook(self, ticker: str) -> Dict[str, Any]:
        # No authentication required per docs; we'll keep auth=False.
        return self.get(f"/trade-api/v2/markets/{ticker}/orderbook", auth=False)

    # ---- Orders (auth) ----

    def create_limit_buy(
        self,
        *,
        ticker: str,
        side: str,
        count: int,
        price_cents: int,
        client_order_id: str,
        post_only: bool,
        buy_max_cost_cents: Optional[int],
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "type": "limit",
            "count": int(count),
            "client_order_id": client_order_id,
        }
        if side == "yes":
            payload["yes_price"] = int(price_cents)
        else:
            payload["no_price"] = int(price_cents)
        if post_only:
            payload["post_only"] = True
        if buy_max_cost_cents is not None:
            payload["buy_max_cost"] = int(buy_max_cost_cents)
        if self.subaccount:
            payload["subaccount"] = int(self.subaccount)

        return self.post("/trade-api/v2/portfolio/orders", payload, auth=True)

    def get_order(self, order_id: str) -> Dict[str, Any]:
        return self.get(f"/trade-api/v2/portfolio/orders/{order_id}", auth=True)

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        return self.delete(f"/trade-api/v2/portfolio/orders/{order_id}", params={"subaccount": self.subaccount}, auth=True)


# ----------------------------
# Fee model (pre-trade estimate)
# ----------------------------

def kalshi_fee_cents(price_cents: int, count: int, fee_rate: float) -> int:
    """
    Fee model:
      fees = round_up_to_cent( fee_rate * C * P * (1 - P) )
    where:
      C = contracts
      P = price in dollars (0..1)
    We return integer cents.

    NOTE: This is a pre-trade estimate. Live fills can report actual maker/taker fees on the order object.
    """
    p = price_cents / 100.0
    fee_dollars = fee_rate * float(count) * p * (1.0 - p)
    fee_cents = int(math.ceil(fee_dollars * 100.0 - 1e-12))
    return max(0, fee_cents)


def max_affordable_count(
    *,
    budget_cents: int,
    price_cents: int,
    fee_rate: float,
    hard_cap: int,
) -> int:
    if price_cents <= 0:
        return 0
    # Upper bound by cost only
    upper = min(hard_cap, budget_cents // price_cents if price_cents else 0)
    if upper <= 0:
        return 0
    # Search downward; budgets here are small enough that O(upper) is OK.
    for c in range(upper, 0, -1):
        total = c * price_cents + kalshi_fee_cents(price_cents, c, fee_rate)
        if total <= budget_cents:
            return c
    return 0


# ----------------------------
# Strike extraction / selection
# ----------------------------

@dataclasses.dataclass(frozen=True)
class StrikeInfo:
    strike_type: Optional[str]
    floor: Optional[float]
    cap: Optional[float]
    mid: Optional[float]
    desc: str


def extract_strike_info(m: Dict[str, Any]) -> StrikeInfo:
    st = m.get("strike_type")
    floor = m.get("floor_strike")
    cap = m.get("cap_strike")
    mid: Optional[float] = None
    desc = ""

    try:
        floor_f = float(floor) if floor is not None else None
    except Exception:
        floor_f = None
    try:
        cap_f = float(cap) if cap is not None else None
    except Exception:
        cap_f = None

    if floor_f is not None and cap_f is not None:
        mid = 0.5 * (floor_f + cap_f)
        desc = f"[{floor_f}, {cap_f}]"
    elif floor_f is not None:
        mid = floor_f
        desc = f"{floor_f}"
    elif cap_f is not None:
        mid = cap_f
        desc = f"{cap_f}"
    else:
        # Try functional_strike as last resort
        fs = m.get("functional_strike")
        if isinstance(fs, str) and fs.strip():
            desc = fs.strip()
            # Do not force parse; could be non-numeric

    return StrikeInfo(strike_type=st, floor=floor_f, cap=cap_f, mid=mid, desc=desc)


def classify_regime(rsi: Optional[float], bull: float, bear: float) -> str:
    if rsi is None:
        return "neutral"
    if rsi >= bull:
        return "bullish"
    if rsi <= bear:
        return "bearish"
    return "neutral"


def select_market_from_close_group(
    close_group: List[Dict[str, Any]],
    *,
    spot: float,
    regime: str,
) -> Tuple[Dict[str, Any], StrikeInfo]:
    """
    Select market according to user spec:
    - bullish: prefer strikes >= spot, closest
    - bearish: prefer strikes <= spot, closest
    - neutral: closest to spot
    If strike mid is missing for all, pick lexicographically smallest ticker (deterministic).
    """
    enriched: List[Tuple[Dict[str, Any], StrikeInfo]] = [(m, extract_strike_info(m)) for m in close_group]

    with_strike = [(m, s) for (m, s) in enriched if s.mid is not None]
    if not with_strike:
        chosen = sorted(close_group, key=lambda x: x.get("ticker", ""))[0]
        return chosen, extract_strike_info(chosen)

    # Deterministic tie-breaker by (distance, strike_mid, ticker)
    def key_neutral(item):
        m, s = item
        return (abs(float(s.mid) - spot), float(s.mid), m.get("ticker", ""))

    def key_bull(item):
        m, s = item
        above = float(s.mid) >= spot
        dist = (float(s.mid) - spot) if above else float("inf")
        return (0 if above else 1, dist, float(s.mid), m.get("ticker", ""))

    def key_bear(item):
        m, s = item
        below = float(s.mid) <= spot
        dist = (spot - float(s.mid)) if below else float("inf")
        return (0 if below else 1, dist, float(s.mid), m.get("ticker", ""))

    if regime == "bearish":
        chosen = min(with_strike, key=key_bull)
    elif regime == "bullish":
        chosen = min(with_strike, key=key_bear)
    else:
        chosen = min(with_strike, key=key_neutral)
    return chosen[0], chosen[1]


def select_side_once(
    *,
    regime: str,
    rsi: Optional[float],
    spot: float,
    strike_mid: Optional[float],
    cfg: BotConfig,
) -> str:
    if regime == "bullish":
        return "yes"
    if regime == "bearish":
        return "no"
    # neutral
    if strike_mid is None:
        return cfg.default_neutral_side
    return "no" if spot >= float(strike_mid) else "yes"


# ----------------------------
# DB + logging
# ----------------------------

class BotDB:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS windows (
                window_id TEXT PRIMARY KEY,
                series_ticker TEXT,
                window_start_ts INTEGER,
                close_ts INTEGER,
                created_ts INTEGER,
                updated_ts INTEGER,

                chosen_ticker TEXT,
                strike_desc TEXT,
                strike_mid REAL,
                strike_type TEXT,
                floor_strike REAL,
                cap_strike REAL,

                spot REAL,
                rsi REAL,
                sigma_1m REAL,
                expected_move_1sigma REAL,
                atr REAL,
                regime TEXT,
                chosen_side TEXT,

                -- entry snapshot at attempt that filled (or last attempt)
                entry_bid INTEGER,
                entry_ask INTEGER,
                entry_spread INTEGER,
                entry_limit_price INTEGER,
                entry_count INTEGER,
                entry_fee_est INTEGER,
                entry_total_est INTEGER,

                -- live fill details
                order_id TEXT,
                filled_count INTEGER,
                fill_cost INTEGER,
                fees_paid INTEGER,
                total_spent INTEGER,
                fill_avg_price REAL,
                attempts INTEGER,
                time_to_close_at_fill REAL,

                fill_status TEXT,  -- "filled", "no_fill", "skipped"
                skip_reason TEXT,
                notes TEXT,

                settlement_result TEXT,
                win INTEGER,
                payoff INTEGER,
                realized_pnl INTEGER,
                cumulative_pnl INTEGER
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS state (
                k TEXT PRIMARY KEY,
                v TEXT
            )
            """
        )
        self.conn.commit()

    def get_state_int(self, key: str, default: int = 0) -> int:
        cur = self.conn.execute("SELECT v FROM state WHERE k=?", (key,))
        row = cur.fetchone()
        if not row:
            return default
        try:
            return int(row[0])
        except Exception:
            return default

    def set_state_int(self, key: str, value: int) -> None:
        self.conn.execute("INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(int(value))))
        self.conn.commit()

    def window_exists(self, window_id: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM windows WHERE window_id=?", (window_id,))
        return cur.fetchone() is not None

    def upsert_window(self, window_id: str, fields: Dict[str, Any]) -> None:
        # Build dynamic UPSERT statement
        cols = ["window_id"] + list(fields.keys())
        placeholders = ",".join(["?"] * len(cols))
        updates = ",".join([f"{c}=excluded.{c}" for c in fields.keys()])
        values = [window_id] + list(fields.values())
        sql = f"INSERT INTO windows ({','.join(cols)}) VALUES ({placeholders}) ON CONFLICT(window_id) DO UPDATE SET {updates}"
        self.conn.execute(sql, values)
        self.conn.commit()

    def fetch_unsettled_filled_windows(self) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            """
            SELECT window_id, chosen_ticker, chosen_side, filled_count, total_spent, close_ts, strike_type, floor_strike, cap_strike
            FROM windows
            WHERE fill_status='filled' AND (settlement_result IS NULL OR settlement_result='')
            """
        )
        rows = cur.fetchall()
        keys = ["window_id", "chosen_ticker", "chosen_side", "filled_count", "total_spent", "close_ts", "strike_type", "floor_strike", "cap_strike"]
        return [dict(zip(keys, r)) for r in rows]

    def close(self) -> None:
        self.conn.close()


# ----------------------------
# Core bot
# ----------------------------

class BTC15mKalshiBot:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.db = BotDB(cfg.db_path)

        signer = None
        if cfg.mode == "live":
            if not cfg.api_key_id or not cfg.private_key_path:
                raise ValueError("Live mode requires api_key_id and private_key_path in config.")
            signer = KalshiSigner(cfg.api_key_id, cfg.private_key_path)

        self.kalshi = KalshiClient(cfg.base_url, timeout=cfg.http_timeout_seconds, signer=signer, subaccount=cfg.subaccount)
        self.btc = build_btc_provider(cfg)

        # cumulative pnl in cents
        self.cum_pnl_cents = self.db.get_state_int("cumulative_pnl_cents", default=0)

    # ---- Market discovery ----

    def get_next_close_group(self) -> Optional[Tuple[dt.datetime, List[Dict[str, Any]]]]:
        """Find the next upcoming close-time group for the configured series.

        Important: BTC 15m markets are often `unopened` until near the window start.
        Kalshi's Get Markets timestamp filters (min_close_ts/max_close_ts) require leaving
        `status` empty, so we query by close-time range without a status filter and then
        select the earliest future close_time that we have NOT already processed/logged.
        """

        now = utcnow()
        now_ts = int(now.timestamp())
        max_ts = now_ts + int(self.cfg.discovery_horizon_seconds)

        try:
            markets = self.kalshi.list_markets_by_close_range(
                series_ticker=self.cfg.series_ticker,
                min_close_ts=now_ts,
                max_close_ts=max_ts,
                limit=1000,
            )
        except Exception as e:
            logging.warning("Market discovery failed (series=%s): %s", self.cfg.series_ticker, e)
            return None

        # Group by close_ts (seconds) for exact matching.
        groups: Dict[int, List[Dict[str, Any]]] = {}
        for m in markets:
            ct = m.get("close_time")
            if not ct:
                continue
            try:
                close_dt = parse_iso8601_z(ct)
            except Exception:
                continue
            close_ts = int(close_dt.timestamp())
            if close_ts <= now_ts:
                continue
            groups.setdefault(close_ts, []).append(m)

        if not groups:
            return None

        for close_ts in sorted(groups.keys()):
            window_id = f"{self.cfg.series_ticker}:{close_ts}"
            if self.db.window_exists(window_id):
                continue
            close_dt = dt.datetime.fromtimestamp(close_ts, tz=UTC)
            return close_dt, groups[close_ts]

        return None

    # ---- Settlement updates ----

    def settle_open_trades(self) -> None:
        # Only uses public market data; safe in both paper and live.
        pending = self.db.fetch_unsettled_filled_windows()
        if not pending:
            return

        for row in pending:
            ticker = row["chosen_ticker"]
            if not ticker:
                continue
            try:
                market = self.kalshi.get_market(ticker)
            except Exception as e:
                logging.warning("Settlement check failed for %s: %s", ticker, e)
                continue

            result = market.get("result")  # e.g., "yes", "no", "void", "scalar"
            if not result:
                continue

            filled_count = int(row["filled_count"] or 0)
            total_spent = int(row["total_spent"] or 0)
            side = row["chosen_side"]

            payoff = 0
            win: Optional[int] = None
            realized = None

            if result in ("yes", "no"):
                win = 1 if result == side else 0
                payoff = 100 * filled_count if win else 0
                realized = payoff - total_spent
            elif result == "void":
                # Best-effort: voided markets return positions at original cost; fees may or may not be refunded.
                # Without portfolio-level settlement data, assume cost refunded but fees not.
                # If you want exact behavior, augment with /portfolio/settlements.
                payoff = max(0, total_spent)  # conservative placeholder
                realized = 0
                win = None
            else:
                # Scalar or other outcomes: not expected for binary BTC markets, but handle gracefully.
                payoff = 0
                realized = None

            updated = int(time.time())
            fields: Dict[str, Any] = {
                "updated_ts": updated,
                "settlement_result": result,
                "win": win,
                "payoff": payoff,
            }
            if realized is not None:
                self.cum_pnl_cents += realized
                self.db.set_state_int("cumulative_pnl_cents", self.cum_pnl_cents)
                fields["realized_pnl"] = realized
                fields["cumulative_pnl"] = self.cum_pnl_cents

            self.db.upsert_window(row["window_id"], fields)
            logging.info("Settled %s result=%s realized=%s cum=%s", ticker, result, realized, self.cum_pnl_cents)

    # ---- Window processing ----

    def process_next_window_forever(self) -> None:
        logging.info("Starting bot. mode=%s series=%s base_url=%s", self.cfg.mode, self.cfg.series_ticker, self.cfg.base_url)
        while True:
            self.settle_open_trades()

            nxt = self.get_next_close_group()
            if not nxt:
                logging.warning("No upcoming close group found. Sleeping...")
                sleep_with_heartbeat(10.0, self.settle_open_trades, step_seconds=5.0)
                continue

            close_dt, group = nxt
            window_start = close_dt - dt.timedelta(seconds=self.cfg.window_seconds)
            now = utcnow()

            window_id = f"{self.cfg.series_ticker}:{int(close_dt.timestamp())}"
            if self.db.window_exists(window_id):
                # Already processed; re-run discovery shortly.
                sleep_with_heartbeat(0.5, self.settle_open_trades, step_seconds=0.5)
                continue

            if now < window_start:
                sleep_secs = (window_start - now).total_seconds()
                logging.info("Next window %s starts in %.1fs (close=%s)", window_id, sleep_secs, close_dt.isoformat())
                sleep_with_heartbeat(sleep_secs, self.settle_open_trades, step_seconds=5.0)
                continue

            # If we missed window start by too much, skip (deterministic rule: compute signals at start)
            lateness = (now - window_start).total_seconds()
            if lateness > self.cfg.window_start_grace_seconds:
                logging.warning("Missed window start for %s by %.1fs; skipping window for determinism.", window_id, lateness)
                self.db.upsert_window(
                    window_id,
                    {
                        "series_ticker": self.cfg.series_ticker,
                        "window_start_ts": int(window_start.timestamp()),
                        "close_ts": int(close_dt.timestamp()),
                        "created_ts": int(time.time()),
                        "updated_ts": int(time.time()),
                        "fill_status": "skipped",
                        "skip_reason": f"started_late_by_{lateness:.1f}s",
                        "cumulative_pnl": self.cum_pnl_cents,
                    },
                )
                # Do NOT sleep until close: the next 15m window starts immediately at this close.
                # Instead, loop and discover the next unprocessed close group.
                sleep_with_heartbeat(0.5, self.settle_open_trades, step_seconds=0.5)
                continue

            # Process this window
            self.run_window(window_id, window_start, close_dt, group)

            # After window close, short pause then loop.
            sleep_with_heartbeat(1.0, self.settle_open_trades, step_seconds=1.0)

    def run_window(self, window_id: str, window_start: dt.datetime, close_dt: dt.datetime, group: List[Dict[str, Any]]) -> None:
        cfg = self.cfg
        created_ts = int(time.time())
        logging.info("Window start %s (close=%s, markets=%d)", window_id, close_dt.isoformat(), len(group))

        # --- Lock signals once at window start ---
        try:
            spot = self.btc.get_spot()
        except Exception as e:
            logging.error("Failed to fetch BTC spot; skipping window. err=%s", e)
            self.db.upsert_window(
                window_id,
                {
                    "series_ticker": cfg.series_ticker,
                    "window_start_ts": int(window_start.timestamp()),
                    "close_ts": int(close_dt.timestamp()),
                    "created_ts": created_ts,
                    "updated_ts": int(time.time()),
                    "fill_status": "skipped",
                    "skip_reason": "spot_fetch_failed",
                    "notes": str(e),
                    "cumulative_pnl": self.cum_pnl_cents,
                },
            )
            return

        lookback = max(cfg.vol_lookback_minutes, cfg.rsi_period * 2 + 5)
        try:
            candles = self.btc.get_recent_ohlc(cfg.candle_interval_seconds, lookback_minutes=lookback)
        except Exception as e:
            logging.error("Failed to fetch BTC candles; skipping window. err=%s", e)
            self.db.upsert_window(
                window_id,
                {
                    "series_ticker": cfg.series_ticker,
                    "window_start_ts": int(window_start.timestamp()),
                    "close_ts": int(close_dt.timestamp()),
                    "created_ts": created_ts,
                    "updated_ts": int(time.time()),
                    "fill_status": "skipped",
                    "skip_reason": "history_fetch_failed",
                    "notes": str(e),
                    "spot": spot,
                    "cumulative_pnl": self.cum_pnl_cents,
                },
            )
            return

        closes = [c["close"] for c in candles]
        rsi = compute_rsi_wilder(closes, cfg.rsi_period)
        sigma_1m = compute_log_return_sigma_per_min(closes)
        if sigma_1m is None:
            sigma_1m = 0.0
        sigma_1m = max(cfg.vol_min_sigma_1m, float(sigma_1m))
        atr = compute_atr(candles, period=min(14, max(2, cfg.rsi_period)))

        minutes_to_close = cfg.window_seconds / 60.0
        expected_move_1sigma = abs(spot) * sigma_1m * math.sqrt(minutes_to_close)

        regime = classify_regime(rsi, cfg.rsi_bull_threshold, cfg.rsi_bear_threshold)
        chosen_market, strike = select_market_from_close_group(group, spot=spot, regime=regime)
        chosen_ticker = chosen_market["ticker"]
        chosen_side = select_side_once(regime=regime, rsi=rsi, spot=spot, strike_mid=strike.mid, cfg=cfg)

        # Write initial window record
        self.db.upsert_window(
            window_id,
            {
                "series_ticker": cfg.series_ticker,
                "window_start_ts": int(window_start.timestamp()),
                "close_ts": int(close_dt.timestamp()),
                "created_ts": created_ts,
                "updated_ts": int(time.time()),
                "chosen_ticker": chosen_ticker,
                "strike_desc": strike.desc,
                "strike_mid": strike.mid,
                "strike_type": strike.strike_type,
                "floor_strike": strike.floor,
                "cap_strike": strike.cap,
                "spot": spot,
                "rsi": rsi,
                "sigma_1m": sigma_1m,
                "expected_move_1sigma": expected_move_1sigma,
                "atr": atr,
                "regime": regime,
                "chosen_side": chosen_side,
                "fill_status": "",  # updated later
                "cumulative_pnl": self.cum_pnl_cents,
            },
        )

        # --- Entry attempts until cutoff ---
        budget_cents = int(round(cfg.usd_budget * 100))
        cutoff_dt = close_dt - dt.timedelta(seconds=cfg.min_seconds_to_close)

        attempts = 0
        walked_ticks = 0
        last_skip_reason: Optional[str] = None

        # For deterministic price walking: keep track of our last posted limit
        last_limit_price: Optional[int] = None

        while utcnow() < cutoff_dt:
            # Always keep settlement processing responsive
            self.settle_open_trades()

            now = utcnow()
            ttc = (close_dt - now).total_seconds()
            if ttc < cfg.min_seconds_to_close:
                break

            # Pull latest market snapshot
            try:
                market = self.kalshi.get_market(chosen_ticker)
            except Exception as e:
                last_skip_reason = "market_fetch_failed"
                logging.warning("Market fetch failed %s: %s", chosen_ticker, e)
                time.sleep(cfg.poll_interval_seconds)
                continue

            tick_size = int(market.get("tick_size") or 1)
            tick_size = max(1, tick_size)

            if chosen_side == "yes":
                bid = market.get("yes_bid")
                ask = market.get("yes_ask")
            else:
                bid = market.get("no_bid")
                ask = market.get("no_ask")

            # Basic liquidity checks
            if bid is None or ask is None:
                last_skip_reason = "missing_bid_or_ask"
                time.sleep(cfg.poll_interval_seconds)
                continue

            bid = int(bid)
            ask = int(ask)
            if bid < cfg.min_best_bid_cents or ask < cfg.min_best_ask_cents:
                last_skip_reason = "bid_or_ask_too_low"
                time.sleep(cfg.poll_interval_seconds)
                continue

            spread = ask - bid
            if spread < 0:
                # stale/inconsistent snapshot
                last_skip_reason = "negative_spread_snapshot"
                time.sleep(cfg.poll_interval_seconds)
                continue

            if ask > cfg.entry_max_cents:
                last_skip_reason = "ask_above_entry_max"
                time.sleep(cfg.poll_interval_seconds)
                continue

            if spread > cfg.spread_max_cents:
                last_skip_reason = "spread_too_wide"
                time.sleep(cfg.poll_interval_seconds)
                continue

            # Optional EV filter: compare model P(YES) to ask price
            if cfg.use_ev_filter:
                p_yes = estimate_probability_yes(
                    strike_type=strike.strike_type,
                    floor_strike=strike.floor,
                    cap_strike=strike.cap,
                    spot=spot,
                    sigma_1m=sigma_1m,
                    minutes_to_close=minutes_to_close,
                    mu_per_min=cfg.drift_per_min,
                )
                if p_yes is not None:
                    if chosen_side == "yes":
                        edge = p_yes * 100.0 - float(ask)
                    else:
                        edge = (1.0 - p_yes) * 100.0 - float(ask)
                    if edge < cfg.min_edge_cents:
                        last_skip_reason = f"edge_too_small({edge:.2f}c)"
                        time.sleep(cfg.poll_interval_seconds)
                        continue

            # Determine next limit price (walk up deterministically)
            if last_limit_price is None:
                if bid + tick_size <= ask:
                    limit_price = bid + tick_size
                else:
                    limit_price = ask  # crossed already
            else:
                # Walk upward toward ask, but never above ask or entry_max
                limit_price = min(ask, last_limit_price + tick_size)
                walked_ticks += 1

            limit_price = clamp_int(limit_price, 1, cfg.entry_max_cents)

            if walked_ticks > cfg.max_price_walk_ticks:
                last_skip_reason = "max_price_walk_reached"
                break

            # Determine fee rate based on whether this order is marketable at current snapshot.
            # If limit >= ask, it will likely be taker; otherwise maker/post-only.
            is_marketable = limit_price >= ask
            fee_rate = cfg.fee_rate_taker if is_marketable else cfg.fee_rate_maker

            count = max_affordable_count(
                budget_cents=budget_cents,
                price_cents=limit_price,
                fee_rate=fee_rate,
                hard_cap=cfg.max_contracts_per_window,
            )
            if count <= 0:
                last_skip_reason = "budget_cannot_afford_1_contract"
                time.sleep(cfg.poll_interval_seconds)
                continue

            fee_est = kalshi_fee_cents(limit_price, count, fee_rate)
            total_est = count * limit_price + fee_est
            if total_est > budget_cents:
                # Shouldn't happen due to max_affordable_count, but guard anyway.
                last_skip_reason = "budget_check_failed"
                time.sleep(cfg.poll_interval_seconds)
                continue

            post_only = not is_marketable
            buy_max_cost = budget_cents  # extra safety rail

            # Update latest snapshot fields (so even no-fill has the last attempt's context)
            self.db.upsert_window(
                window_id,
                {
                    "updated_ts": int(time.time()),
                    "entry_bid": bid,
                    "entry_ask": ask,
                    "entry_spread": spread,
                    "entry_limit_price": limit_price,
                    "entry_count": count,
                    "entry_fee_est": fee_est,
                    "entry_total_est": total_est,
                    "attempts": attempts,
                },
            )

            attempts += 1
            last_limit_price = limit_price

            if cfg.mode == "paper":
                filled = self._paper_attempt_fill(
                    window_id=window_id,
                    close_dt=close_dt,
                    bid=bid,
                    ask=ask,
                    spread=spread,
                    limit_price=limit_price,
                    count=count,
                    fee_rate=fee_rate,
                    is_marketable=is_marketable,
                )
                if filled:
                    return
                time.sleep(cfg.poll_interval_seconds)
                continue

            # Live mode
            try:
                client_order_id = f"{window_id}:{attempts}:{chosen_side}:{uuid.uuid4().hex[:8]}"
                resp = self.kalshi.create_limit_buy(
                    ticker=chosen_ticker,
                    side=chosen_side,
                    count=count,
                    price_cents=limit_price,
                    client_order_id=client_order_id,
                    post_only=post_only,
                    buy_max_cost_cents=buy_max_cost,
                )
                order = resp.get("order") or resp.get("data", {}).get("order")
                if not order:
                    raise RuntimeError(f"Unexpected create_order response: {resp}")
                order_id = order["order_id"]
            except Exception as e:
                last_skip_reason = "order_create_failed"
                logging.warning("Order create failed: %s", e)
                time.sleep(cfg.poll_interval_seconds)
                continue

            # Wait briefly, then check status
            time.sleep(cfg.fill_wait_seconds)

            try:
                o = self.kalshi.get_order(order_id).get("order")
                if not o:
                    raise RuntimeError("Missing order in get_order response")
            except Exception as e:
                last_skip_reason = "order_status_failed"
                logging.warning("Order status fetch failed %s: %s", order_id, e)
                # Best effort cancel
                try:
                    self.kalshi.cancel_order(order_id)
                except Exception:
                    pass
                time.sleep(cfg.poll_interval_seconds)
                continue

            fill_count = int(o.get("fill_count") or 0)
            if fill_count > 0:
                # Enforce one-filled-entry rule: cancel any remainder immediately
                try:
                    self.kalshi.cancel_order(order_id)
                except Exception:
                    pass

                taker_fees = int(o.get("taker_fees") or 0)
                maker_fees = int(o.get("maker_fees") or 0)
                taker_cost = int(o.get("taker_fill_cost") or 0)
                maker_cost = int(o.get("maker_fill_cost") or 0)
                fees_paid = taker_fees + maker_fees
                fill_cost = taker_cost + maker_cost
                total_spent = fill_cost + fees_paid
                avg_price = (fill_cost / fill_count) if fill_count else None

                time_to_close = (close_dt - utcnow()).total_seconds()

                self.db.upsert_window(
                    window_id,
                    {
                        "updated_ts": int(time.time()),
                        "fill_status": "filled",
                        "order_id": order_id,
                        "filled_count": fill_count,
                        "fill_cost": fill_cost,
                        "fees_paid": fees_paid,
                        "total_spent": total_spent,
                        "fill_avg_price": avg_price,
                        "attempts": attempts,
                        "time_to_close_at_fill": time_to_close,
                        "skip_reason": None,
                        "cumulative_pnl": self.cum_pnl_cents,
                    },
                )
                logging.info("FILLED %s side=%s count=%d avg_price=%.2f total_spent=$%.2f ttc=%.1fs",
                             chosen_ticker, chosen_side, fill_count,
                             avg_price if avg_price is not None else float("nan"),
                             total_spent / 100.0, time_to_close)
                return

            # Not filled -> cancel and retry
            try:
                self.kalshi.cancel_order(order_id)
            except Exception:
                pass

            time.sleep(cfg.poll_interval_seconds)

        # No fill before cutoff
        status = "no_fill" if attempts > 0 else "skipped"
        reason = last_skip_reason or ("cutoff_reached" if attempts > 0 else "no_attempts")
        self.db.upsert_window(
            window_id,
            {
                "updated_ts": int(time.time()),
                "fill_status": status,
                "skip_reason": reason,
                "attempts": attempts,
                "cumulative_pnl": self.cum_pnl_cents,
            },
        )
        logging.info("Window done %s status=%s reason=%s attempts=%d", window_id, status, reason, attempts)

    def _paper_attempt_fill(
        self,
        *,
        window_id: str,
        close_dt: dt.datetime,
        bid: int,
        ask: int,
        spread: int,
        limit_price: int,
        count: int,
        fee_rate: float,
        is_marketable: bool,
    ) -> bool:
        """
        Deterministic paper fill simulation:
        - If limit_price >= current ask: immediate fill at ask (taker).
        - Otherwise: check again after fill_wait_seconds; if new ask <= limit_price then fill.
        """
        cfg = self.cfg
        chosen_ticker = self.db.conn.execute("SELECT chosen_ticker FROM windows WHERE window_id=?", (window_id,)).fetchone()[0]
        chosen_side = self.db.conn.execute("SELECT chosen_side FROM windows WHERE window_id=?", (window_id,)).fetchone()[0]

        def do_fill(fill_price: int) -> bool:
            # Fees estimate using the fee_rate determined at attempt time
            fees = kalshi_fee_cents(fill_price, count, fee_rate)
            fill_cost = fill_price * count
            total_spent = fill_cost + fees
            time_to_close = (close_dt - utcnow()).total_seconds()

            self.db.upsert_window(
                window_id,
                {
                    "updated_ts": int(time.time()),
                    "fill_status": "filled",
                    "order_id": f"paper:{uuid.uuid4().hex}",
                    "filled_count": count,
                    "fill_cost": fill_cost,
                    "fees_paid": fees,
                    "total_spent": total_spent,
                    "fill_avg_price": float(fill_price),
                    "time_to_close_at_fill": time_to_close,
                    "cumulative_pnl": self.cum_pnl_cents,
                },
            )
            logging.info("PAPER FILLED %s side=%s count=%d fill_price=%dc total_spent=$%.2f ttc=%.1fs",
                         chosen_ticker, chosen_side, count, fill_price, total_spent / 100.0, time_to_close)
            return True

        if limit_price >= ask:
            return do_fill(fill_price=ask)

        # Maker-ish attempt: check after a brief wait
        time.sleep(cfg.fill_wait_seconds)
        try:
            market = self.kalshi.get_market(chosen_ticker)
            new_ask = int(market.get("yes_ask") if chosen_side == "yes" else market.get("no_ask"))
        except Exception:
            return False

        if new_ask <= limit_price:
            return do_fill(fill_price=new_ask)
        return False


# ----------------------------
# CLI
# ----------------------------

def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def main() -> None:
    configure_logging()

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="Path to JSON config file")
    args = ap.parse_args()

    cfg = BotConfig.from_json(args.config)

    # Ensure base_url is consistent with env if user didn’t override
    if cfg.env == "demo" and "demo-api.kalshi.co" not in cfg.base_url:
        logging.warning("env=demo but base_url=%s; expected https://demo-api.kalshi.co", cfg.base_url)

    bot = BTC15mKalshiBot(cfg)
    bot.process_next_window_forever()


if __name__ == "__main__":
    main()
