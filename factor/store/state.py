"""
SQLite state: processed alert ids, trades, meta.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS processed_alerts (
                    alert_id INTEGER PRIMARY KEY,
                    kind TEXT,
                    symbol TEXT,
                    side TEXT,
                    reason TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id INTEGER NOT NULL,
                    broker TEXT NOT NULL,
                    status TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    volume_lots REAL,
                    entry_price REAL,
                    sl_price REAL,
                    tp_price REAL,
                    kind TEXT,
                    broker_order_id TEXT,
                    detail_json TEXT,
                    created_at TEXT NOT NULL,
                    closed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_trades_symbol_status
                    ON trades(symbol, status);
                CREATE INDEX IF NOT EXISTS idx_trades_created
                    ON trades(created_at);
                """
            )

    # --- meta ---

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def last_processed_alert_id(self) -> int:
        raw = self.get_meta("last_alert_id", "0") or "0"
        try:
            return int(raw)
        except ValueError:
            return 0

    def set_last_processed_alert_id(self, alert_id: int) -> None:
        cur = self.last_processed_alert_id()
        if alert_id > cur:
            self.set_meta("last_alert_id", str(alert_id))

    # --- processed ---

    def was_processed(self, alert_id: int) -> bool:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_alerts WHERE alert_id = ?",
                (int(alert_id),),
            ).fetchone()
            return row is not None

    def mark_processed(
        self,
        alert_id: int,
        *,
        kind: str = "",
        symbol: str = "",
        side: str = "",
        reason: str = "",
    ) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO processed_alerts
                    (alert_id, kind, symbol, side, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(alert_id),
                    kind,
                    symbol,
                    side,
                    reason,
                    _iso(_utc_now()),
                ),
            )
        self.set_last_processed_alert_id(int(alert_id))

    # --- trades ---

    def insert_trade(
        self,
        *,
        alert_id: int,
        broker: str,
        status: str,
        symbol: str,
        side: str,
        volume_lots: float,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        kind: str,
        broker_order_id: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        now = _iso(_utc_now())
        detail_json = json.dumps(detail or {}, default=str)
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO trades (
                    alert_id, broker, status, symbol, side, volume_lots,
                    entry_price, sl_price, tp_price, kind, broker_order_id,
                    detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(alert_id),
                    broker,
                    status,
                    symbol,
                    side,
                    float(volume_lots),
                    float(entry_price),
                    float(sl_price),
                    float(tp_price),
                    kind,
                    broker_order_id,
                    detail_json,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def count_open_positions(self, symbol: str) -> int:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM trades
                WHERE symbol = ? AND status = 'open'
                """,
                (symbol,),
            ).fetchone()
            return int(row["n"] if row else 0)

    def count_open_positions_total(self) -> int:
        """Open trades across all symbols (status='open')."""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM trades
                WHERE status = 'open'
                """
            ).fetchone()
            return int(row["n"] if row else 0)

    def count_trades_today(self) -> int:
        # UTC day
        day = _utc_now().strftime("%Y-%m-%d")
        with self._lock, self._conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM trades
                WHERE created_at >= ? AND status IN ('open', 'filled', 'paper')
                """,
                (day,),
            ).fetchone()
            return int(row["n"] if row else 0)

    def last_trade_time(self, symbol: str) -> Optional[str]:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                """
                SELECT created_at FROM trades
                WHERE symbol = ? AND status IN ('open', 'filled', 'paper')
                ORDER BY id DESC LIMIT 1
                """,
                (symbol,),
            ).fetchone()
            return row["created_at"] if row else None

    def seconds_since(self, iso_ts: str) -> Optional[float]:
        try:
            ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return (_utc_now() - ts.astimezone(timezone.utc)).total_seconds()
        except Exception:
            return None

    def list_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM trades ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def stats(self) -> Dict[str, Any]:
        with self._lock, self._conn() as conn:
            n_proc = conn.execute(
                "SELECT COUNT(*) AS n FROM processed_alerts"
            ).fetchone()["n"]
            n_trades = conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()[
                "n"
            ]
            n_open = conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE status = 'open'"
            ).fetchone()["n"]
        return {
            "processed_alerts": int(n_proc),
            "trades": int(n_trades),
            "open_positions": int(n_open),
            "last_alert_id": self.last_processed_alert_id(),
        }
