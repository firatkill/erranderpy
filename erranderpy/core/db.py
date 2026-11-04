"""SQLite storage: one file (data/assistant.db), no server.

We use Python's built-in `sqlite3` with plain SQL so every query is visible
and there is nothing extra to install. Agents bring their own tables in
`agents/<name>/schema.sql`. See docs/decisions/0005-sqlite.md.

All timestamps are stored as UTC ISO-8601 strings ("2026-11-04T15:13:16+00:00"),
which sort correctly as text.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY,
    chat_id    INTEGER NOT NULL,
    role       TEXT NOT NULL,          -- 'user' or 'assistant'
    text       TEXT NOT NULL,
    trace_id   TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_chat ON messages (chat_id, id);

CREATE TABLE IF NOT EXISTS traces (
    id         INTEGER PRIMARY KEY,
    trace_id   TEXT NOT NULL,          -- one per incoming message
    step       TEXT NOT NULL,          -- 'message', 'router.decide', 'agent.llm', 'tool.call', ...
    agent      TEXT,
    tool       TEXT,
    input      TEXT,
    output     TEXT,
    latency_ms REAL,
    tokens_in  INTEGER,
    tokens_out INTEGER,
    cost_usd   REAL,
    error      TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS traces_by_trace ON traces (trace_id);
CREATE INDEX IF NOT EXISTS traces_by_time ON traces (created_at);
"""


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the Telegram gateway runs our (synchronous)
        # pipeline in a worker thread. The lock below serializes access.
        # isolation_level=None: autocommit, every statement is saved at once.
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
        self.lock = threading.RLock()
        self.apply_schema(CORE_SCHEMA)

    def apply_schema(self, sql: str) -> None:
        """Run CREATE TABLE IF NOT EXISTS ... scripts; safe to run on every start."""
        with self.lock:
            self.conn.executescript(sql)

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        # Always pass values as `params`, never with f-strings: placeholders
        # (?) make SQL injection impossible.
        with self.lock:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    # --- settings -----------------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    # --- conversation history (short-term memory) --------------------------

    def save_message(self, chat_id: int, role: str, text: str, trace_id: str | None) -> None:
        self.execute(
            "INSERT INTO messages (chat_id, role, text, trace_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, role, text, trace_id, utc_now_iso()),
        )

    def recent_messages(self, chat_id: int, limit: int) -> list[sqlite3.Row]:
        rows = self.query(
            "SELECT role, text, trace_id FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        return list(reversed(rows))  # oldest first, as a conversation reads
