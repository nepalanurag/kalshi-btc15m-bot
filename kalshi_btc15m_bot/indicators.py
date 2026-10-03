from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from .price_feed import Candle


def compute_rsi(closes: List[float], period: int) -> Optional[float]:
    """Standard RSI (Wilder) computed on closing prices."""
    if period <= 0 or len(closes) < period + 1:
        return None

    # Price changes
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    # Wilder smoothing
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return float(rsi)


def compute_atr_proxy(candles: List[Candle], period: int) -> Optional[float]:
    """ATR-like proxy from candles (true range with Wilder-style initial mean, but simple mean here).

    Returns ATR in *price units* (USD for BTC-USD).
    """
    if period <= 0 or len(candles) < period + 1:
        return None

    trs: List[float] = []
    prev_close = candles[0].close
    for c in candles[1:]:
        tr = max(
            c.high - c.low,
            abs(c.high - prev_close),
            abs(c.low - prev_close),
        )
        trs.append(tr)
        prev_close = c.close

    if len(trs) < period:
        return None

    # Use the most recent `period` TR values
    recent = trs[-period:]
    return sum(recent) / period


@dataclass(frozen=True)
class VolEstimate:
    atr: float
    expected_range: float


def estimate_expected_range(
    candles: List[Candle],
    atr_period: int,
    candle_seconds: int,
    window_minutes: int,
) -> Optional[VolEstimate]:
    atr = compute_atr_proxy(candles, atr_period)
    if atr is None:
        return None
    window_seconds = window_minutes * 60
    if candle_seconds <= 0:
        return None
    multiplier = window_seconds / candle_seconds
    expected_range = atr * multiplier
    return VolEstimate(atr=float(atr), expected_range=float(expected_range))
