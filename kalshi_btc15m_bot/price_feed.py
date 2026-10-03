from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import List, Tuple

import requests

from .util import UTC


class PriceFeedError(RuntimeError):
    pass


@dataclass
class Candle:
    ts: dt.datetime  # candle start time (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float


class CoinbasePriceFeed:
    """Public Coinbase spot + candles.

    - Spot: https://api.coinbase.com/v2/prices/BTC-USD/spot
    - Candles (Exchange API): https://api.exchange.coinbase.com/products/<product>/candles

    NOTE: Coinbase's candle endpoint returns [time, low, high, open, close, volume] in reverse-chronological order.
    """

    def __init__(self, product_id: str = "BTC-USD", timeout_seconds: float = 10.0):
        self.product_id = product_id
        self.timeout = timeout_seconds

    def get_spot(self) -> float:
        url = f"https://api.coinbase.com/v2/prices/{self.product_id}/spot"
        r = requests.get(url, timeout=self.timeout)
        if r.status_code >= 400:
            raise PriceFeedError(f"Spot request failed: {r.status_code} {r.text}")
        data = r.json()
        amt = data["data"]["amount"]
        return float(amt)

    def get_candles(
        self,
        *,
        start: dt.datetime,
        end: dt.datetime,
        granularity_seconds: int,
    ) -> List[Candle]:
        # Coinbase Exchange API expects RFC3339/ISO times
        start = start.astimezone(UTC)
        end = end.astimezone(UTC)

        url = f"https://api.exchange.coinbase.com/products/{self.product_id}/candles"
        params = {
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "granularity": int(granularity_seconds),
        }
        r = requests.get(url, params=params, timeout=self.timeout)
        if r.status_code >= 400:
            raise PriceFeedError(f"Candles request failed: {r.status_code} {r.text}")
        raw = r.json()
        candles: List[Candle] = []
        for row in raw:
            # [time, low, high, open, close, volume]
            t, low, high, opn, cls, vol = row
            ts = dt.datetime.fromtimestamp(int(t), tz=UTC)
            candles.append(Candle(ts=ts, open=float(opn), high=float(high), low=float(low), close=float(cls), volume=float(vol)))

        # Sort ascending by timestamp (oldest -> newest)
        candles.sort(key=lambda c: c.ts)
        return candles
