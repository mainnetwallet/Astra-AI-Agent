"""Schema-level migration support for the Store.

Astra uses SQLite's user_version as its migration counter. Each module owns
its tables (self-installing), but structural *changes* go through here so an
existing database from an older Astra version upgrades in place without data
loss. `migrate()` runs only the steps the database hasn't reached yet.
"""
from __future__ import annotations

# version -> (sql, description). Append new migrations, never edit old ones.
MIGRATIONS: list[tuple[str, str]] = [
    # 1: baseline — core tables are installed by their own modules; this
    # just marks the first schema generation.
    ("CREATE TABLE IF NOT EXISTS astra_migrations_log ("
     " v INTEGER PRIMARY KEY, applied_at TEXT DEFAULT '');",
     "baseline migration metadata"),
]


def migrate(store, target_version: int | None = None) -> int:
    """Apply all pending migrations. Returns the new user_version."""
    cur = store.user_version()
    for v, (sql, _desc) in enumerate(MIGRATIONS, start=1):
        if v <= cur:
            continue
        store.exec(sql)
        from datetime import datetime
        store.exec(
            "INSERT INTO astra_migrations_log(v, applied_at) VALUES(?,?) "
            "ON CONFLICT(v) DO NOTHING",
            (v, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        store.set_user_version(v)
        cur = v
    return cur


def migration_log(store) -> list[dict]:
    return store.fetch("SELECT v, applied_at FROM astra_migrations_log ORDER BY v")