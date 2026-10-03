from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Any, Dict, Optional, Tuple


UTC = _dt.timezone.utc


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(tz=UTC)


def parse_iso8601(ts: str) -> _dt.datetime:
    # Kalshi timestamps are typically like "2023-11-07T05:31:56Z"
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(ts)


def isoformat_z(dt: _dt.datetime) -> str:
    dt = dt.astimezone(UTC)
    s = dt.isoformat()
    return s.replace("+00:00", "Z")


def ceil_to_cent(dollars: Decimal) -> int:
    """Return fee in cents, rounding UP to the nearest cent."""
    cents = (dollars * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_CEILING)
    return int(cents)


def fee_cents_taker(count: int, price_cents: int) -> int:
    """
    Fee model (taker):
      fee = round up(0.07 * C * P * (1-P)) to the nearest cent,
    where P is price in dollars.
    """
    P = Decimal(price_cents) / Decimal(100)
    raw = Decimal("0.07") * Decimal(count) * P * (Decimal(1) - P)
    return ceil_to_cent(raw)


def fee_cents_maker(count: int, price_cents: int) -> int:
    """
    Fee model (maker):
      fee = round up(0.0175 * C * P * (1-P)) to the nearest cent
    where P is price in dollars.
    """
    P = Decimal(price_cents) / Decimal(100)
    raw = Decimal("0.0175") * Decimal(count) * P * (Decimal(1) - P)
    return ceil_to_cent(raw)


def max_affordable_contracts(
    budget_cents: int,
    limit_price_cents: int,
    fee_mode: str = "taker",
) -> int:
    """Max contracts such that (count*price + fees) <= budget."""
    if limit_price_cents <= 0:
        return 0

    # Upper bound ignoring fees
    max_by_cost = budget_cents // limit_price_cents
    if max_by_cost <= 0:
        return 0

    def fees(c: int) -> int:
        if fee_mode == "maker":
            return fee_cents_maker(c, limit_price_cents)
        return fee_cents_taker(c, limit_price_cents)

    # Try max and walk down (small counts typically; deterministic).
    c = int(max_by_cost)
    while c > 0:
        total = c * limit_price_cents + fees(c)
        if total <= budget_cents:
            return c
        c -= 1
    return 0


def safe_int(x: Any) -> Optional[int]:
    try:
        if x is None:
            return None
        return int(x)
    except Exception:
        return None


def safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


@dataclass(frozen=True)
class OrderbookTop:
    yes_bid: int
    yes_ask: int
    no_bid: int
    no_ask: int

    @staticmethod
    def from_orderbook(orderbook: Dict[str, Any]) -> "OrderbookTop":
        # orderbook["orderbook"]["yes"] and ["no"] are arrays of [price, qty] sorted asc
        ob = orderbook.get("orderbook", orderbook)
        yes = ob.get("yes") or []
        no = ob.get("no") or []

        def best_bid(side):
            if not side:
                return 0
            # arrays sorted asc; best bid is last element
            return int(side[-1][0])

        yes_bid = best_bid(yes)
        no_bid = best_bid(no)
        yes_ask = 100 - no_bid if no_bid > 0 else 100
        no_ask = 100 - yes_bid if yes_bid > 0 else 100
        return OrderbookTop(yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask)

    def side_bid_ask_spread(self, side: str) -> Tuple[int, int, int]:
        side = side.lower()
        if side == "yes":
            bid, ask = self.yes_bid, self.yes_ask
        else:
            bid, ask = self.no_bid, self.no_ask
        return bid, ask, max(0, ask - bid)
