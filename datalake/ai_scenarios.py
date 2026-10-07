"""Adversarial order-book scenarios + backtest stress tests.

``generate`` writes adversarial scenario JSONs to ``datalake/scenarios/``.
``stress`` runs the backtester (candle scenarios) and the fill models
(book scenarios) against them and reports the drawdown distribution.

Scenario kinds:

* ``candles`` -- synthetic 60s candle regimes fed to
  ``backtest.backtester.run_backtest``: baseline (control), volatility_shock,
  flash_crash, trend_day, trading_halt (a data gap the harness must survive).
* ``book`` -- adversarial order-book ladders fed to
  ``backtest.fill_models.optimistic_fill`` vs ``conservative_fill``:
  thin_book, wide_spread, plus a normal_book control.

The template generator is deterministic (seeded); ``generate --llm`` asks an
LLM to propose *additional* scenarios in the same JSON schema, which are
schema-checked before being written (generator: "llm").

Run:

    python -m datalake.ai_scenarios generate
    python -m datalake.ai_scenarios stress --seeds 5
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import random
import statistics
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datalake import DATA_CONTRACT_VERSION
from datalake.config import get_settings
from datalake.logging import configure_logging, get_logger

UTC = dt.timezone.utc
log = get_logger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
from backtest.backtester import BacktestParams, run_backtest  # noqa: E402
from backtest.fill_models import conservative_fill, optimistic_fill  # noqa: E402

BASE_PER_MIN_SIGMA = 0.0008  # ~3% daily vol for BTC, per-minute log-return sigma
SCENARIO_EPOCH = dt.datetime(2026, 1, 5, tzinfo=UTC)  # fixed Monday; reproducible


# --------------------------------------------------------------------------
# Scenario definitions
# --------------------------------------------------------------------------


def template_scenarios(seed: int) -> List[Dict[str, Any]]:
    now = dt.datetime.now(UTC).isoformat()
    base = {
        "seed": seed,
        "generated_at": now,
        "contract_version": DATA_CONTRACT_VERSION,
        "generator": "template",
    }
    return [
        {
            **base,
            "name": "baseline",
            "kind": "candles",
            "description": "Control: 7 days of GBM candles, no drift, normal volatility.",
            "candle_params": {
                "days": 7,
                "start_price": 85000.0,
                "mu_per_min": 0.0,
                "sigma_mult": 1.0,
                "shock": None,
                "jump": None,
                "gap": None,
            },
        },
        {
            **base,
            "name": "volatility_shock",
            "kind": "candles",
            "description": "Volatility triples for a 2-hour window mid-week; "
            "tests whether the edge survives a vol regime change.",
            "candle_params": {
                "days": 7,
                "start_price": 85000.0,
                "mu_per_min": 0.0,
                "sigma_mult": 1.0,
                "shock": {"start_min": 3 * 1440 + 600, "len_min": 120, "sigma_mult": 3.0},
                "jump": None,
                "gap": None,
            },
        },
        {
            **base,
            "name": "flash_crash",
            "kind": "candles",
            "description": "A single -5% candle (exchange glitch / liquidation cascade); "
            "tests settlement against a discontinuous move.",
            "candle_params": {
                "days": 7,
                "start_price": 85000.0,
                "mu_per_min": 0.0,
                "sigma_mult": 1.0,
                "shock": None,
                "jump": {"at_min": 2 * 1440 + 720, "log_jump": -0.0513},
                "gap": None,
            },
        },
        {
            **base,
            "name": "trend_day",
            "kind": "candles",
            "description": "Persistent +1%/day drift; the RSI rule should ride it -- "
            "tests regime attribution under trend.",
            "candle_params": {
                "days": 7,
                "start_price": 85000.0,
                "mu_per_min": 0.01 / 1440,
                "sigma_mult": 1.0,
                "shock": None,
                "jump": None,
                "gap": None,
            },
        },
        {
            **base,
            "name": "trading_halt",
            "kind": "candles",
            "description": "30-minute data gap (feed outage); the harness must skip "
            "windows cleanly, not crash or hallucinate fills.",
            "candle_params": {
                "days": 7,
                "start_price": 85000.0,
                "mu_per_min": 0.0,
                "sigma_mult": 1.0,
                "shock": None,
                "jump": None,
                "gap": {"start_min": 4 * 1440, "len_min": 30},
            },
        },
        {
            **base,
            "name": "normal_book",
            "kind": "book",
            "description": "Control book: 5c-wide top, 8 levels, healthy size.",
            "book_params": {
                "ladder": [
                    [48, 40],
                    [49, 60],
                    [50, 80],
                    [51, 60],
                    [52, 40],
                    [53, 30],
                    [54, 20],
                    [55, 10],
                ],
                "desired_count": 10,
                "limit_price_cents": 60,
            },
        },
        {
            **base,
            "name": "thin_book",
            "kind": "book",
            "description": "Three levels, 1-2 contracts each: partial fills and "
            "adverse selection should bite the conservative model.",
            "book_params": {
                "ladder": [[52, 1], [53, 1], [54, 2]],
                "desired_count": 10,
                "limit_price_cents": 60,
            },
        },
        {
            **base,
            "name": "wide_spread",
            "kind": "book",
            "description": "30c spread (40 bid / 70 ask): nothing is executable under "
            "a 60c limit; both models must report zero fills, not errors.",
            "book_params": {
                "ladder": [[70, 50], [71, 50], [72, 50]],
                "desired_count": 10,
                "limit_price_cents": 60,
            },
        },
    ]


def validate_scenario(spec: Dict[str, Any]) -> List[str]:
    """Return a list of problems (empty = valid)."""
    problems = []
    for key in ("name", "kind", "description", "contract_version"):
        if not spec.get(key):
            problems.append(f"missing {key}")
    if spec.get("kind") == "candles":
        cp = spec.get("candle_params") or {}
        for key in ("days", "start_price", "mu_per_min", "sigma_mult"):
            if key not in cp:
                problems.append(f"candle_params missing {key}")
        if not (1 <= int(cp.get("days", 0)) <= 30):
            problems.append("candle_params.days must be 1..30")
    elif spec.get("kind") == "book":
        bp = spec.get("book_params") or {}
        ladder = bp.get("ladder")
        if not isinstance(ladder, list) or not ladder:
            problems.append("book_params.ladder must be a non-empty list")
        else:
            for lvl in ladder:
                if (
                    not isinstance(lvl, (list, tuple))
                    or len(lvl) != 2
                    or not (0 < lvl[0] < 100)
                    or lvl[1] <= 0
                ):
                    problems.append(f"bad ladder level {lvl!r}")
                    break
    else:
        problems.append(f"unknown kind {spec.get('kind')!r}")
    return problems


# --------------------------------------------------------------------------
# Synthesis
# --------------------------------------------------------------------------


def synth_candles(params: Dict[str, Any], seed: int) -> List[Tuple]:
    rng = random.Random(seed)
    days = int(params["days"])
    n_min = days * 1440
    price = float(params["start_price"])
    mu = float(params["mu_per_min"])
    base_sigma = BASE_PER_MIN_SIGMA * float(params["sigma_mult"])
    shock = params.get("shock")
    jump = params.get("jump")
    gap = params.get("gap")
    out: List[Tuple] = []
    for m in range(n_min):
        if gap and gap["start_min"] <= m < gap["start_min"] + gap["len_min"]:
            continue
        sigma = base_sigma
        if shock and shock["start_min"] <= m < shock["start_min"] + shock["len_min"]:
            sigma = base_sigma * float(shock["sigma_mult"])
        r = rng.gauss(mu, sigma)
        if jump and m == int(jump["at_min"]):
            r += float(jump["log_jump"])
        o = price
        c = price * math.exp(r)
        wick = abs(rng.gauss(0.0, sigma * 0.4))
        h = max(o, c) * math.exp(wick)
        low = min(o, c) * math.exp(-wick)
        v = max(0.0, rng.gauss(1.0, 0.3))
        ts = SCENARIO_EPOCH + dt.timedelta(seconds=60 * m)
        out.append((ts, o, h, low, c, v))
        price = c
    return out


# --------------------------------------------------------------------------
# Stress
# --------------------------------------------------------------------------


def _dist(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"n": 0, "mean": None, "p5": None, "p95": None, "max": None}
    s = sorted(values)
    # 'inclusive' keeps quantiles inside the observed range (with few seeds,
    # the default exclusive method extrapolates past the max).
    q = statistics.quantiles(s, n=100, method="inclusive") if len(s) > 1 else [s[0], s[0]]
    return {
        "n": len(s),
        "mean": round(statistics.fmean(s), 2),
        "p5": round(q[4], 2),
        "p95": round(q[94], 2),
        "max": round(s[-1], 2),
    }


def stress_candle_scenario(spec: Dict[str, Any], seeds: int, base_seed: int) -> Dict[str, Any]:
    cp = spec["candle_params"]
    days = int(cp["days"])
    start = SCENARIO_EPOCH
    end = SCENARIO_EPOCH + dt.timedelta(days=days)
    pnls, dds, hit_rates, traded, skipped = [], [], [], [], []
    for s in range(seeds):
        candles = synth_candles(cp, base_seed + s)
        rep = run_backtest(candles, BacktestParams(), start, end, collect_trades=False)
        pnls.append(rep["total_pnl_dollars"])
        dds.append(rep["max_drawdown_dollars"])
        traded.append(rep["windows_traded"])
        skipped.append(sum(rep["skipped"].values()))
        wins = sum(r["traded_windows"] * (r["hit_rate"] or 0) for r in rep["by_regime"].values())
        n = sum(r["traded_windows"] for r in rep["by_regime"].values())
        hit_rates.append(round(wins / n, 4) if n else None)
    hr = [h for h in hit_rates if h is not None]
    return {
        "name": spec["name"],
        "kind": "candles",
        "seeds": seeds,
        "pnl_dollars": _dist(pnls),
        "drawdown_dollars": _dist(dds),
        "hit_rate": {"n": len(hr), "mean": round(statistics.fmean(hr), 4) if hr else None},
        "windows_traded_mean": round(statistics.fmean(traded), 1),
        "windows_skipped_mean": round(statistics.fmean(skipped), 1),
    }


def stress_book_scenario(spec: Dict[str, Any], seeds: int, base_seed: int) -> Dict[str, Any]:
    bp = spec["book_params"]
    ladder = [(int(p), int(q)) for p, q in bp["ladder"]]
    desired = int(bp["desired_count"])
    limit = int(bp["limit_price_cents"])
    rows = []
    for s in range(seeds):
        for model_name, fn in (
            ("optimistic", optimistic_fill),
            ("conservative", conservative_fill),
        ):
            kw = {} if model_name == "optimistic" else {"seed": base_seed + s}
            filled, cost, fee = fn(ladder, limit, desired, **kw)
            avg_px = round(cost / filled, 2) if filled else None
            rows.append(
                {
                    "seed": base_seed + s,
                    "model": model_name,
                    "filled": filled,
                    "fill_rate": round(filled / desired, 3),
                    "avg_price_cents": avg_px,
                    "cost_cents": cost,
                    "fee_cents": fee,
                }
            )
    summary = {}
    for model_name in ("optimistic", "conservative"):
        fr = [r["fill_rate"] for r in rows if r["model"] == model_name]
        px = [
            r["avg_price_cents"]
            for r in rows
            if r["model"] == model_name and r["avg_price_cents"] is not None
        ]
        summary[model_name] = {
            "fill_rate_mean": round(statistics.fmean(fr), 3),
            "fill_rate_min": round(min(fr), 3),
            "avg_price_cents_mean": round(statistics.fmean(px), 2) if px else None,
        }
    return {
        "name": spec["name"],
        "kind": "book",
        "seeds": seeds,
        "per_seed": rows,
        "summary": summary,
    }


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def render_stress_report(results: List[Dict[str, Any]], seeds: int) -> str:
    lines = [
        "# Adversarial stress-test report",
        "",
        f"Generated {dt.datetime.now(UTC).isoformat()} | seeds per scenario: {seeds} "
        f"| contract v{DATA_CONTRACT_VERSION}",
        "",
        "Candle scenarios synthesize 7 days of 60s candles (seeded GBM) and run "
        "them through `backtest.backtester.run_backtest` with default params. "
        "Book scenarios run `optimistic_fill` vs `conservative_fill` on the "
        "scenario ladder. Drawdown is the max peak-to-trough of the traded equity "
        "curve, in dollars.",
        "",
        "## Candle scenarios: PnL and drawdown distribution",
        "",
        "| scenario | pnl mean $ | pnl p5 $ | pnl p95 $ | drawdown mean $ | "
        "drawdown p95 $ | drawdown max $ | hit rate | traded/wk |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if r["kind"] != "candles":
            continue
        p, d = r["pnl_dollars"], r["drawdown_dollars"]
        hr = r["hit_rate"]["mean"]
        lines.append(
            f"| {r['name']} | {p['mean']} | {p['p5']} | {p['p95']} | "
            f"{d['mean']} | {d['p95']} | {d['max']} | {hr} | {r['windows_traded_mean']} |"
        )
    lines += [
        "",
        "## Book scenarios: fill stress",
        "",
        "| scenario | model | fill rate (mean) | fill rate (min) | " "avg price c (mean) |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        if r["kind"] != "book":
            continue
        for model_name, s in r["summary"].items():
            lines.append(
                f"| {r['name']} | {model_name} | {s['fill_rate_mean']} | "
                f"{s['fill_rate_min']} | {s['avg_price_cents_mean']} |"
            )
    lines += [
        "",
        "## Reading this",
        "",
        "- The backtest's headline numbers are conditional on its documented "
        "assumptions (modeled entry prices, full fills). These scenarios probe "
        "where those assumptions break: thin books cut the conservative fill "
        "rate, volatility shocks widen the drawdown distribution.",
        "- `trading_halt` shows the harness skips gap windows without crashing; "
        "check `windows_skipped_mean` in the JSON for the exact count.",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def cmd_generate(args) -> int:
    settings = get_settings()
    out_dir = args.out or settings.scenarios_dir
    os.makedirs(out_dir, exist_ok=True)
    specs = template_scenarios(args.seed)
    if args.llm:
        from datalake.ai_audit import DEFAULT_ENDPOINT, call_llm

        api_key = os.environ.get("GOOGLE_GEMINI_API_KEY")
        if not api_key:
            print("error: --llm needs GOOGLE_GEMINI_API_KEY in the environment", file=sys.stderr)
            return 1
        prompt = (
            "You design adversarial market scenarios for stress-testing a "
            "Kalshi BTC 15-minute binary-options backtester. Propose 2-3 NEW "
            "scenarios (different from: baseline, volatility_shock, flash_crash, "
            "trend_day, trading_halt, normal_book, thin_book, wide_spread). "
            "Each scenario is kind 'candles' (candle_params: days 1..30, "
            "start_price, mu_per_min, sigma_mult, optional shock {start_min, "
            "len_min, sigma_mult}, optional jump {at_min, log_jump}, optional gap "
            "{start_min, len_min}) or kind 'book' (book_params: ladder as "
            "[[price_cents 1..99, size>0]...], desired_count, limit_price_cents). "
            "Return ONLY a JSON array of objects with keys: name, kind, "
            "description, candle_params OR book_params. No markdown fences."
        )
        raw = call_llm(prompt, args.llm_model, DEFAULT_ENDPOINT, api_key)
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            import re as _re

            cleaned = _re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
            cleaned = _re.sub(r"\n?```$", "", cleaned)
        try:
            extra = json.loads(cleaned)
        except json.JSONDecodeError:
            print("error: LLM did not return valid JSON; template scenarios kept", file=sys.stderr)
            return 1
        for spec in extra:
            spec.setdefault("seed", args.seed)
            spec.setdefault("generated_at", dt.datetime.now(UTC).isoformat())
            spec.setdefault("contract_version", DATA_CONTRACT_VERSION)
            spec["generator"] = "llm"
            problems = validate_scenario(spec)
            if problems:
                print(
                    f"warning: skipping LLM scenario {spec.get('name')}: " f"{'; '.join(problems)}",
                    file=sys.stderr,
                )
                continue
            specs.append(spec)
        log.info("llm_scenarios_added", n=len(specs) - 8)
    written = []
    for spec in specs:
        problems = validate_scenario(spec)
        if problems:
            print(
                f"error: invalid scenario {spec.get('name')}: {'; '.join(problems)}",
                file=sys.stderr,
            )
            return 1
        path = os.path.join(out_dir, f"{spec['name']}.json")
        with open(path, "w") as f:
            json.dump(spec, f, indent=2)
        written.append(path)
    print(f"wrote {len(written)} scenarios to {out_dir}")
    return 0


def cmd_stress(args) -> int:
    settings = get_settings()
    scen_dir = args.scenarios or settings.scenarios_dir
    if not os.path.isdir(scen_dir):
        print(f"error: scenarios dir not found: {scen_dir} (run generate first)", file=sys.stderr)
        return 1
    specs = []
    for fn in sorted(os.listdir(scen_dir)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(scen_dir, fn)) as f:
            spec = json.load(f)
        problems = validate_scenario(spec)
        if problems:
            print(f"error: invalid scenario {fn}: {'; '.join(problems)}", file=sys.stderr)
            return 1
        specs.append(spec)
    if not specs:
        print(f"error: no scenario JSONs in {scen_dir}", file=sys.stderr)
        return 1
    results = []
    for spec in specs:
        log.info("stressing", scenario=spec["name"], kind=spec["kind"])
        if spec["kind"] == "candles":
            results.append(stress_candle_scenario(spec, args.seeds, args.base_seed))
        else:
            results.append(stress_book_scenario(spec, args.seeds, args.base_seed))
    out_base = args.out or os.path.join(os.path.dirname(scen_dir), "stress_report")
    if out_base.endswith(".md"):
        out_base = out_base[:-3]
    with open(out_base + ".json", "w") as f:
        json.dump(
            {
                "generated_at": dt.datetime.now(UTC).isoformat(),
                "seeds": args.seeds,
                "results": results,
            },
            f,
            indent=2,
        )
    with open(out_base + ".md", "w") as f:
        f.write(render_stress_report(results, args.seeds))
    print(f"wrote {out_base}.json and {out_base}.md")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Adversarial scenario generator + backtest stress tester."
    )
    sub = ap.add_subparsers(dest="command", required=True)
    g = sub.add_parser("generate", help="write adversarial scenario JSONs")
    g.add_argument("--out", default=None)
    g.add_argument("--seed", type=int, default=7)
    g.add_argument(
        "--llm",
        action="store_true",
        help="ask an LLM for additional scenarios (needs GOOGLE_GEMINI_API_KEY)",
    )
    g.add_argument("--llm-model", default="gemini-3.8-flash")
    g.add_argument("--log-format", choices=["json", "console"], default=None)
    s = sub.add_parser("stress", help="run the backtester/fill models on scenarios")
    s.add_argument("--scenarios", default=None)
    s.add_argument("--seeds", type=int, default=5)
    s.add_argument("--base-seed", type=int, default=1000)
    s.add_argument("--out", default=None, help="report path base (default datalake/stress_report)")
    s.add_argument("--log-format", choices=["json", "console"], default=None)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "stress" and args.seeds < 1:
        build_parser().error("--seeds must be >= 1")
    settings = get_settings()
    if getattr(args, "log_format", None):
        settings.log_format = args.log_format
    configure_logging(settings.log_format, settings.log_level)
    if args.command == "generate":
        return cmd_generate(args)
    return cmd_stress(args)


if __name__ == "__main__":
    raise SystemExit(main())
