"""Validation suite for buffered market data.

Reads one hourly buffer file (or every file with ``--all``), then runs,
in order:

1. **Schema checks** -- pandera ``DataFrameSchema`` validation of snapshots
   and candles against the versioned data contracts (``schemas.py``), plus
   named cross-column checks (bid <= ask, spread consistency, OHLC ordering,
   ladder JSON parseable, ``captured_at >= feed_ts``).
2. **Staleness check** -- per feed, the largest gap between consecutive
   records must not exceed ``staleness_threshold_seconds`` (default 180s).
   A gap means the capture loop was down or the upstream API stalled.
3. **Cross-feed alignment** -- every Kalshi snapshot must have a Coinbase
   candle whose ``candle_start`` is within ``alignment_tolerance_seconds``
   (default 150s: covers the 60s capture cadence plus one minute of upstream
   lag) of the snapshot time. Research joining the two feeds is
   only valid if they describe the same minute.

Any failure quarantines the whole batch: the hour file is copied to
``validation/quarantine/<hour>/records.jsonl`` and a
``validation_report.json`` describing every failed check is written next to
it. Exit code is 0 on pass, 2 on validation failure.

Run:

    python -m datalake.validate --hour 2026-10-07T09
    python -m datalake.validate --all
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from pandera.errors import SchemaErrors

from datalake import DATA_CONTRACT_VERSION
from datalake.config import DatalakeSettings, get_settings
from datalake.logging import configure_logging, get_logger
from datalake.schemas import CANDLE_SCHEMA, SNAPSHOT_SCHEMA

UTC = dt.timezone.utc
log = get_logger(__name__)

EXIT_OK = 0
EXIT_VALIDATION_FAILED = 2


@dataclasses.dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclasses.dataclass
class ValidationResult:
    hour: str
    ok: bool
    report: Dict[str, Any]
    exit_code: int


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def list_hour_files(buffer_dir: str) -> List[str]:
    if not os.path.isdir(buffer_dir):
        return []
    return sorted(f for f in os.listdir(buffer_dir) if f.endswith(".jsonl"))


def hour_from_filename(filename: str) -> str:
    return filename[: -len(".jsonl")]


def read_buffer_file(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"buffer file not found: {path}")
    records: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
    return records


def flatten(records: List[Dict[str, Any]]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Split buffer records into snapshot / candle DataFrames (flat columns).

    Accepts both the flat record layout written by ``capture.py`` and the
    enveloped layout ``{"record_type", "captured_at", "feed_ts", "payload"}``.
    """
    snaps: List[Dict[str, Any]] = []
    candles: List[Dict[str, Any]] = []
    for r in records:
        rtype = r.get("record_type")
        payload = r.get("payload")
        if isinstance(payload, dict):
            row = {
                "record_type": rtype,
                "captured_at": r.get("captured_at"),
                "feed_ts": r.get("feed_ts"),
            }
            row.update(payload)
        else:
            row = dict(r)
            row["record_type"] = rtype
        (snaps if rtype == "snapshot" else candles).append(row)
    # Unknown record types are a schema-level problem; keep them visible.
    unknown = [r for r in records if r.get("record_type") not in ("snapshot", "candle")]
    if unknown:
        raise ValueError(
            f"{len(unknown)} record(s) with unknown record_type: "
            f"{sorted({r.get('record_type') for r in unknown})}"
        )
    return pd.DataFrame(snaps), pd.DataFrame(candles)


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def check_schema(df: pd.DataFrame, schema, label: str) -> CheckResult:
    if df.empty:
        return CheckResult(f"schema:{label}", False, "no records of this type in batch")
    try:
        schema.validate(df, lazy=True)
    except SchemaErrors as exc:
        cases = exc.failure_cases
        lines = []
        for _, row in cases.head(10).iterrows():
            lines.append(
                f"column `{row['column']}`: check `{row['check']}` failed "
                f"on {row['failure_case']!r} (index {row['index']})"
            )
        more = f" (+{len(cases) - 10} more)" if len(cases) > 10 else ""
        return CheckResult(
            f"schema:{label}", False, f"{len(cases)} violation(s): " + "; ".join(lines) + more
        )
    n = len(df)
    return CheckResult(
        f"schema:{label}", True, f"{n} rows satisfy the v{DATA_CONTRACT_VERSION} contract"
    )


def _le(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a <= b) | a.isna() | b.isna()


def run_cross_checks(snaps: pd.DataFrame, candles: pd.DataFrame) -> List[CheckResult]:
    out: List[CheckResult] = []
    if not snaps.empty:
        bad = snaps[
            ~(
                _le(snaps["yes_bid_cents"], snaps["yes_ask_cents"])
                & _le(snaps["no_bid_cents"], snaps["no_ask_cents"])
            )
        ]
        out.append(
            CheckResult(
                "cross:bid_ask_order",
                bad.empty,
                (
                    "all bids <= asks"
                    if bad.empty
                    else f"{len(bad)} snapshot(s) with bid > ask (e.g. index {bad.index[:3].tolist()})"
                ),
            )
        )

        ok_spread = (
            snaps["spread_cents"].isna()
            | (
                (snaps["yes_ask_cents"] - snaps["yes_bid_cents"] - snaps["spread_cents"]).abs()
                < 1e-9
            )
            | snaps["yes_ask_cents"].isna()
            | snaps["yes_bid_cents"].isna()
        )
        bad = snaps[~ok_spread]
        out.append(
            CheckResult(
                "cross:spread_consistent",
                bad.empty,
                (
                    "spread == yes_ask - yes_bid everywhere"
                    if bad.empty
                    else f"{len(bad)} snapshot(s) with inconsistent spread"
                ),
            )
        )

        bad_ladders = []
        for idx, row in snaps.iterrows():
            for key, nkey in (
                ("ladder_yes_json", "n_levels_yes"),
                ("ladder_no_json", "n_levels_no"),
            ):
                try:
                    levels = json.loads(row[key])
                    assert isinstance(levels, list)
                    if len(levels) != int(row[nkey]):
                        bad_ladders.append(idx)
                        break
                except (ValueError, AssertionError, TypeError):
                    bad_ladders.append(idx)
                    break
        out.append(
            CheckResult(
                "cross:ladders_parseable",
                not bad_ladders,
                (
                    "all ladders parse and match n_levels"
                    if not bad_ladders
                    else f"{len(bad_ladders)} snapshot(s) with unparseable/mismatched ladders"
                ),
            )
        )

        late = snaps[
            pd.to_datetime(snaps["captured_at"], utc=True)
            < pd.to_datetime(snaps["feed_ts"], utc=True)
        ]
        out.append(
            CheckResult(
                "cross:captured_after_feed",
                late.empty,
                (
                    "captured_at >= feed_ts everywhere"
                    if late.empty
                    else f"{len(late)} snapshot(s) captured before their feed timestamp"
                ),
            )
        )

    if not candles.empty:
        ohlc_ok = (candles["high"] >= candles[["open", "close"]].max(axis=1)) & (
            candles["low"] <= candles[["open", "close"]].min(axis=1)
        )
        bad = candles[~ohlc_ok]
        out.append(
            CheckResult(
                "cross:candle_ohlc",
                bad.empty,
                (
                    "high/low bound open/close everywhere"
                    if bad.empty
                    else f"{len(bad)} candle(s) violating OHLC ordering"
                ),
            )
        )

        starts = pd.to_datetime(candles["candle_start"], utc=True)
        feeds = pd.to_datetime(candles["feed_ts"], utc=True)
        aligned = (starts == feeds) & (starts.dt.second == 0) & (starts.dt.microsecond == 0)
        bad = candles[~aligned]
        out.append(
            CheckResult(
                "cross:candle_aligned",
                bad.empty,
                (
                    "feed_ts == minute-aligned candle_start everywhere"
                    if bad.empty
                    else f"{len(bad)} candle(s) not minute-aligned"
                ),
            )
        )
    return out


def check_staleness(df: pd.DataFrame, label: str, threshold_s: int) -> CheckResult:
    name = f"staleness:{label}"
    if df.empty:
        return CheckResult(name, False, "no records: feed is missing entirely")
    if len(df) < 2:
        return CheckResult(
            name, True, "only 1 record: gap analysis needs >= 2 (warning, not a failure)"
        )
    ts = pd.to_datetime(df["feed_ts"], utc=True).sort_values().reset_index(drop=True)
    gaps = ts.diff().dt.total_seconds().dropna()
    worst = float(gaps.max())
    at = ts[gaps.idxmax()]
    if worst > threshold_s:
        return CheckResult(
            name,
            False,
            f"max gap {worst:.0f}s > {threshold_s}s around {at.isoformat()}: "
            f"capture was down or the feed stalled",
        )
    return CheckResult(name, True, f"max gap {worst:.0f}s <= {threshold_s}s over {len(df)} records")


def check_alignment(snaps: pd.DataFrame, candles: pd.DataFrame, tolerance_s: int) -> CheckResult:
    name = "alignment:snapshot_vs_candle"
    if snaps.empty or candles.empty:
        return CheckResult(name, False, "cannot align: one of the feeds has no records")
    s_ts = pd.to_datetime(snaps["feed_ts"], utc=True).reset_index(drop=True)
    c_ts = pd.to_datetime(candles["candle_start"], utc=True).sort_values().reset_index(drop=True)
    c_vals = c_ts.values.astype("datetime64[s]").astype("int64")
    worst = 0.0
    n_bad = 0
    for v in s_ts.values.astype("datetime64[s]").astype("int64"):
        delta = float(abs(c_vals - v).min())
        worst = max(worst, delta)
        if delta > tolerance_s:
            n_bad += 1
    if n_bad:
        return CheckResult(
            name,
            False,
            f"{n_bad}/{len(snaps)} snapshot(s) have no candle within {tolerance_s}s "
            f"(worst {worst:.0f}s): feeds are describing different minutes",
        )
    return CheckResult(
        name, True, f"every snapshot within {tolerance_s}s of a candle (worst {worst:.0f}s)"
    )


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def quarantine_batch(
    hour: str, source_path: str, validation_dir: str, report: Dict[str, Any]
) -> str:
    dest_dir = os.path.join(validation_dir, "quarantine", hour)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "records.jsonl")
    with open(source_path, "rb") as src, open(dest, "wb") as out:
        out.write(src.read())
    with open(os.path.join(dest_dir, "validation_report.json"), "w") as f:
        json.dump(report, f, indent=2)
    return dest


def run_validation(
    buffer_dir: str, filename: str, validation_dir: str, settings: DatalakeSettings
) -> ValidationResult:
    hour = hour_from_filename(filename)
    path = os.path.join(buffer_dir, filename)
    records = read_buffer_file(path)
    log.info("validating", hour=hour, records=len(records), source=filename)

    report: Dict[str, Any] = {
        "contract_version": DATA_CONTRACT_VERSION,
        "hour": hour,
        "source_file": filename,
        "validated_at": dt.datetime.now(UTC).isoformat(),
        "counts": {"records": len(records), "snapshots": 0, "candles": 0},
        "checks": [],
        "status": "passed",
        "quarantine_path": None,
    }
    checks: List[CheckResult] = []

    try:
        snaps, candles = flatten(records)
    except ValueError as exc:
        checks.append(CheckResult("load:records", False, str(exc)))
        snaps, candles = pd.DataFrame(), pd.DataFrame()

    report["counts"]["snapshots"] = len(snaps)
    report["counts"]["candles"] = len(candles)

    if not [c for c in checks if not c.passed]:
        checks.append(check_schema(snaps, SNAPSHOT_SCHEMA, "snapshots"))
        checks.append(check_schema(candles, CANDLE_SCHEMA, "candles"))
        checks.extend(run_cross_checks(snaps, candles))
        checks.append(check_staleness(snaps, "snapshots", settings.staleness_threshold_seconds))
        checks.append(check_staleness(candles, "candles", settings.staleness_threshold_seconds))
        checks.append(check_alignment(snaps, candles, settings.alignment_tolerance_seconds))

    failed = [c for c in checks if not c.passed]
    report["checks"] = [dataclasses.asdict(c) for c in checks]

    if failed:
        report["status"] = "failed"
        qpath = quarantine_batch(hour, path, validation_dir, report)
        report["quarantine_path"] = qpath
        # rewrite the report inside quarantine with the quarantine path filled in
        with open(
            os.path.join(validation_dir, "quarantine", hour, "validation_report.json"), "w"
        ) as f:
            json.dump(report, f, indent=2)
        log.error(
            "validation_failed", hour=hour, failed_checks=[c.name for c in failed], quarantine=qpath
        )
        return ValidationResult(hour, False, report, EXIT_VALIDATION_FAILED)

    os.makedirs(validation_dir, exist_ok=True)
    with open(os.path.join(validation_dir, f"validation_report_{hour}.json"), "w") as f:
        json.dump(report, f, indent=2)
    log.info(
        "validation_passed",
        hour=hour,
        snapshots=len(snaps),
        candles=len(candles),
        checks=len(checks),
    )
    return ValidationResult(hour, True, report, EXIT_OK)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Validate buffered market data: schema, staleness, cross-feed "
        "alignment. Failures quarantine the batch; exit code 2."
    )
    ap.add_argument(
        "--hour",
        default=None,
        help="hour to validate as YYYY-MM-DDTHH (default: latest buffer file)",
    )
    ap.add_argument("--all", action="store_true", help="validate every buffer file")
    ap.add_argument("--buffer-dir", default=None)
    ap.add_argument("--validation-dir", default=None)
    ap.add_argument(
        "--staleness-threshold",
        type=int,
        default=None,
        help="max allowed gap between records, seconds",
    )
    ap.add_argument(
        "--alignment-tolerance",
        type=int,
        default=None,
        help="max |snapshot_time - candle_time|, seconds",
    )
    ap.add_argument("--log-format", choices=["json", "console"], default=None)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    if args.buffer_dir:
        settings.buffer_dir = args.buffer_dir
    if args.validation_dir:
        settings.validation_dir = args.validation_dir
    if args.staleness_threshold is not None:
        if args.staleness_threshold <= 0:
            build_parser().error("--staleness-threshold must be positive")
        settings.staleness_threshold_seconds = args.staleness_threshold
    if args.alignment_tolerance is not None:
        if args.alignment_tolerance <= 0:
            build_parser().error("--alignment-tolerance must be positive")
        settings.alignment_tolerance_seconds = args.alignment_tolerance
    if args.log_format:
        settings.log_format = args.log_format
    configure_logging(settings.log_format, settings.log_level)

    files = list_hour_files(settings.buffer_dir)
    if not files:
        log.error("no_buffer_files", buffer_dir=settings.buffer_dir)
        print(f"error: no .jsonl buffer files in {settings.buffer_dir}", file=sys.stderr)
        return EXIT_VALIDATION_FAILED

    if args.all:
        targets = files
    elif args.hour:
        wanted = args.hour if args.hour.endswith("Z") else args.hour + "Z"
        targets = [f for f in files if f == wanted + ".jsonl"]
        if not targets:
            print(
                f"error: no buffer file for hour {args.hour} in {settings.buffer_dir}",
                file=sys.stderr,
            )
            return EXIT_VALIDATION_FAILED
    else:
        targets = [files[-1]]

    worst = EXIT_OK
    for filename in targets:
        try:
            result = run_validation(
                settings.buffer_dir, filename, settings.validation_dir, settings
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_VALIDATION_FAILED
        worst = max(worst, result.exit_code)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
