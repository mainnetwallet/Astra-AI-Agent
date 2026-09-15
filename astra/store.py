"""Generic SQLite storage for Astra.

The Store is deliberately small and plugin-agnostic: plugins install their
own tables with `install(sql)` and then use `exec`/`fetch`/`fetchone` with
parameterised queries. Everything is plain dicts — no ORM, no surprises.

Thread-safety: a single RLock guards every call because the HTTP server is
threaded (`ThreadingHTTPServer`).
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Store:
    """Thin, generic SQLite wrapper. `Store(":memory:")` for tests."""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        parent = os.path.dirname(path)
        if path != ":memory:" and parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL" if path != ":memory:" else "PRAGMA journal_mode = MEMORY")
        self._lock = threading.RLock()

    # -- core operations -----------------------------------------------------
    def install(self, sql: str) -> None:
        """Plugin schema bootstrap; safe to run on an existing database."""
        with self._lock:
            self._conn.executescript(sql)
            self._conn.commit()

    def exec(self, sql: str, args: tuple = ()) -> int:
        """Run a write/DDL statement. Returns lastrowid (or 0)."""
        with self._lock:
            try:
                cur = self._conn.execute(sql, args)
                self._conn.commit()
                return cur.lastrowid or 0
            except Exception:
                self._conn.rollback()
                raise

    def fetch(self, sql: str, args: tuple = ()) -> list[dict]:
        """Run a query returning zero or more rows."""
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return [dict(r) for r in cur.fetchall()]

    def fetchone(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.fetch(sql, args)
        return rows[0] if rows else None

    def insert(self, table: str, **fields) -> int:
        """Convenience: INSERT INTO table (cols) VALUES (...). Returns id."""
        cols = list(fields)
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        return self.exec(sql, tuple(fields[c] for c in cols))

    def table_exists(self, name: str) -> bool:
        r = self.fetchone(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
        return r is not None

    # -- versioning / migrations ---------------------------------------------
    def user_version(self) -> int:
        r = self.fetchone("PRAGMA user_version")
        return int((r.get("user_version") if r else 0) or 0)

    def set_user_version(self, version: int) -> None:
        with self._lock:
            self._conn.execute(f"PRAGMA user_version = {int(version)}")
            self._conn.commit()

    def migrate(self, target_version: int | None = None) -> int:
        """Run pending schema migrations. Returns the new user_version."""
        from .core.store_migrations import migrate
        return migrate(self, target_version)

    # -- context manager ------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    # -- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def __del__(self) -> None:
        """Safety net: close the connection if the owner forgot to. Debug loop
        safety only — the app and tests close explicitly where practical."""
        try:
            self._conn.close()
        except Exception:
            pass