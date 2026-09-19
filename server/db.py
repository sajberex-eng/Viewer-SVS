"""SQLite-каталог: слайды, пользователи, журнал просмотров."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    login         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('resident', 'teacher', 'admin')),
    name          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS slides (
    id         TEXT PRIMARY KEY,
    key        TEXT NOT NULL UNIQUE,
    size       INTEGER NOT NULL,
    mtime      REAL NOT NULL,
    width      INTEGER NOT NULL,
    height     INTEGER NOT NULL,
    objective  REAL,
    mpp        REAL,
    case_code  TEXT,
    glass      TEXT,
    stain      TEXT,
    missing    INTEGER NOT NULL DEFAULT 0,
    added_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS view_log (
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    slide_id  TEXT NOT NULL,
    opened_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    """Соединение открывается на каждый запрос: SQLite это дёшево, а потоков много."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with closing(self._connect()) as conn:
            return conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple = ()) -> int:
        with closing(self._connect()) as conn, conn:
            return conn.execute(sql, params).rowcount
