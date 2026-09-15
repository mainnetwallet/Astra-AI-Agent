"""Astra core: the Plugin interface + registry.

Future domains (token tracking, calendar, notes, trading alerts…) are added
as new Plugin subclasses — the core never changes. A plugin declares its
identity, installs its own tables into a shared Store, handles NL chat
intents, registers HTTP routes and exposes dashboard/summary/export data.
"""
from __future__ import annotations

import inspect
from typing import Callable


class Plugin:
    """Base class for every Astra module. Subclass + override what you need."""

    # identity ---------------------------------------------------------------
    slug: str = "base"             # unique id, used for manifest keys
    title: str = "Base"
    icon: str = "🧩"
    version: str = "0.1.0"
    description: str = ""
    order: int = 100               # lower = higher in the tab bar / dashboard

    # lifecycle --------------------------------------------------------------
    def __init__(self, store):
        self.store = store
        if getattr(self, "SCHEMA", None):
            store.install(self.SCHEMA)

    def installed(self) -> None:
        """Called after ALL plugins are loaded & registered. Override for any
        one-time wiring (e.g. deriving settings)."""

    # optional capability hooks -------------------------------------------------
    def process(self, text: str) -> tuple[bool, object, str, dict] | None:
        """Handle a chat message. Return (handled, reply, action, data) if you
        understand it, else None to let the next plugin / LLM try."""
        return None

    def routes(self) -> list[tuple]:
        """Declare HTTP routes: (method, path_parts_tuple, handler).

        path_parts_tuple may contain '<id>' placeholders that match ints and
        are injected into `params` as `id`. Example:
            ("GET",  ("api", "wallets"),             handler_list)
            ("PATCH",("api", "wallets", "<id>"),     handler_update)
        Handlers are `(store, params, body) -> (status, payload)`."""
        return []

    def summary(self) -> dict | None:
        """Contribute optional KPI cards/blocks to the shared dashboard.
        Keys are JSON-friendly scalars/primitive lists."""
        return None

    def export(self) -> dict | None:
        """Return data to include in a global backup file."""
        return None

    def import_data(self, payload: dict) -> dict | None:
        """Restore data handed back from `export()`. Return a small report
        dict ({added_x: n, ...}) or None if nothing handled."""
        return None


class Registry:
    """Collects plugin classes by import path and instantiates them on a store."""

    def __init__(self):
        self._classes: list[type[Plugin]] = []

    def add(self, cls: type[Plugin]) -> None:
        if not inspect.isclass(cls) or not issubclass(cls, Plugin):
            raise TypeError(f"{cls!r} is not a Plugin subclass")
        self._classes.append(cls)

    def load(self, store) -> list[Plugin]:
        """Instantiate every registered plugin against `store`, sorted by
        `order` (stable), then fire `installed()` on each."""
        plugins = [cls(store) for cls in self._classes]
        plugins.sort(key=lambda p: (p.order, p.slug))
        for p in plugins:
            p.installed()
        return plugins

    @property
    def classes(self) -> list[type[Plugin]]:
        return list(self._classes)