"""
SQLite database for per-user state (GitHub tokens, mode, etc.).
Stdlib sqlite3, no ORM, no migrations framework.
"""
from __future__ import annotations

import sqlite3
import os
from pathlib import Path
from typing import Any

DB_PATH = Path(os.environ.get("DATA_DIR", ".")) / "bot.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id      INTEGER PRIMARY KEY,
                github_token TEXT    NOT NULL DEFAULT '',
                github_user  TEXT    NOT NULL DEFAULT '',
                mode         TEXT    NOT NULL DEFAULT 'normal',
                created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at   TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id      INTEGER PRIMARY KEY,
                created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
            );
        """)

# ponytail: single-row ops, fine for <10K users. Connection pool if scale >100 concurrent.


def ensure_user(chat_id: int) -> None:
    with _conn() as conn:
        conn.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))


def get_user(chat_id: int) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    return dict(row) if row else None


def set_github_token(chat_id: int, token: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET github_token = ?, updated_at = datetime('now') WHERE chat_id = ?",
            (token, chat_id),
        )


def set_github_user(chat_id: int, username: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET github_user = ?, updated_at = datetime('now') WHERE chat_id = ?",
            (username, chat_id),
        )


def set_mode(chat_id: int, mode: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET mode = ?, updated_at = datetime('now') WHERE chat_id = ?",
            (mode, chat_id),
        )


# ── Subscribers (migrated from JSON) ──


def subscribe(chat_id: int) -> None:
    with _conn() as conn:
        conn.execute("INSERT OR IGNORE INTO subscribers (chat_id) VALUES (?)", (chat_id,))


def unsubscribe(chat_id: int) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))


def get_subscribers() -> list[int]:
    with _conn() as conn:
        rows = conn.execute("SELECT chat_id FROM subscribers").fetchall()
    return [r["chat_id"] for r in rows]


def is_subscribed(chat_id: int) -> bool:
    with _conn() as conn:
        row = conn.execute("SELECT 1 FROM subscribers WHERE chat_id = ?", (chat_id,)).fetchone()
    return row is not None
