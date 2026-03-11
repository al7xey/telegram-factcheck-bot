"""Persistent storage for subscriptions, usage limits, and user state."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from config import config


def _connect() -> sqlite3.Connection:
    db_path = Path(config.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(db_path)


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                expires_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage (
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                count INTEGER NOT NULL,
                PRIMARY KEY (user_id, day)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS last_news (
                user_id INTEGER PRIMARY KEY,
                text TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_state (
                user_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                payload TEXT
            )
            """
        )


def get_subscription_expires_at(user_id: int) -> datetime | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT expires_at FROM subscriptions WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except ValueError:
        return None


def set_subscription(user_id: int, expires_at: datetime) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO subscriptions (user_id, expires_at)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET expires_at = excluded.expires_at
            """,
            (user_id, expires_at.isoformat()),
        )


def delete_subscription(user_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM subscriptions WHERE user_id = ?",
            (user_id,),
        )


def set_last_news(user_id: int, text: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO last_news (user_id, text, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET text = excluded.text, updated_at = excluded.updated_at
            """,
            (user_id, text, datetime.utcnow().isoformat()),
        )


def get_last_news(user_id: int) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT text FROM last_news WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return str(row[0])


def set_state(user_id: int, state: str, payload: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_state (user_id, state, payload)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET state = excluded.state, payload = excluded.payload
            """,
            (user_id, state, payload),
        )


def get_state(user_id: int) -> tuple[str, str | None] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT state, payload FROM user_state WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return str(row[0]), row[1] if row[1] is None else str(row[1])


def clear_state(user_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM user_state WHERE user_id = ?",
            (user_id,),
        )


def get_usage_count(user_id: int, day: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT count FROM usage WHERE user_id = ? AND day = ?",
            (user_id, day),
        ).fetchone()
    if not row:
        return 0
    return int(row[0])


def increment_usage(user_id: int, day: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT count FROM usage WHERE user_id = ? AND day = ?",
            (user_id, day),
        ).fetchone()
        if row:
            new_count = int(row[0]) + 1
            conn.execute(
                "UPDATE usage SET count = ? WHERE user_id = ? AND day = ?",
                (new_count, user_id, day),
            )
            return new_count
        conn.execute(
            "INSERT INTO usage (user_id, day, count) VALUES (?, ?, 1)",
            (user_id, day),
        )
        return 1
