"""Astra core: the Plugin interface + registry + shared core subsystems.

Future domains (token tracking, calendar, notes, trading alerts…) are added
as new Plugin subclasses — the core never changes. A plugin declares its
identity, installs its own tables into a shared Store, handles NL chat
intents, registers HTTP routes and exposes dashboard/summary/export data.

Core subsystems (config, events, tasks, permissions, orchestrator, …) live
in sibling modules of this package: ``astra.core.*``.
"""
from __future__ import annotations

import inspect


class Plugin:
    """Base class for every Astra module. Subclass + override what you need."""

    # identity ---------------------------------------------------------------
    slug: str = "base"             # unique id, used for manifest keys
    title: str = "Base"
    icon: str = "🧩"
    version: str = "0.1.0"
    description: str = ""
    author: str = ""               # plugin author (defaults to "Astra")
    order: int = 100               # lower = higher in the tab bar / dashboard
    capabilities: list[str] = []   # e.g. ["airdrops", "tasks", "wallets"]
    permissions: list[str] = []    # permissions the plugin requests, e.g. ["read", "write"]

    # lifecycle --------------------------------------------------------------
    enabled: bool = True           # disable without unloading (Plugin System 2.0)

    def __init__(self, store, config=None):
        self.store = store
        self.config = config
        if getattr(self, "SCHEMA", None):
            store.install(self.SCHEMA)

    def startup(self) -> None:
        """Called after ALL plugins are loaded & registered, right before the
        server starts. Override for background threads, watchers, etc."""

    def shutdown(self) -> None:
        """Called on graceful server stop. Override to stop background work."""

    def health_check(self) -> dict:
        """Return a diagnostics dict for GET /api/health, e.g.
        {"ok": True, "details": "..."} or {"ok": False, "error": "..."}."""
        return {"ok": True}

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

    def tools(self) -> list[dict]:
        """Declare tools for the universal tool registry. Each entry:
        {"name", "description", "category", "risk", "requires_confirmation",
         "fn": callable(args, ctx)->dict}. Registered at boot so other modules
        (orchestrator, workflows) can call plugin capabilities generically."""
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
    """Collects plugin classes by import path and instantiates them on a store.

    Plugin System 2.0: each plugin is isolated (a synthetic init failure marks
    it broken instead of crashing the whole load) and its `enabled` flag is
    persisted in the store's `astra_plugin_state` table so it can be toggled
    at runtime without touching code.
    """

    STATE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS astra_plugin_state (
        slug     TEXT PRIMARY KEY,
        enabled  INTEGER NOT NULL DEFAULT 1,
        broken   TEXT DEFAULT '',
        updated_at TEXT DEFAULT ''
    );
    """

    def __init__(self):
        self._classes: list[type[Plugin]] = []

    def add(self, cls: type[Plugin]) -> None:
        if not inspect.isclass(cls) or not issubclass(cls, Plugin):
            raise TypeError(f"{cls!r} is not a Plugin subclass")
        self._classes.append(cls)

    def load(self, store, config=None) -> list[Plugin]:
        """Instantiate every registered plugin against `store`, sorted by
        `order`, honouring persisted enabled/broken state, then fire
        `installed()` on each. A plugin that fails to construct is kept in
        the list with `enabled=False` and a `broken` reason (isolation)."""
        if config is None:
            from .config import Config
            config = Config()
        store.install(self.STATE_SCHEMA)
        plugins: list[Plugin] = []
        for cls in self._classes:
            state = store.fetchone(
                "SELECT enabled, broken FROM astra_plugin_state WHERE slug = ?",
                (getattr(cls, "slug", "base"),))
            try:
                p = cls(store, config)
                p.enabled = bool(state["enabled"]) if state else p.enabled
                # a previously-broken plugin is only auto-retried after the
                # store version bumps; user must re-enable explicitly
                if state and state["broken"]:
                    p.enabled = False
                p.health = {"broken": state["broken"]} if state and state["broken"] else None
            except Exception as e:                 # plugin isolation
                broken = f"{type(e).__name__}: {e}"
                store.exec(
                    "INSERT INTO astra_plugin_state(slug, enabled, broken, updated_at)"
                    " VALUES(?,0,?,?) ON CONFLICT(slug) DO UPDATE SET broken=excluded.broken",
                    (getattr(cls, "slug", "base"), broken, __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                p = object.__new__(cls)             # ghost instance, not enabled
                p.slug = getattr(cls, "slug", "base")
                p.enabled = False
                p.health = {"broken": broken}
            plugins.append(p)
        plugins.sort(key=lambda p: (p.order, p.slug))
        for p in plugins:
            health = getattr(p, "health", None) or {}
            if getattr(p, "enabled", True) and not health.get("broken"):
                try:
                    p.installed()
                except Exception:
                    pass
        return plugins

    def set_enabled(self, store, slug: str, enabled: bool) -> None:
        """Persist a plugin's enabled state (no restart needed for state;
        runtime effect applies on next request/manifest generation)."""
        store.exec(
            "INSERT INTO astra_plugin_state(slug, enabled, updated_at)"
            " VALUES(?,?,?) ON CONFLICT(slug) DO UPDATE SET enabled=excluded.enabled,"
            " updated_at=excluded.updated_at, broken=''",
            (slug, 1 if enabled else 0,
             __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    @property
    def classes(self) -> list[type[Plugin]]:
        return list(self._classes)