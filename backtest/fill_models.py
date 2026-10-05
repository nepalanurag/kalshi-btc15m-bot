"""Fill-model comparison for paper trading (validation hardening).

The bot's paper mode simulates fills deterministically against a single
live order-book snapshot (``Executor._paper_simulate_fill``): it consumes
the full displayed size at each price level up to the limit. That is the
optimistic case -- it assumes you are at the front of the queue, the book
does not move while you walk it, and resting orders are never pulled.

This module keeps that optimistic model as a reference re-implementation
(``optimistic_fill``; the live code is untouched) and adds a conservative
counterpart (``conservative_fill``) that models three things the optimistic
one assumes away:

1. Queue position. Only a share of the displayed size is realistically
   accessible to a newly placed limit order (``queue_share``); the rest
   belongs to orders ahead of you.
2. Partial fills. You get what is accessible, not what you asked for; the
   remainder goes unfilled.
3. Adverse selection. When the accessible book is thin relative to your
   size, the fill reprices against you by ``adverse_selection_cents``
   (the market moves while you take the last of the liquidity), and each
   level can disappear with probability ``level_pull_prob`` (quote flicker).

Both models run on the same ladder so paper PnL can be reported under both
assumptions side by side. ``compare_from_jsonl`` reads the bot's paper
window logs; paper logs record top-of-book only (bid/ask/spread), so depth
beyond the top is synthesized by ``ladder_from_top`` -- a stated assumption,
flagged in the output. For a real depth comparison, capture full
order-book snapshots per attempt (that capture pipeline is the planned
market-data lake under ``datalake/``).
"""

from __future__ import annotations

import json
import os
import random
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

from kalshi_btc15m_bot.util import fee_cents_taker

Ladder = List[Tuple[int, int]]  # (ask_price_cents, qty)


# --------------------------------------------------------------------------
# Ladders
# --------------------------------------------------------------------------

def ladder_from_orderbook(orderbook: Dict, side: str) -> Ladder:
    """Build the ask ladder the bot would take, mirroring the paper simulator.

    To buy YES you lift NO bids at price y, so the YES ask is 100 - y; to
    buy NO you lift YES bids at price x, so the NO ask is 100 - x. Levels
    are ordered best-ask first, exactly as ``_paper_simulate_fill`` walks
    them.
    """
    ob = orderbook.get("orderbook", orderbook)
    yes_bids = ob.get("yes") or []
    no_bids = ob.get("no") or []
    ladder: Ladder = []
    if side == "yes":
        for price_cents, qty in reversed(no_bids):
            ladder.append((100 - int(price_cents), int(qty)))
    else:
        for price_cents, qty in reversed(yes_bids):
            ladder.append((100 - int(price_cents), int(qty)))
    return ladder


def ladder_from_top(bid_cents: int, ask_cents: int, desired_count: int,
                    depth_multiple: float = 3.0) -> Ladder:
    """Synthesize a ladder from logged top-of-book (paper logs record only this).

    Assumption: a single level at the ask with ``depth_multiple`` times the
    desired size behind it. This is a placeholder depth model; it makes the
    optimistic fill trivially complete and the conservative fill partial by
    construction (queue_share < 1). Treat side-by-side PnL from JSONL as
    illustrative of the *mechanism*, not as measured depth.
    """
    if ask_cents <= 0 or desired_count <= 0:
        return []
    return [(int(ask_cents), int(max(1, round(desired_count * depth_multiple))))]


# --------------------------------------------------------------------------
# Fill models
# --------------------------------------------------------------------------

def optimistic_fill(ladder: Ladder, limit_price_cents: int,
                    desired_count: int) -> Tuple[int, int, int]:
    """Reference re-implementation of the paper fill (optimistic case).

    Consumes full displayed size at each level up to the limit, from a
    single snapshot. Mirrors ``Executor._paper_simulate_fill``; kept here
    so both models can run on identical inputs. Returns
    (filled_count, cost_cents, fee_cents) with the bot's taker-fee model
    (fee summed across levels, then rounded up -- same as the executor).
    """
    from decimal import Decimal, ROUND_CEILING

    remaining = int(desired_count)
    filled = 0
    cost = 0
    raw_fee_dollars = Decimal(0)
    for ask_price, qty in ladder:
        if remaining <= 0:
            break
        if ask_price > limit_price_cents:
            break
        take = min(remaining, qty)
        remaining -= take
        filled += take
        cost += take * ask_price
        P = Decimal(ask_price) / Decimal(100)
        raw_fee_dollars += Decimal("0.07") * Decimal(take) * P * (Decimal(1) - P)
    if filled <= 0:
        return 0, 0, 0
    fee_cents = int((raw_fee_dollars * Decimal(100)).quantize(Decimal("1"),
                                                              rounding=ROUND_CEILING))
    return filled, cost, fee_cents


def conservative_fill(
    ladder: Ladder,
    limit_price_cents: int,
    desired_count: int,
    *,
    queue_share: float = 0.5,
    adverse_selection_cents: int = 1,
    level_pull_prob: float = 0.1,
    seed: Optional[int] = None,
) -> Tuple[int, int, int]:
    """Conservative fill: queue position, partial fills, adverse selection.

    - Each level offers only ``queue_share`` of its displayed size (you are
      behind existing queue).
    - Each level disappears with probability ``level_pull_prob`` (seeded RNG
      for reproducibility).
    - If total accessible size across executable levels is less than twice
      the desired size (thin book), every filled contract costs an extra
      ``adverse_selection_cents`` -- the price moves against the taker of
      the last liquidity.

    Returns (filled_count, cost_cents, fee_cents).
    """
    rng = random.Random(seed)
    accessible: Ladder = []
    for ask_price, qty in ladder:
        if ask_price > limit_price_cents:
            break
        if rng.random() < level_pull_prob:
            continue  # level pulled before the order reaches it
        aq = int(qty * queue_share)
        if aq > 0:
            accessible.append((ask_price, aq))

    total_accessible = sum(q for _, q in accessible)
    thin_book = total_accessible < 2 * desired_count

    remaining = int(desired_count)
    filled = 0
    cost = 0
    for ask_price, qty in accessible:
        if remaining <= 0:
            break
        take = min(remaining, qty)
        remaining -= take
        filled += take
        px = ask_price + (adverse_selection_cents if thin_book else 0)
        px = max(1, min(99, px))
        cost += take * px

    if filled <= 0:
        return 0, 0, 0
    avg_price = int(round(cost / filled))
    return filled, cost, fee_cents_taker(filled, avg_price)


# --------------------------------------------------------------------------
# Side-by-side comparison
# --------------------------------------------------------------------------

def compare(trades: List[Dict], seed: int = 7) -> Dict:
    """Run both fill models over ``trades`` and report PnL side by side.

    Each trade: {"side", "limit_price_cents", "desired_count", "ladder",
    "settle_win" (bool), "label" (optional)}. PnL assumes positions held to
    settlement: win -> payoff 100c/contract, loss -> 0.
    """
    rows = []
    tot = {
        "optimistic": {"filled": 0, "cost": 0, "fees": 0, "pnl": 0, "trades": 0},
        "conservative": {"filled": 0, "cost": 0, "fees": 0, "pnl": 0, "trades": 0},
    }
    for i, t in enumerate(trades):
        ladder = t["ladder"]
        o_f, o_c, o_fee = optimistic_fill(ladder, t["limit_price_cents"], t["desired_count"])
        c_f, c_c, c_fee = conservative_fill(
            ladder, t["limit_price_cents"], t["desired_count"], seed=seed + i)
        win = bool(t.get("settle_win"))
        o_pnl = (o_f * 100 if win else 0) - o_c - o_fee
        c_pnl = (c_f * 100 if win else 0) - c_c - c_fee
        rows.append({
            "label": t.get("label", f"trade_{i}"),
            "side": t["side"],
            "desired": t["desired_count"],
            "optimistic": {"filled": o_f, "cost_cents": o_c, "fees_cents": o_fee, "pnl_cents": o_pnl},
            "conservative": {"filled": c_f, "cost_cents": c_c, "fees_cents": c_fee, "pnl_cents": c_pnl},
            "settle_win": win,
        })
        for key, f_, c_, fee_, pnl_ in (("optimistic", o_f, o_c, o_fee, o_pnl),
                                        ("conservative", c_f, c_c, c_fee, c_pnl)):
            d = tot[key]
            d["filled"] += f_
            d["cost"] += c_
            d["fees"] += fee_
            d["pnl"] += pnl_
            d["trades"] += 1

    return {"trades": rows, "totals": tot,
            "pnl_gap_cents": tot["optimistic"]["pnl"] - tot["conservative"]["pnl"]}


def trades_from_jsonl(log_path: str, depth_multiple: float = 3.0) -> List[Dict]:
    """Build comparison trades from paper window logs (top-of-book only).

    Uses each filled entry's side, limit price, and filled count as the
    desired size, with the settlement win for PnL. Depth beyond the top is
    synthesized (see ``ladder_from_top``); flagged per trade.
    """
    trades = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            entry = rec.get("entry") or {}
            decision = rec.get("decision") or {}
            settlement = rec.get("settlement") or {}
            if not entry or "win" not in settlement:
                continue
            bid = int(entry.get("bid_cents") or 0)
            ask = int(entry.get("ask_cents") or 0)
            desired = int(entry.get("filled_count") or 0)
            if ask <= 0 or desired <= 0:
                continue
            trades.append({
                "label": (decision.get("market_ticker")
                          or rec.get("timestamps", {}).get("close_time", "window")),
                "side": decision.get("side", "yes"),
                "limit_price_cents": int(entry.get("limit_price_cents") or ask),
                "desired_count": desired,
                "ladder": ladder_from_top(bid, ask, desired, depth_multiple),
                "settle_win": bool(settlement["win"]),
                "depth_note": "synthesized from top-of-book; illustrative only",
            })
    return trades


def print_comparison(result: Dict) -> None:
    tot = result["totals"]
    print(f"{'model':<13} {'trades':>6} {'filled':>7} {'cost_$':>8} "
          f"{'fees_$':>8} {'pnl_$':>9}")
    for key in ("optimistic", "conservative"):
        d = tot[key]
        print(f"{key:<13} {d['trades']:>6} {d['filled']:>7} "
              f"{d['cost'] / 100:>8.2f} {d['fees'] / 100:>8.2f} {d['pnl'] / 100:>9.2f}")
    print(f"PnL gap (optimistic - conservative): ${result['pnl_gap_cents'] / 100:.2f}")


def demo_on_backtest_trades(n: int = 200, seed: int = 7) -> None:
    """Illustrate the mechanism on backtest trades with a synthetic book.

    Backtest trades have no order book (no historical Kalshi data), so each
    trade gets a two-level synthetic ladder around its proxied entry price.
    This shows how the two models *differ in mechanism*, not a measured PnL.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    report_path = os.path.join(here, "reports", "backtest_report.json")
    if not os.path.exists(report_path):
        print("no backtest report found; run python -m backtest.backtester first")
        return
    with open(report_path) as f:
        report = json.load(f)
    trades = []
    for t in report.get("trades", [])[:n]:
        px = t["price_cents"]
        # Two-level book: some size at the proxied ask, more one cent worse.
        ladder = [(px, t["count"] * 2), (min(99, px + 1), t["count"] * 4)]
        trades.append({
            "label": t["window_start"],
            "side": t["side"],
            "limit_price_cents": px,
            "desired_count": t["count"],
            "ladder": ladder,
            "settle_win": t["win"],
        })
    print(f"fill-model comparison on {len(trades)} backtest trades "
          f"(synthetic two-level book -- mechanism illustration only):")
    print_comparison(compare(trades, seed=seed))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Optimistic vs conservative fill comparison.")
    ap.add_argument("--jsonl", default=None, help="paper window log to compare on")
    ap.add_argument("--demo", action="store_true", help="illustrate on backtest trades")
    args = ap.parse_args()
    if args.jsonl:
        trades = trades_from_jsonl(args.jsonl)
        if not trades:
            print("no settled, filled entries found in", args.jsonl)
        else:
            print(f"comparing {len(trades)} paper entries (depth synthesized from top-of-book):")
            print_comparison(compare(trades))
    elif args.demo:
        demo_on_backtest_trades()
    else:
        ap.print_help()
