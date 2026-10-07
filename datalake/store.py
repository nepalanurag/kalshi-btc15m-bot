"""Partitioned Parquet writer: buffer -> lake.

Reads a validated hourly buffer file and writes Hive-style partitions::

    datalake/lake/date=2026-10-07/hour=09/snapshots.parquet
    datalake/lake/date=2026-10-07/hour=09/candles.parquet
    datalake/lake/date=2026-10-07/hour=09/manifest.json

The manifest records the data-contract version, row counts, per-feed time
range, validation status (with the validation report's SHA-256), and the
source file's SHA-256, so any partition is fully traceable back to the raw
buffer. Re-running for the same hour overwrites the partition (idempotent).

Only hours whose validation report says ``passed`` are stored; use
``--force`` to override (logs a loud warning and stamps the manifest).

Run:

    python -m datalake.store --hour 2026-10-07T09
    python -m datalake.store --all
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from datalake import DATA_CONTRACT_VERSION
from datalake.config import get_settings
from datalake.logging import configure_logging, get_logger
from datalake.schemas import CANDLE_SCHEMA, SNAPSHOT_SCHEMA
from datalake.validate import flatten, hour_from_filename, list_hour_files, read_buffer_file

UTC = dt.timezone.utc
log = get_logger(__name__)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def validation_report_path(validation_dir: str, hour: str) -> str:
    return os.path.join(validation_dir, f"validation_report_{hour}.json")


def load_validation_status(validation_dir: str, hour: str) -> Dict[str, Any]:
    path = validation_report_path(validation_dir, hour)
    if not os.path.exists(path):
        return {"status": "missing", "path": path}
    with open(path) as f:
        report = json.load(f)
    return {"status": report.get("status", "unknown"), "path": path, "report": report}


def partition_dir(lake_dir: str, hour: str) -> str:
    # hour looks like "2026-10-07T09Z"
    date = hour[:10]
    hh = hour[11:13]
    return os.path.join(lake_dir, f"date={date}", f"hour={hh}")


def write_partition(df: pd.DataFrame, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, path, compression="snappy")


def time_range(df: pd.DataFrame, col: str) -> Dict[str, Optional[str]]:
    if df.empty:
        return {"min": None, "max": None}
    ts = pd.to_datetime(df[col], utc=True)
    return {"min": ts.min().isoformat(), "max": ts.max().isoformat()}


def store_hour(
    buffer_dir: str, filename: str, lake_dir: str, validation_dir: str, force: bool = False
) -> Dict[str, Any]:
    hour = hour_from_filename(filename)
    src = os.path.join(buffer_dir, filename)
    vstat = load_validation_status(validation_dir, hour)
    if vstat["status"] != "passed" and not force:
        raise RuntimeError(
            f"hour {hour}: validation status is '{vstat['status']}' "
            f"({vstat['path']}); refusing to store. Re-run validate or pass --force."
        )
    if force and vstat["status"] != "passed":
        log.warning("storing_unvalidated_hour", hour=hour, validation_status=vstat["status"])

    records = read_buffer_file(src)
    snaps, candles = flatten(records)
    # Defensive: the schema gate belongs to validate, but the store re-checks
    # so a hand-edited buffer can never silently enter the lake.
    if not snaps.empty:
        SNAPSHOT_SCHEMA.validate(snaps, lazy=True)
    if not candles.empty:
        CANDLE_SCHEMA.validate(candles, lazy=True)

    dest = partition_dir(lake_dir, hour)
    snap_path = os.path.join(dest, "snapshots.parquet")
    candle_path = os.path.join(dest, "candles.parquet")
    if not snaps.empty:
        write_partition(snaps, snap_path)
    if not candles.empty:
        write_partition(candles, candle_path)

    manifest = {
        "contract_version": DATA_CONTRACT_VERSION,
        "partition": {"date": hour[:10], "hour": hour[11:13]},
        "row_counts": {"snapshots": len(snaps), "candles": len(candles)},
        "time_range": {
            "snapshots": time_range(snaps, "feed_ts"),
            "candles": time_range(candles, "feed_ts"),
        },
        "validation": {
            "status": vstat["status"],
            "report_path": vstat["path"],
            "report_sha256": sha256_file(vstat["path"]) if os.path.exists(vstat["path"]) else None,
            "forced": bool(force and vstat["status"] != "passed"),
        },
        "source": {"file": filename, "sha256": sha256_file(src)},
        "written_at": dt.datetime.now(UTC).isoformat(),
        "writer": "datalake.store",
    }
    manifest_path = os.path.join(dest, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    log.info("stored", hour=hour, partition=dest, snapshots=len(snaps), candles=len(candles))
    return manifest


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Write validated buffer hours to date/hour-partitioned Parquet "
        "with a per-partition manifest."
    )
    ap.add_argument("--hour", default=None, help="hour as YYYY-MM-DDTHH (default: latest)")
    ap.add_argument("--all", action="store_true", help="store every buffer file")
    ap.add_argument(
        "--force",
        action="store_true",
        help="store even if validation did not pass (stamped in manifest)",
    )
    ap.add_argument("--buffer-dir", default=None)
    ap.add_argument("--lake-dir", default=None)
    ap.add_argument("--validation-dir", default=None)
    ap.add_argument("--log-format", choices=["json", "console"], default=None)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    if args.buffer_dir:
        settings.buffer_dir = args.buffer_dir
    if args.lake_dir:
        settings.lake_dir = args.lake_dir
    if args.validation_dir:
        settings.validation_dir = args.validation_dir
    if args.log_format:
        settings.log_format = args.log_format
    configure_logging(settings.log_format, settings.log_level)

    files = list_hour_files(settings.buffer_dir)
    if not files:
        print(f"error: no .jsonl buffer files in {settings.buffer_dir}", file=sys.stderr)
        return 1
    if args.all:
        targets = files
    elif args.hour:
        wanted = (args.hour if args.hour.endswith("Z") else args.hour + "Z") + ".jsonl"
        targets = [f for f in files if f == wanted]
        if not targets:
            print(f"error: no buffer file for hour {args.hour}", file=sys.stderr)
            return 1
    else:
        targets = [files[-1]]

    for filename in targets:
        try:
            store_hour(
                settings.buffer_dir,
                filename,
                settings.lake_dir,
                settings.validation_dir,
                force=args.force,
            )
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
