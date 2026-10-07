"""Unit tests for the capture loop's market selection."""

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datalake.capture import pick_active_market

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 7, 16, 20, tzinfo=UTC)


def market(ticker, status, open_time, close_time):
    return {
        "ticker": ticker,
        "status": status,
        "open_time": open_time.isoformat(),
        "close_time": close_time.isoformat(),
    }


def test_picks_market_containing_now():
    markets = [
        market(
            "KXBTC15M-26OCT071200-00",
            "active",
            NOW - dt.timedelta(minutes=20),
            NOW - dt.timedelta(minutes=5),
        ),
        market(
            "KXBTC15M-26OCT071215-15",
            "active",
            NOW - dt.timedelta(minutes=5),
            NOW + dt.timedelta(minutes=10),
        ),
    ]
    chosen = pick_active_market(markets, NOW)
    assert chosen["ticker"] == "KXBTC15M-26OCT071215-15"


def test_excludes_closed_market_even_if_listed():
    # The API's status=open filter is eventually consistent: a just-closed
    # market can still be listed. The picker must not choose it.
    markets = [
        market(
            "KXBTC15M-26OCT071215-15",
            "closed",
            NOW - dt.timedelta(minutes=20),
            NOW - dt.timedelta(minutes=5),
        ),
        market(
            "KXBTC15M-26OCT071230-30",
            "active",
            NOW - dt.timedelta(minutes=5),
            NOW + dt.timedelta(minutes=10),
        ),
    ]
    chosen = pick_active_market(markets, NOW)
    assert chosen["ticker"] == "KXBTC15M-26OCT071230-30"


def test_returns_none_when_nothing_eligible():
    markets = [
        market(
            "KXBTC15M-26OCT071215-15",
            "closed",
            NOW - dt.timedelta(minutes=20),
            NOW - dt.timedelta(minutes=5),
        ),
        market(
            "KXBTC15M-26OCT071200-00",
            "settled",
            NOW - dt.timedelta(minutes=35),
            NOW - dt.timedelta(minutes=20),
        ),
    ]
    assert pick_active_market(markets, NOW) is None
    assert pick_active_market([], NOW) is None


def test_falls_back_to_earliest_close_when_none_contains_now():
    markets = [
        market(
            "KXBTC15M-26OCT071245-45",
            "active",
            NOW + dt.timedelta(minutes=25),
            NOW + dt.timedelta(minutes=40),
        ),
        market(
            "KXBTC15M-26OCT071230-30",
            "active",
            NOW + dt.timedelta(minutes=10),
            NOW + dt.timedelta(minutes=25),
        ),
    ]
    chosen = pick_active_market(markets, NOW)
    assert chosen["ticker"] == "KXBTC15M-26OCT071230-30"
