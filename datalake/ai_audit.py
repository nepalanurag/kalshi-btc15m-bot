"""LLM red-team audit of the backtest harness.

Feeds the backtester source (``backtest/backtester.py``,
``backtest/fill_models.py``, ``backtest/ev_gate.py``,
``backtest/threshold_sensitivity.py`` -- code *and* the documented
assumptions in their docstrings) to a reviewer whose job is to find
lookahead bias, survivorship bias, and unrealistic fill assumptions.

Two modes:

* ``--dry-run`` (default): a curated 12-point checklist. Each check encodes
  *what* to look for; the evidence (file:line snippets) is extracted from
  the actual source at audit time. No network, fully reproducible.
* ``--llm``: the same material is sent to an LLM reviewer (OpenAI-compatible
  chat-completions endpoint; defaults to Gemini's, key from
  ``GOOGLE_GEMINI_API_KEY``). LLM findings are merged with the checklist.

Findings carry severity ratings (HIGH / MEDIUM / LOW / INFO) and are
written to ``datalake/ai_audit_report.md``.

Run:

    python -m datalake.ai_audit --dry-run
    python -m datalake.ai_audit --llm --llm-model gemini-3.8-flash
    python -m datalake.ai_audit --print-prompt   # inspect the LLM prompt only
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import sys
import urllib.request
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datalake.config import get_settings
from datalake.logging import configure_logging, get_logger

UTC = dt.timezone.utc
log = get_logger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDITED_FILES = [
    "backtest/backtester.py",
    "backtest/fill_models.py",
    "backtest/ev_gate.py",
    "backtest/threshold_sensitivity.py",
]
DEFAULT_REPORT = os.path.join(REPO_ROOT, "datalake", "ai_audit_report.md")
DEFAULT_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"


@dataclasses.dataclass
class Finding:
    id: str
    title: str
    category: str  # lookahead | survivorship | fills | assumptions | hygiene
    severity: str  # HIGH | MEDIUM | LOW | INFO
    status: str  # pass | flag | note
    finding: str
    evidence: str
    source: str = "checklist"  # checklist | llm


# --------------------------------------------------------------------------
# Source loading + evidence extraction
# --------------------------------------------------------------------------


def load_sources() -> Dict[str, str]:
    out = {}
    for rel in AUDITED_FILES:
        path = os.path.join(REPO_ROOT, rel)
        with open(path, encoding="utf-8") as f:
            out[rel] = f.read()
    return out


def grep(source: str, pattern: str) -> List[Tuple[int, str]]:
    hits = []
    for i, line in enumerate(source.splitlines(), 1):
        if re.search(pattern, line):
            hits.append((i, line.strip()[:160]))
    return hits


def evidence_block(rel: str, hits: List[Tuple[int, str]], limit: int = 3) -> str:
    return "\n".join(f"{rel}:{ln}: {text}" for ln, text in hits[:limit]) or "(no match)"


# --------------------------------------------------------------------------
# Dry-run checklist
# --------------------------------------------------------------------------


def run_checklist(sources: Dict[str, str]) -> List[Finding]:
    bt = sources["backtest/backtester.py"]
    fm = sources["backtest/fill_models.py"]
    findings: List[Finding] = []

    def F(id_, title, category, severity, status, finding, evidence):
        findings.append(Finding(id_, title, category, severity, status, finding, evidence))

    # 1. Decision uses only closed candles strictly before window open.
    hits = grep(bt, r"i_end = bisect\.bisect_left\(ts, w_start\)")
    F(
        "decision-no-lookahead",
        "Decision snapshot uses only candles strictly before window open",
        "lookahead",
        "INFO",
        "pass",
        "The decision index is bisect_left(ts, w_start): candles starting at or after "
        "the window open are excluded, and the lookback slice closes[i_start:i_end] "
        "ends at the last closed candle. No future information enters the signal.",
        evidence_block("backtest/backtester.py", hits),
    )

    # 2. RSI computed on closed closes only.
    hits = grep(bt, r"rsi = rsi_vals\[i_end - 1\]")
    F(
        "rsi-closed-candles",
        "RSI is evaluated on the last closed candle, not the in-progress one",
        "lookahead",
        "INFO",
        "pass",
        "rsi_vals is indexed at i_end - 1 (the newest candle strictly before the "
        "window open). The in-progress candle never enters the backtest signal.",
        evidence_block("backtest/backtester.py", hits),
    )

    # 3. Settlement uses post-decision data (correct, not lookahead).
    hits = grep(bt, r"settle_close = closes\[s_end - 1\]")
    F(
        "settlement-is-resolution",
        "Settlement reads the window-close candle, which is the contract resolution",
        "lookahead",
        "INFO",
        "note",
        "Settlement uses candles inside [w_start, w_end) -- data that only exists "
        "after the decision. For a held-to-expiry binary this is the resolution "
        "mechanism, not lookahead bias; it would only be bias if the decision used it.",
        evidence_block("backtest/backtester.py", hits),
    )

    # 4. Fee model applied.
    hits = grep(bt, r"fee_cents = fee_cents_taker\(count, price_cents\)")
    F(
        "fee-model-applied",
        "Taker fees are deducted on every fill via the bot's own fee function",
        "fills",
        "INFO",
        "pass",
        "Every traded window subtracts fee_cents_taker(count, price_cents), the same "
        "ceiling-rounding taker model the live bot uses.",
        evidence_block("backtest/backtester.py", hits),
    )

    # 5. Entry price is a modeled proxy, not a measured Kalshi price. HIGH.
    hits = grep(bt, r"stand-in for the Kalshi")
    F(
        "entry-price-modeled",
        "Entry price is a normal-CDF moneyness proxy, not a measured Kalshi ask",
        "fills",
        "HIGH",
        "flag",
        "Historical Kalshi order books are unavailable, so the backtest prices entries "
        "with P(win) under N(spot, sigma) rounded to cents. If the real book charges "
        "more (wider spreads, worse queue), every PnL number is optimistic. The "
        "assumption is documented in the module docstring, but nothing in the harness "
        "bounds the error -- the lake's captured books are the planned fix.",
        evidence_block("backtest/backtester.py", hits)
        + "\n"
        + evidence_block("backtest/backtester.py", grep(bt, r"def implied_win_prob"), limit=2),
    )

    # 6. Full-fill assumption in the main path. HIGH.
    fills_doc = "optimistic" in fm and "conservative" in fm
    hits = grep(bt, r"count = max_affordable_contracts")
    F(
        "full-fill-assumption",
        "Main backtest path assumes full fills at the proxied price",
        "fills",
        "HIGH",
        "flag",
        "run_backtest fills every entered window in full at price_cents: no queue "
        "position, no partial fills, no adverse selection, no slippage beyond fees. "
        "fill_models.py builds the conservative counterpart, but the headline PnL, "
        "hit-rate and drawdown numbers come from the optimistic path. Paper PnL "
        "should be reported under both assumptions side by side.",
        evidence_block("backtest/backtester.py", hits),
    )

    # 7. Synthetic strike grid. MEDIUM.
    hits = grep(bt, r"def synthetic_strike")
    F(
        "synthetic-strike-grid",
        "Strikes come from a synthetic round-number grid, not historical Kalshi strikes",
        "assumptions",
        "MEDIUM",
        "flag",
        "History has no Kalshi markets, so strikes are snapped to a "
        "--strike-step-usd grid. Real Kalshi BTC-15M strikes sit on round levels, so "
        "the grid is a reasonable stand-in, but a different grid changes which "
        "windows trade and at what moneyness.",
        evidence_block("backtest/backtester.py", hits),
    )

    # 8. No spread gate in the backtest. MEDIUM.
    hits = grep(bt, r"no spread gate")
    F(
        "no-spread-gate",
        "The live spread gate has no backtest equivalent (spreads unobservable)",
        "assumptions",
        "MEDIUM",
        "flag",
        "The live bot can refuse wide spreads; the backtest cannot replay them, so "
        "it trades windows the live bot might skip. Direction of bias: unknown, "
        "depends on whether wide-spread windows are systematically worse.",
        evidence_block("backtest/backtester.py", hits),
    )

    # 9. Neutral fallback side is arbitrary. MEDIUM.
    hits = grep(bt, r"neutral_fallback_side")
    F(
        "neutral-fallback-arbitrary",
        "Neutral-regime side defaults to 'yes' with no measured edge",
        "assumptions",
        "MEDIUM",
        "flag",
        "When RSI is neutral the bot still trades, defaulting to the side nearest "
        "the strike. There is no evidence this default has edge; the EV gate "
        "(ev_gate.py) exists to address exactly this but is not wired into the "
        "backtest entry path.",
        evidence_block("backtest/backtester.py", hits, limit=2),
    )

    # 10. Live-vs-backtest partial-candle difference. LOW.
    hits = grep(bt, r"in-progress candle")
    F(
        "live-partial-candle",
        "Live bot may read the in-progress candle; the backtest does not",
        "lookahead",
        "LOW",
        "note",
        "Stated difference in the harness docstring (Assumption 5). The backtest is "
        "the cleaner of the two; any live-vs-backtest mismatch here flatters the "
        "backtest slightly and should be closed by dropping the partial candle live.",
        evidence_block("backtest/backtester.py", hits, limit=2),
    )

    # 11. Survivorship: no Kalshi history is used at all. INFO.
    uses_history = bool(re.search(r"kalshi.*histor|histor.*kalshi", bt, re.I))
    F(
        "survivorship-na",
        "No historical Kalshi data is consumed, so survivorship bias cannot enter",
        "survivorship",
        "INFO",
        "pass" if not uses_history else "flag",
        "The backtest replays Coinbase candles only; delisted/expired Kalshi markets "
        "never enter the sample because no Kalshi history is used. The day the lake "
        "starts feeding real historical books, this check must be re-run: expired "
        "markets must be retained in the sample.",
        "backtest/backtester.py: no reference to historical Kalshi data "
        f"(regex search: {'found' if uses_history else 'clean'})",
    )

    # 12. Determinism.
    has_random = "import random" in bt or "np.random" in bt
    F(
        "determinism",
        "Backtest core is deterministic (no RNG in the decision path)",
        "hygiene",
        "INFO",
        "pass" if not has_random else "flag",
        "run_backtest uses no randomness: same candles + params => same report. "
        "(Randomness appears only in fill_models' conservative simulator, which is "
        "explicitly stochastic and seeded by the caller.)",
        "backtest/backtester.py: " + ("contains RNG use" if has_random else "no RNG imports"),
    )

    _ = fills_doc
    return findings


# --------------------------------------------------------------------------
# LLM mode
# --------------------------------------------------------------------------

AUDIT_SYSTEM_PROMPT = """You are a red-team quantitative reviewer auditing a historical \
backtester for a Kalshi BTC 15-minute binary-options trading bot. Your job is to find \
methodological flaws, not to praise the code.

Hunt specifically for:
1. LOOKAHEAD BIAS - any use of information at decision time that would not have been \
available live (future candles, future volatility, settlement data leaking into signals).
2. SURVIVORSHIP BIAS - any sample construction that drops losers, delisted markets, or \
skipped windows in a way that flatters results.
3. UNREALISTIC FILL ASSUMPTIONS - prices, fills, fees, spreads, queue position, partial \
fills, adverse selection.
4. Other assumption violations: unjustified parameters, mismatched live-vs-backtest \
behavior, unmeasured edge.

For each finding return: title, category (one of lookahead/survivorship/fills/assumptions/hygiene), \
severity (HIGH/MEDIUM/LOW/INFO), and the exact code or docstring quote that supports it. \
Be specific and cite file:line. Do not invent code that is not in the sources. If the \
documented assumptions already disclose a weakness, say so and rate whether the disclosure \
is sufficient.

Return ONLY a JSON array of objects with keys: title, category, severity, finding, evidence. \
No markdown fences, no prose outside the JSON."""


def build_llm_prompt(sources: Dict[str, str]) -> str:
    parts = [
        "The backtester under review consists of these files (code + documented assumptions):\n"
    ]
    for rel, text in sources.items():
        parts.append(f"\n===== {rel} =====\n{text}")
    parts.append(
        "\nAudit the backtester for lookahead bias, survivorship bias, and "
        "unrealistic fill assumptions. Return the JSON array."
    )
    return "\n".join(parts)


def call_llm(prompt: str, model: str, endpoint: str, api_key: str, timeout: int = 120) -> str:
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": AUDIT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
        }
    ).encode()
    req = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.load(resp)
    return payload["choices"][0]["message"]["content"]


def parse_llm_findings(text: str) -> List[Finding]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    try:
        items = json.loads(cleaned)
    except json.JSONDecodeError:
        return [
            Finding(
                "llm-raw",
                "LLM returned unparseable output",
                "hygiene",
                "LOW",
                "note",
                "The reviewer model did not return the requested JSON; "
                "its raw output is preserved below as evidence.",
                cleaned[:2000],
                source="llm",
            )
        ]
    out = []
    for i, it in enumerate(items):
        sev = str(it.get("severity", "INFO")).upper()
        if sev not in ("HIGH", "MEDIUM", "LOW", "INFO"):
            sev = "INFO"
        out.append(
            Finding(
                id=f"llm-{i+1}",
                title=str(it.get("title", "untitled"))[:200],
                category=str(it.get("category", "assumptions"))[:40],
                severity=sev,
                status="flag" if sev in ("HIGH", "MEDIUM") else "note",
                finding=str(it.get("finding", ""))[:2000],
                evidence=str(it.get("evidence", ""))[:2000],
                source="llm",
            )
        )
    return out


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}


def render_report(findings: List[Finding], mode: str, model: Optional[str]) -> str:
    by_sev: Dict[str, int] = {}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
    lines = [
        "# AI red-team audit: backtest harness",
        "",
        f"Generated {dt.datetime.now(UTC).isoformat()} | mode: {mode}"
        + (f" | model: {model}" if model else "")
        + f" | contract v{__import__('datalake').DATA_CONTRACT_VERSION}",
        "",
        "Scope: `backtest/backtester.py`, `backtest/fill_models.py`, "
        "`backtest/ev_gate.py`, `backtest/threshold_sensitivity.py` -- code and "
        "documented assumptions. The audit targets lookahead bias, survivorship "
        "bias, and unrealistic fill assumptions.",
        "",
        "## Summary",
        "",
        "| severity | count |",
        "|---|---|",
    ]
    for sev in ("HIGH", "MEDIUM", "LOW", "INFO"):
        lines.append(f"| {sev} | {by_sev.get(sev, 0)} |")
    lines += ["", "## Findings", ""]
    for f in sorted(findings, key=lambda x: (SEVERITY_ORDER.get(x.severity, 4), x.id)):
        lines += [
            f"### [{f.severity}] {f.title}",
            "",
            f"- id: `{f.id}` | category: {f.category} | status: {f.status} "
            f"| source: {f.source}",
            "",
            f.finding,
            "",
            "Evidence:",
            "```",
            f.evidence,
            "```",
            "",
        ]
    lines += [
        "## Methodology note",
        "",
        "Checklist mode encodes twelve targeted checks; each check's reasoning is "
        "fixed, but the evidence quotes are extracted from the audited source at "
        "run time, so the report goes stale if the code changes -- re-run the audit "
        "after any backtest change. LLM mode sends the same sources to an external "
        "reviewer and merges its findings; treat LLM findings as leads, not verdicts.",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Red-team audit of the backtest harness: checklist (--dry-run, "
        "default) or LLM reviewer (--llm). Writes ai_audit_report.md."
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="checklist-based self-audit, no API calls (default)"
    )
    ap.add_argument("--llm", action="store_true", help="also send the sources to an LLM reviewer")
    ap.add_argument("--llm-model", default="gemini-3.8-flash", help="model name for --llm")
    ap.add_argument(
        "--llm-endpoint",
        default=DEFAULT_ENDPOINT,
        help="OpenAI-compatible chat-completions endpoint",
    )
    ap.add_argument(
        "--print-prompt", action="store_true", help="print the LLM prompt and exit (no API call)"
    )
    ap.add_argument("--out", default=None, help="report path override")
    ap.add_argument("--log-format", choices=["json", "console"], default=None)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    if args.log_format:
        settings.log_format = args.log_format
    configure_logging(settings.log_format, settings.log_level)

    sources = load_sources()
    if args.print_prompt:
        print(build_llm_prompt(sources))
        return 0

    findings = run_checklist(sources)
    mode = "dry-run"
    model = None
    if args.llm:
        api_key = os.environ.get("GOOGLE_GEMINI_API_KEY")
        if not api_key:
            print("error: --llm needs GOOGLE_GEMINI_API_KEY in the environment", file=sys.stderr)
            return 1
        prompt = build_llm_prompt(sources)
        log.info("llm_audit_started", model=args.llm_model)
        try:
            raw = call_llm(prompt, args.llm_model, args.llm_endpoint, api_key)
        except Exception as exc:
            print(f"error: LLM call failed: {exc}", file=sys.stderr)
            return 1
        llm_findings = parse_llm_findings(raw)
        findings.extend(llm_findings)
        mode = "checklist+llm"
        model = args.llm_model
        log.info("llm_audit_finished", llm_findings=len(llm_findings))

    out = args.out or DEFAULT_REPORT
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(render_report(findings, mode, model))
    n_flag = sum(1 for x in findings if x.status == "flag")
    log.info("audit_written", report=out, findings=len(findings), flags=n_flag)
    print(f"wrote {out} ({len(findings)} findings, {n_flag} flagged)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
