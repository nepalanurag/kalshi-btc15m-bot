"""Expected-value gate for the kalshi-btc15m-bot (validation hardening).

The bot's entry checks (ask <= entry_max, spread <= spread_max, per-window
budget) control *cost*, not *edge*: nothing compares the price paid to the
signal's measured win rate. Buying a side at 59c needs a >59% hit rate to
break even, and that comparison is what this module adds.

Rule (optional pre-trade check):

    implied_prob < estimated_win_prob - margin

where ``estimated_win_prob`` is the realized hit rate for the current
regime, measured from settled windows (backtest report or paper-trading
window logs), and ``margin`` is a safety buffer for estimation error.

DEFAULT STATE: DISABLED. ``check()`` with ``enabled=False`` (the default)
always returns "allowed", so wiring this module next to the live bot
changes nothing until it is explicitly enabled, e.g. via the
``validation.ev_gate`` section of the config:

    validation:
      ev_gate:
        enabled: false        # set true to enforce the gate
        margin: 0.05          # required edge buffer
        min_n: 30             # min settled windows per regime before the gate trusts the estimate
        stats_path: backtest/reports/backtest_report.json

This is the EV gate the live bot can enable: ``check_entry`` takes the same
decision fields the bot already logs, so it can sit directly in front of
the entry loop as a pre-trade check.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Dict, Optional

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

try:
    import yaml
except ImportError:
    yaml = None

DEFAULT_MARGIN = 0.05
DEFAULT_MIN_N = 30


@dataclass
class RegimeStat:
    n: int
    hit_rate: float


@dataclass
class GateResult:
    allowed: bool
    reason: str
    regime: str
    implied_prob: float
    estimated_win_prob: Optional[float]
    margin: float


def stats_from_report(report_path: str) -> Dict[str, RegimeStat]:
    """Realized hit rate by regime from a backtest report JSON."""
    with open(report_path) as f:
        report = json.load(f)
    stats: Dict[str, RegimeStat] = {}
    for regime, r in report.get("by_regime", {}).items():
        n = int(r.get("traded_windows") or 0)
        hr = r.get("hit_rate")
        if n and hr is not None:
            stats[regime] = RegimeStat(n=n, hit_rate=float(hr))
    return stats


def stats_from_jsonl(log_path: str) -> Dict[str, RegimeStat]:
    """Realized hit rate by regime from paper-trading window logs.

    Reads settled windows from the bot's JSONL log (records with
    ``decision.regime`` and ``settlement.win``). Only windows that actually
    entered (have an ``entry``) and settled count.
    """
    wins: Dict[str, int] = {}
    total: Dict[str, int] = {}
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") != "settled":
                continue
            decision = rec.get("decision") or {}
            settlement = rec.get("settlement") or {}
            regime = decision.get("regime")
            win = settlement.get("win")
            if regime is None or win is None or not rec.get("entry"):
                continue
            total[regime] = total.get(regime, 0) + 1
            wins[regime] = wins.get(regime, 0) + (1 if win else 0)
    return {
        regime: RegimeStat(n=n, hit_rate=wins[regime] / n)
        for regime, n in total.items()
        if n > 0
    }


def check(
    implied_prob: float,
    regime: str,
    stats: Dict[str, RegimeStat],
    *,
    margin: float = DEFAULT_MARGIN,
    min_n: int = DEFAULT_MIN_N,
    enabled: bool = False,
) -> GateResult:
    """Apply the EV gate. Returns a GateResult; never raises on thin data.

    With ``enabled=False`` the gate is a pass-through: allowed=True with a
    reason saying so. Live behavior is unchanged until explicitly enabled.
    """
    if not enabled:
        return GateResult(
            allowed=True,
            reason="ev_gate disabled (pass-through); live behavior unchanged",
            regime=regime,
            implied_prob=implied_prob,
            estimated_win_prob=None,
            margin=margin,
        )
    stat = stats.get(regime)
    if stat is None or stat.n < min_n:
        have = stat.n if stat else 0
        return GateResult(
            allowed=False,
            reason=f"insufficient history for regime '{regime}': {have} settled windows < min_n={min_n}",
            regime=regime,
            implied_prob=implied_prob,
            estimated_win_prob=stat.hit_rate if stat else None,
            margin=margin,
        )
    required = stat.hit_rate - margin
    if implied_prob < required:
        return GateResult(
            allowed=True,
            reason=(f"implied {implied_prob:.3f} < hit_rate {stat.hit_rate:.3f} "
                    f"- margin {margin:.3f} (n={stat.n})"),
            regime=regime,
            implied_prob=implied_prob,
            estimated_win_prob=stat.hit_rate,
            margin=margin,
        )
    return GateResult(
        allowed=False,
        reason=(f"no edge: implied {implied_prob:.3f} >= hit_rate {stat.hit_rate:.3f} "
                f"- margin {margin:.3f} (n={stat.n})"),
        regime=regime,
        implied_prob=implied_prob,
        estimated_win_prob=stat.hit_rate,
        margin=margin,
    )


def check_entry(
    decision: Dict,
    implied_prob: float,
    stats: Dict[str, RegimeStat],
    *,
    margin: float = DEFAULT_MARGIN,
    min_n: int = DEFAULT_MIN_N,
    enabled: bool = False,
) -> GateResult:
    """Drop-in pre-trade check: ``decision`` carries the bot's regime field."""
    return check(
        implied_prob,
        str(decision.get("regime", "neutral")),
        stats,
        margin=margin,
        min_n=min_n,
        enabled=enabled,
    )


def load_gate_config(config_path: str) -> Dict:
    """Read the optional ``validation.ev_gate`` section of the bot config."""
    cfg: Dict = {"enabled": False, "margin": DEFAULT_MARGIN,
                 "min_n": DEFAULT_MIN_N, "stats_path": None}
    if not config_path or yaml is None or not os.path.exists(config_path):
        return cfg
    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}
    section = (raw.get("validation") or {}).get("ev_gate") or {}
    for key in cfg:
        if key in section:
            cfg[key] = section[key]
    return cfg


def demo() -> None:
    """Show what the gate would do on the current backtest report (not enforced)."""
    here = os.path.dirname(os.path.abspath(__file__))
    report_path = os.path.join(here, "reports", "backtest_report.json")
    if not os.path.exists(report_path):
        print("no backtest report found; run python -m backtest.backtester first")
        return
    with open(report_path) as f:
        report = json.load(f)
    stats = stats_from_report(report_path)
    print("EV gate evaluation on backtest regimes (gate ENABLED here for illustration):")
    print(f"{'regime':<9} {'n':>5} {'hit_rate':>9} {'avg_implied':>11} {'verdict':<40}")
    for regime, r in report.get("by_regime", {}).items():
        stat = stats.get(regime)
        res = check(
            implied_prob=r["avg_implied_prob"],
            regime=regime,
            stats=stats,
            margin=DEFAULT_MARGIN,
            enabled=True,
        )
        print(f"{regime:<9} {stat.n:>5} {stat.hit_rate:>9.3f} "
              f"{r['avg_implied_prob']:>11.3f} "
              f"{'ENTER' if res.allowed else 'SKIP'} ({res.reason})")


if __name__ == "__main__":
    demo()
