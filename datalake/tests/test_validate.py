"""Test the validation suite against synthetic good/bad batches."""

import datetime as dt
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datalake.config import DatalakeSettings
from datalake.validate import (
    EXIT_OK,
    EXIT_VALIDATION_FAILED,
    main,
    run_validation,
)

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


def make_snapshot(ts: dt.datetime, **over) -> dict:
    rec = {
        "record_type": "snapshot",
        "captured_at": ts.isoformat(),
        "feed_ts": ts.isoformat(),
        "payload": {
            "record_type": "snapshot",
            "captured_at": ts.isoformat(),
            "feed_ts": ts.isoformat(),
            "market_ticker": "KXBTC15M-26OCT070900-09",
            "strike": 83400.0,
            "status": "open",
            "yes_bid_cents": 48.0,
            "yes_ask_cents": 52.0,
            "no_bid_cents": 48.0,
            "no_ask_cents": 52.0,
            "spread_cents": 4.0,
            "n_levels_yes": 2,
            "n_levels_no": 2,
            "ladder_yes_json": json.dumps([[48, 10], [47, 20]]),
            "ladder_no_json": json.dumps([[48, 12], [47, 18]]),
        },
    }
    rec["payload"].update(over)
    return rec


def make_candle(ts: dt.datetime, **over) -> dict:
    rec = {
        "record_type": "candle",
        "captured_at": (ts + dt.timedelta(seconds=65)).isoformat(),
        "feed_ts": ts.isoformat(),
        "payload": {
            "record_type": "candle",
            "captured_at": (ts + dt.timedelta(seconds=65)).isoformat(),
            "feed_ts": ts.isoformat(),
            "candle_start": ts.isoformat(),
            "open": 83400.0,
            "high": 83450.0,
            "low": 83380.0,
            "close": 83420.0,
            "volume": 1.5,
        },
    }
    rec["payload"].update(over)
    return rec


@pytest.fixture
def dirs(tmp_path):
    buffer_dir = tmp_path / "buffer"
    validation_dir = tmp_path / "validation"
    buffer_dir.mkdir()
    settings = DatalakeSettings(
        buffer_dir=str(buffer_dir),
        validation_dir=str(validation_dir),
        log_format="console",
    )
    return buffer_dir, validation_dir, settings


def write_hour(buffer_dir, hour_name: str, records) -> str:
    filename = hour_name + ".jsonl"
    with open(buffer_dir / filename, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return filename


def good_batch(n: int = 10):
    recs = []
    for i in range(n):
        ts = T0 + dt.timedelta(seconds=60 * i)
        recs.append(make_snapshot(ts))
        recs.append(make_candle(ts))
    return recs


def check_named(report, name):
    for c in report["checks"]:
        if c["name"] == name:
            return c
    raise AssertionError(f"check {name} not in report")


def test_good_batch_passes(dirs):
    buffer_dir, validation_dir, settings = dirs
    fn = write_hour(buffer_dir, "2026-10-07T09Z", good_batch())
    result = run_validation(str(buffer_dir), fn, str(validation_dir), settings)
    assert result.ok and result.exit_code == EXIT_OK
    assert result.report["status"] == "passed"
    assert result.report["counts"] == {"records": 20, "snapshots": 10, "candles": 10}
    assert all(c["passed"] for c in result.report["checks"])
    assert os.path.exists(validation_dir / "validation_report_2026-10-07T09Z.json")


def test_bad_schema_quarantines(dirs):
    buffer_dir, validation_dir, settings = dirs
    recs = good_batch(5)
    recs[3] = make_snapshot(T0 + dt.timedelta(seconds=180), yes_bid_cents=-5.0)
    fn = write_hour(buffer_dir, "2026-10-07T09Z", recs)
    result = run_validation(str(buffer_dir), fn, str(validation_dir), settings)
    assert not result.ok and result.exit_code == EXIT_VALIDATION_FAILED
    assert result.report["status"] == "failed"
    assert not check_named(result.report, "schema:snapshots")["passed"]
    qdir = validation_dir / "quarantine" / "2026-10-07T09Z"
    assert (qdir / "records.jsonl").exists()
    assert (qdir / "validation_report.json").exists()


def test_staleness_gap_fails(dirs):
    buffer_dir, validation_dir, settings = dirs
    recs = []
    for i in [0, 1, 2, 8, 9]:  # 6-minute hole between i=2 and i=8
        ts = T0 + dt.timedelta(seconds=60 * i)
        recs.append(make_snapshot(ts))
        recs.append(make_candle(ts))
    fn = write_hour(buffer_dir, "2026-10-07T09Z", recs)
    result = run_validation(str(buffer_dir), fn, str(validation_dir), settings)
    assert result.exit_code == EXIT_VALIDATION_FAILED
    stale = check_named(result.report, "staleness:snapshots")
    assert not stale["passed"]
    assert "360s" in stale["detail"]


def test_cross_feed_misalignment_fails(dirs):
    buffer_dir, validation_dir, settings = dirs
    recs = []
    for i in range(6):
        ts = T0 + dt.timedelta(seconds=60 * i)
        recs.append(make_snapshot(ts))
        # candles shifted 5 minutes into the future: no snapshot aligns
        recs.append(make_candle(ts + dt.timedelta(seconds=300)))
    fn = write_hour(buffer_dir, "2026-10-07T09Z", recs)
    result = run_validation(str(buffer_dir), fn, str(validation_dir), settings)
    assert result.exit_code == EXIT_VALIDATION_FAILED
    align = check_named(result.report, "alignment:snapshot_vs_candle")
    assert not align["passed"]


def test_bid_above_ask_fails_cross_check(dirs):
    buffer_dir, validation_dir, settings = dirs
    recs = good_batch(4)
    recs[0] = make_snapshot(T0, yes_bid_cents=60.0, yes_ask_cents=55.0, spread_cents=-5.0)
    fn = write_hour(buffer_dir, "2026-10-07T09Z", recs)
    result = run_validation(str(buffer_dir), fn, str(validation_dir), settings)
    assert result.exit_code == EXIT_VALIDATION_FAILED
    assert not check_named(result.report, "cross:bid_ask_order")["passed"]


def test_one_sided_book_passes(dirs):
    # A live market can have an empty side (seen near expiry on 2026-10-07):
    # the lake records reality; bids are nullable and n_levels may be 0.
    buffer_dir, validation_dir, settings = dirs
    recs = []
    for i in range(4):
        ts = T0 + dt.timedelta(seconds=60 * i)
        rec = make_snapshot(
            ts,
            n_levels_no=0,
            no_bid_cents=None,
            no_ask_cents=None,
            spread_cents=None,
            ladder_no_json=json.dumps([]),
        )
        recs.append(rec)
        recs.append(make_candle(ts))
    fn = write_hour(buffer_dir, "2026-10-07T09Z", recs)
    result = run_validation(str(buffer_dir), fn, str(validation_dir), settings)
    assert result.ok and result.exit_code == EXIT_OK


def test_empty_file_fails_loudly(dirs):
    buffer_dir, validation_dir, settings = dirs
    fn = write_hour(buffer_dir, "2026-10-07T09Z", [])
    result = run_validation(str(buffer_dir), fn, str(validation_dir), settings)
    assert result.exit_code == EXIT_VALIDATION_FAILED
    assert result.report["counts"]["records"] == 0


def test_cli_validates_latest_by_default(dirs, capsys):
    buffer_dir, validation_dir, settings = dirs
    write_hour(buffer_dir, "2026-10-07T09Z", good_batch(3))
    code = main(
        [
            "--buffer-dir",
            str(buffer_dir),
            "--validation-dir",
            str(validation_dir),
            "--log-format",
            "console",
        ]
    )
    assert code == EXIT_OK


def test_cli_missing_hour_fails_loudly(dirs, capsys):
    buffer_dir, validation_dir, settings = dirs
    write_hour(buffer_dir, "2026-10-07T09Z", good_batch(3))
    code = main(
        [
            "--buffer-dir",
            str(buffer_dir),
            "--validation-dir",
            str(validation_dir),
            "--hour",
            "1999-01-01T00",
            "--log-format",
            "console",
        ]
    )
    assert code == EXIT_VALIDATION_FAILED
    assert "no buffer file for hour" in capsys.readouterr().err
