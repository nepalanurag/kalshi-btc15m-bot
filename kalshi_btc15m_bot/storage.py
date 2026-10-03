from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .util import utc_now, isoformat_z


@dataclass
class WindowRow:
    close_time: str
    window_start: str
    status: str
    decision_json: str
    attempts: int
    entry_json: Optional[str]
    settlement_json: Optional[str]
    last_update: str


@dataclass
class PositionRow:
    id: int
    close_time: str
    market_ticker: str
    side: str
    count: int
    entry_cost_cents: int
    fees_cents: int
    entry_price_cents: int
    entry_time: str
    status: str
    result: Optional[str]
    realized_pnl_cents: Optional[int]
    settlement_time: Optional[str]


class BotStorage:
    def __init__(self, sqlite_path: str):
        self.sqlite_path = sqlite_path
        self.conn = sqlite3.connect(self.sqlite_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS windows (
                close_time TEXT PRIMARY KEY,
                window_start TEXT NOT NULL,
                status TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                entry_json TEXT,
                settlement_json TEXT,
                last_update TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                close_time TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                count INTEGER NOT NULL,
                entry_cost_cents INTEGER NOT NULL,
                fees_cents INTEGER NOT NULL,
                entry_price_cents INTEGER NOT NULL,
                entry_time TEXT NOT NULL,
                status TEXT NOT NULL,
                result TEXT,
                realized_pnl_cents INTEGER,
                settlement_time TEXT
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status)")
        self.conn.commit()

    # ---- state ----
    def get_state_int(self, key: str, default: int = 0) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
        row = cur.fetchone()
        if not row:
            return default
        try:
            return int(row["value"])
        except Exception:
            return default

    def set_state_int(self, key: str, value: int) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO bot_state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(int(value))),
        )
        self.conn.commit()

    # ---- windows ----
    def upsert_window(
        self,
        *,
        close_time: str,
        window_start: str,
        status: str,
        decision: Dict[str, Any],
        attempts: int = 0,
        entry: Optional[Dict[str, Any]] = None,
        settlement: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = isoformat_z(utc_now())
        decision_json = json.dumps(decision, separators=(",", ":"), sort_keys=True)
        entry_json = json.dumps(entry, separators=(",", ":"), sort_keys=True) if entry is not None else None
        settlement_json = json.dumps(settlement, separators=(",", ":"), sort_keys=True) if settlement is not None else None

        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO windows(close_time, window_start, status, decision_json, attempts, entry_json, settlement_json, last_update)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(close_time) DO UPDATE SET
                window_start=excluded.window_start,
                status=excluded.status,
                decision_json=excluded.decision_json,
                attempts=excluded.attempts,
                entry_json=excluded.entry_json,
                settlement_json=excluded.settlement_json,
                last_update=excluded.last_update
            """,
            (close_time, window_start, status, decision_json, int(attempts), entry_json, settlement_json, now),
        )
        self.conn.commit()

    def get_window(self, close_time: str) -> Optional[WindowRow]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM windows WHERE close_time = ?", (close_time,))
        row = cur.fetchone()
        if not row:
            return None
        return WindowRow(
            close_time=row["close_time"],
            window_start=row["window_start"],
            status=row["status"],
            decision_json=row["decision_json"],
            attempts=int(row["attempts"]),
            entry_json=row["entry_json"],
            settlement_json=row["settlement_json"],
            last_update=row["last_update"],
        )

    def list_open_positions(self) -> List[PositionRow]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM positions WHERE status = 'open' ORDER BY entry_time ASC")
        rows = cur.fetchall()
        out: List[PositionRow] = []
        for r in rows:
            out.append(
                PositionRow(
                    id=int(r["id"]),
                    close_time=r["close_time"],
                    market_ticker=r["market_ticker"],
                    side=r["side"],
                    count=int(r["count"]),
                    entry_cost_cents=int(r["entry_cost_cents"]),
                    fees_cents=int(r["fees_cents"]),
                    entry_price_cents=int(r["entry_price_cents"]),
                    entry_time=r["entry_time"],
                    status=r["status"],
                    result=r["result"],
                    realized_pnl_cents=r["realized_pnl_cents"],
                    settlement_time=r["settlement_time"],
                )
            )
        return out

    def insert_position(
        self,
        *,
        close_time: str,
        market_ticker: str,
        side: str,
        count: int,
        entry_cost_cents: int,
        fees_cents: int,
        entry_price_cents: int,
        entry_time: str,
    ) -> int:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO positions(close_time, market_ticker, side, count, entry_cost_cents, fees_cents, entry_price_cents, entry_time, status)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (close_time, market_ticker, side, int(count), int(entry_cost_cents), int(fees_cents), int(entry_price_cents), entry_time),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def settle_position(
        self,
        position_id: int,
        *,
        result: str,
        realized_pnl_cents: int,
        settlement_time: str,
    ) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            UPDATE positions
            SET status='settled', result=?, realized_pnl_cents=?, settlement_time=?
            WHERE id=?
            """,
            (result, int(realized_pnl_cents), settlement_time, int(position_id)),
        )
        self.conn.commit()
