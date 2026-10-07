# Market-data lake

Production market-data infrastructure for the kalshi-btc15m-bot trading
research: scheduled capture of Kalshi BTC 15-minute order-book snapshots and
Coinbase 60-second candles, contract validation, date/hour-partitioned
Parquet storage, a monitoring dashboard, and the AI audit/stress components.

```
                    ┌─────────────┐      ┌──────────────┐
  Kalshi public API │   capture   │─────▶│    buffer    │  hourly JSONL
  (order book)      │  (60s loop) │      │ datalake/    │
                    └─────────────┘      │  buffer/     │
                    ┌─────────────┐      └──────┬───────┘
  Coinbase public   │             │             │  dvc.yaml
  API (60s candles) │             │             ▼
                    └─────────────┘      ┌──────────────┐     ┌───────────┐
                                         │   validate   │────▶│ quarantine│
                                         │ (pandera +   │     │ on failure│
                                         │  staleness + │     └───────────┘
                                         │  alignment)  │
                                         └──────┬───────┘
                                                │ passed
                                                ▼
                                         ┌──────────────┐
                                         │    store     │──▶ lake/date=…/hour=…
                                         │ (partitioned │   snapshots.parquet
                                         │  Parquet +   │   candles.parquet
                                         │  manifest)   │   manifest.json
                                         └──────┬───────┘
                                                │
                          ┌─────────────────────┼─────────────────────┐
                          ▼                     ▼                     ▼
                   dashboard.py           ai_audit.py          ai_scenarios.py
                   (JSON + HTML)          (red-team audit)     (stress tests)
```

## Why this exists

The backtest harness (`backtest/`) replays history through *modeled* prices:
it has never seen a real Kalshi order book. This lake captures the real
books, validates them against versioned contracts, and stores them so future
research (fill realism, spread-aware entry gates, realized slippage) runs on
measured data instead of proxies. `backtest/fill_models.py` already names
this pipeline as the planned fix for its synthesized-depth assumption.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r datalake/requirements.txt   # pinned

# 1. Capture a few iterations (live public APIs, no keys needed)
python -m datalake.capture --iterations 5

# 2. Validate the buffer (schema, staleness, cross-feed alignment)
python -m datalake.validate --all          # exit 2 + quarantine on failure

# 3. Store validated hours as partitioned Parquet
python -m datalake.store --all

# 4. Build the monitoring dashboard
python -m datalake.dashboard               # datalake/dashboard/index.html

# 5. Run the test suite
pytest datalake/tests -q

# 6. AI components
python -m datalake.ai_audit --dry-run      # checklist audit, no API
python -m datalake.ai_scenarios generate && python -m datalake.ai_scenarios stress --seeds 5
```

Every CLI has `--help`, sensible defaults, and fails loudly on bad input
(exit 1 for usage errors, exit 2 for validation failures). All settings are
also environment variables prefixed `DATALAKE_` (see `datalake/config.py`);
`DATALAKE_LOG_FORMAT=console` renders human-readable logs.

### Running the capture loop for real

```bash
# forever, 60s cadence, JSON logs to stderr (pipe to your aggregator)
python -m datalake.capture
```

The loop aligns to wall-clock minute boundaries, retries each fetch with
exponential backoff (5 attempts), skips the iteration on persistent failure,
and never crashes on API errors. A systemd unit or cron `@reboot` entry is
the expected deployment; the DVC `capture` stage exists so `dvc repro` runs
a bounded 5-iteration demo of the full pipeline.

## Components

| Module | What it does |
|---|---|
| `capture.py` | 60s loop: active KXBTC15M market → full order-book snapshot + latest closed Coinbase 60s candle → hourly JSONL buffer |
| `schemas.py` | Versioned data contracts (`DATA_CONTRACT_VERSION = "1.0"`): pandera schemas for snapshots and candles |
| `validate.py` | Schema checks, staleness check (no gap > 180s), cross-feed alignment (snapshot within 150s of a candle). Failures quarantine the batch, write a report, exit 2 |
| `store.py` | Hive-partitioned Parquet (`date=YYYY-MM-DD/hour=HH/`) + `manifest.json` per partition (contract version, row counts, time range, validation status + report SHA, source SHA). Idempotent |
| `dashboard.py` | Coverage, microstructure (spread/depth), PnL attribution by RSI regime (from the backtest report), fills/slippage → `dashboard.json` + static `index.html` |
| `ai_audit.py` | LLM red-team audit of the backtest harness (lookahead, survivorship, fill realism) → `ai_audit_report.md`. `--dry-run` checklist (no API); `--llm` sends code+assumptions to a reviewer |
| `ai_scenarios.py` | `generate`: 8 adversarial scenario JSONs (thin books, wide spreads, halts, vol shocks, flash crashes, trend days). `stress`: runs the backtester/fill models over seeds → drawdown distributions in `stress_report.md` |
| `tests/test_validate.py` | Synthetic good/bad batches: schema violation, 6-min staleness gap, cross-feed misalignment, bid>ask, empty file, CLI paths |

## Data contracts (v1.0)

**Snapshot** (one per capture): `market_ticker` (^KXBTC15M-), `strike` (USD),
`status` (active/open), `yes|no_bid|ask_cents` ∈ [0,100], `spread_cents` ≥ 0,
`n_levels_*` > 0, full ladders as JSON (`ladder_yes_json`, `ladder_no_json`).
The YES ask is derived by the complement rule (`100 − best NO bid`), the same
rule `backtest/fill_models.py` uses.

**Candle** (one per capture): minute-aligned `candle_start` == `feed_ts`,
`open/high/low/close` > 0 with high/low bounding open/close, `volume` ≥ 0.

Cross-column rules (bid ≤ ask, spread consistency, OHLC ordering,
`captured_at` ≥ `feed_ts`) are named checks in `validate.py` so failures name
the rule. Bump `DATA_CONTRACT_VERSION` and note it here when a contract changes.

**Why Pandera, not Great Expectations:** GX is the heavier enterprise option
(multi-table suites, data docs hosting); for single-feed dataframe contracts
Pandera is the current standard, has a smaller dependency footprint, and its
`lazy=True` error collection gives the per-column failure report the
quarantine flow needs. If the lake grows cross-table expectations (e.g.
lake-vs-ledger reconciliation), GX becomes the right tool.

## Retention and cost

Measured on 2026-10-07 against the live APIs (3 capture iterations):

| Item | Measured | Projected |
|---|---|---|
| Snapshot JSONL (full 176/74-level ladders) | ~3.7 KB/record | — |
| Candle JSONL | 254 B/record | — |
| Raw buffer | ~4.0 KB/iteration | **~5.8 MB/day** at 60s cadence |
| Lake Parquet (snappy), 1440-row scale test | ~1.9 KB/snapshot | **~2.8 MB/day** |
| Manifest + validation report | ~2 KB/partition | ~50 KB/day |

**Policy:** raw buffer kept 7 days (~40 MB), then deleted — the lake is the
system of record. Hourly lake partitions kept 90 days (~250 MB), then
compacted to daily partitions by a monthly job (planned; the manifest's
`source.sha256` chain keeps the compaction auditable). Manifests and
validation reports kept forever (KBs). **Projected steady state: ~1 GB/year.**

Cost is local disk. A DVC remote is optional: at ~1 GB/year, S3 Standard is
about $0.28/year. No API keys are needed for capture — both feeds are public.

## DVC

```bash
dvc repro      # capture (5 iters) -> validate -> store
dvc dag        # stage graph
```

Stages are defined in `dvc.yaml` with params in `params.yaml`. The
lake is DVC-tracked (see `dvc.lock`); add a remote with
`dvc remote add -d storage <url>` to share it. No remote is configured by
default.

Note: `params.yaml` lives at the repo root, not under `datalake/`, because
DVC 3.59 fails to interpolate `${...}` vars from params files in
subdirectories (params context comes back empty). Verified with a minimal
repro; root-level params are also the DVC convention.

## Operational notes

- **Market rollover.** The 15-minute markets turn over every quarter hour,
  and the API's `status=open` filter is eventually consistent: it can still
  list a just-closed market with an empty book. `pick_active_market`
  re-filters client-side to active/open status and returns None (skip the
  iteration, logged) when nothing is eligible. Observed live on 2026-10-07:
  a closed `...-30` market was listed at 16:30:10 UTC with zero levels on
  both sides; the picker now excludes it.
- **One-sided books are real data.** Near expiry a live market can show an
  empty side (observed: 251 yes-levels, 0 no-levels). The contract allows
  `n_levels = 0` with nullable bids; the dashboard reports `empty_side_share`.
  The lake records what the market showed; consumers filter.

## AI components

**Audit** (`ai_audit.py --dry-run`, the default): twelve checklist checks over
the backtester source — closed-candle decisions, fee model, determinism
(pass); modeled entry prices and full-fill assumptions (HIGH flags);
synthetic strike grid, missing spread gate, arbitrary neutral fallback
(MEDIUM). Evidence quotes are extracted from the code at run time, so re-run
after any backtest change. `--llm` additionally sends the sources to an LLM
reviewer (default `gemini-3.8-flash` via its OpenAI-compatible endpoint;
needs `GOOGLE_GEMINI_API_KEY`) and merges the findings. `--print-prompt`
dumps the prompt without calling.

**Scenarios** (`ai_scenarios.py`): `generate` writes 8 seeded adversarial
scenario JSONs to `datalake/scenarios/` (5 candle regimes, 3 book shapes);
`generate --llm` asks an LLM for additional scenarios, schema-checked before
writing. `stress --seeds N` runs the backtester (candles) and both fill
models (books) per seed and reports per-scenario PnL and drawdown
distributions (mean/p5/p95/max) in `datalake/stress_report.md`.

## Repo hygiene

- `pre-commit install` — black + ruff on `datalake/`.
- `.github/workflows/datalake-ci.yml` — pip install pinned → ruff → black --check → pytest.
- `Dockerfile` — `python:3.12-slim`, pinned install; default CMD shows `capture --help`.
- Buffer, validation reports, and the lake are gitignored; the dashboard,
  audit report, scenarios, and stress report are committed.

## Changelog

- **2026-10-07 — contract v1.0**: initial capture/validate/store/dashboard/AI components.
