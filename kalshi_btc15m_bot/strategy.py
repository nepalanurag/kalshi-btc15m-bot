from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .util import safe_float


@dataclass(frozen=True)
class MarketCandidate:
    ticker: str
    strike: Optional[float]
    raw: Dict[str, Any]


def extract_strike(market: Dict[str, Any]) -> Optional[float]:
    # Prefer numeric strike fields when present
    floor = safe_float(market.get("floor_strike"))
    cap = safe_float(market.get("cap_strike"))
    strike_type = (market.get("strike_type") or "").lower()

    if floor is not None and cap is not None:
        if floor == cap:
            return float(floor)
        if strike_type in {"between", "range", "bounded"}:
            return float((floor + cap) / 2.0)
        # Default deterministic choice: use floor
        return float(floor)

    if floor is not None:
        return float(floor)
    if cap is not None:
        return float(cap)

    # Fallback: parse from title/subtitle
    text = " ".join([str(market.get("title") or ""), str(market.get("subtitle") or "")])
    m = re.search(r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?)", text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except Exception:
            return None
    return None


def rsi_regime(rsi: float, bullish: float, bearish: float) -> str:
    if rsi > bullish:
        return "bullish"
    if rsi < bearish:
        return "bearish"
    return "neutral"


def choose_market(
    markets: List[Dict[str, Any]],
    spot: float,
    rsi: float,
    bullish_th: float,
    bearish_th: float,
) -> MarketCandidate:
    # Build candidates with strikes (possibly None)
    cands: List[MarketCandidate] = []
    for m in markets:
        ticker = str(m.get("ticker") or m.get("market_ticker") or "")
        if not ticker:
            continue
        cands.append(MarketCandidate(ticker=ticker, strike=extract_strike(m), raw=m))

    # Deterministic fallback: lowest ticker lexicographically
    cands.sort(key=lambda c: c.ticker)

    if not cands:
        raise ValueError("No markets in close group.")

    regime = rsi_regime(rsi, bullish_th, bearish_th)

    # Filter to those with strikes
    with_strike = [c for c in cands if c.strike is not None]
    if not with_strike:
        return cands[0]

    if regime == "bullish":
        above = [c for c in with_strike if c.strike is not None and c.strike >= spot]
        if above:
            above.sort(key=lambda c: (abs(c.strike - spot), c.ticker))
            return above[0]
        # If no strike >= spot, choose the highest strike
        with_strike.sort(key=lambda c: (-(c.strike or 0.0), c.ticker))
        return with_strike[0]

    if regime == "bearish":
        below = [c for c in with_strike if c.strike is not None and c.strike <= spot]
        if below:
            below.sort(key=lambda c: (abs(c.strike - spot), c.ticker))
            return below[0]
        # If no strike <= spot, choose the lowest strike
        with_strike.sort(key=lambda c: ((c.strike or 0.0), c.ticker))
        return with_strike[0]

    # Neutral: closest strike
    with_strike.sort(key=lambda c: (abs((c.strike or 0.0) - spot), c.ticker))
    return with_strike[0]


def choose_side(
    spot: float,
    rsi: float,
    bullish_th: float,
    bearish_th: float,
    strike: Optional[float],
    neutral_fallback_side: str,
) -> str:
    regime = rsi_regime(rsi, bullish_th, bearish_th)
    if regime == "bullish":
        return "yes"
    if regime == "bearish":
        return "no"

    # Neutral
    if strike is None:
        return neutral_fallback_side.lower()
    return "yes" if spot >= strike else "no"
