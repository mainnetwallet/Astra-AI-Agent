"""Command Center / System Map <-> backend response-shape contract.

`static/js/astra_os.js` is the only consumer of the Command Center and System
Map data, and it reads a fixed set of `/api/*` endpoints through the shared
`api()` helper (which requires the `{ok, data}` envelope). `system_map_model.js`
is DOM-free on purpose, so it can be driven straight from real payloads.

This test binds the two halves together: it boots the real ASGI app, fetches
exactly the endpoints the two pages read, and feeds those payloads through the
real model under node. A backend field rename, an envelope change or a new
frontend call that nothing covers therefore fails here instead of silently
rendering "Unavailable" in the UI.

It also pins the secret-free guarantee of the aggregate payloads.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request

from tests.helpers import LiveServer, make_stack

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "contract-test-operator-token"
ASTRA_OS_JS = os.path.join(ROOT, "static", "js", "astra_os.js")
MODEL_JS = os.path.join(ROOT, "static", "js", "system_map_model.js")

# Every endpoint the Command Center / System Map read (astra_os.js
# refreshData() + loadHistory()) mapped to the JSON type of `body["data"]`
# that the view code assumes.
ENDPOINTS = [
    ("/api/health", dict),
    ("/api/providers", dict),
    ("/api/gateway/health", dict),
    ("/api/router/stats", dict),
    ("/api/router/status", dict),
    ("/api/tools", dict),
    ("/api/memory", list),
    ("/api/experiences", dict),
    ("/api/workflows", list),
    ("/api/schedules", list),
    ("/api/tasks", list),
    ("/api/runtime/status", dict),
    ("/api/web3/transaction-policy", dict),
    ("/api/metrics", dict),
    ("/api/system-map", dict),
    ("/api/events?limit=200", list),
]

# Provider/model/key material that must never reach these payloads.
SECRET_PATTERNS = (
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
)


_SERVER = None
_PAYLOADS = None
_BODIES = None


def setUpModule():
    """One real server for the whole module — booting it dominates the cost."""
    global _SERVER, _PAYLOADS, _BODIES
    stack = make_stack()
    srv = LiveServer(stack=stack)
    # Make the secret-free assertions meaningful: a configured operator token
    # must never appear in an aggregate payload, so configure one and send it.
    srv.site.operator_token = TOKEN
    payloads, bodies = {}, {}
    for path, _ in ENDPOINTS:
        body, raw = _fetch(srv.base, path)
        payloads[path] = body.get("data")
        bodies[path] = raw
    _SERVER, _PAYLOADS, _BODIES = srv, payloads, bodies


def tearDownModule():
    global _SERVER
    if _SERVER is not None:
        _SERVER.stop()
        _SERVER = None


def _fetch(base, path):
    # Authenticated exactly as the browser is: a configured operator token
    # must not stop these reads, and must never show up in what they return.
    req = urllib.request.Request(base + path, headers={"X-Astra-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw), raw


class FrontendEndpointContract(unittest.TestCase):
    """Every `{ok, data}` response the two pages consume keeps its shape."""

    def test_frontend_calls_are_all_covered_here(self):
        """A new /api call in astra_os.js must be added to ENDPOINTS."""
        src = open(ASTRA_OS_JS, encoding="utf-8").read()
        called = set(re.findall(r'\b(?:api|get)\(\s*"(/api/[^"]+)"', src))
        covered = {path for path, _ in ENDPOINTS}
        self.assertTrue(called, "no /api calls found in astra_os.js")
        self.assertEqual(called - covered, set(),
                         "astra_os.js calls an endpoint this contract does not "
                         "cover — add it to ENDPOINTS with its data type")

    def test_ok_data_envelope_and_data_type(self):
        for path, expected in ENDPOINTS:
            body, _ = _fetch(_SERVER.base, path)
            self.assertTrue(body.get("ok"), f"{path} did not return ok=true")
            self.assertIn("data", body, f"{path} has no data field")
            self.assertIsInstance(body["data"], expected, path)
            # api() surfaces request_id on failures; it must exist on every
            # response so a UI error path can be correlated.
            self.assertIn("request_id", body, path)

    def test_fields_the_views_actually_read(self):
        p = _PAYLOADS
        for key in ("providers", "last_route", "router"):
            self.assertIn(key, p["/api/providers"])
        self.assertIn("state", p["/api/gateway/health"])
        self.assertIn("last_route", p["/api/router/stats"])
        self.assertIn("tools", p["/api/tools"])
        self.assertIn("stats", p["/api/tools"])
        for key in ("total", "successful", "success_rate"):
            self.assertIn(key, p["/api/experiences"])
        for key in ("mode", "policy"):
            self.assertIn(key, p["/api/web3/transaction-policy"])
        for key in ("requests", "uptime_s"):
            self.assertIn(key, p["/api/metrics"])
        for key in ("agents", "agent_manager", "security"):
            self.assertIn(key, p["/api/system-map"])
        self.assertIsInstance(p["/api/runtime/status"].get("available"), bool)

    def test_aggregate_payloads_stay_secret_free(self):
        for path in ("/api/providers", "/api/metrics", "/api/system-map",
                     "/api/runtime/status", "/api/web3/transaction-policy"):
            raw = _BODIES[path]
            self.assertNotIn(TOKEN, raw, path)
            for pattern in SECRET_PATTERNS:
                self.assertIsNone(pattern.search(raw),
                                  f"{path} leaked credential material")

    def test_provider_rows_expose_counts_not_keys(self):
        """The health view may show how many credentials exist, never which."""
        providers = _PAYLOADS["/api/providers"].get("providers") or {}
        for name, row in providers.items():
            self.assertIsInstance(row.get("credentials"), int, name)
            keys = row.get("keys")
            if keys:
                self.assertIsInstance(keys, list, name)
                for k in keys:
                    self.assertTrue(set(k) <= {"key_id", "label", "healthy",
                                               "in_cooldown", "calls",
                                               "errors", "last_error"},
                                    f"{name} per-key row exposes a new field")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ModelConsumesRealPayloads(unittest.TestCase):
    """Feed the live payloads through the real (DOM-free) view model."""

    def _run_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = os.path.join(tmp, "payloads.json")
            with open(args, "w", encoding="utf-8") as fh:
                json.dump(_PAYLOADS, fh)
            driver = os.path.join(tmp, "driver.js")
            with open(driver, "w", encoding="utf-8") as fh:
                fh.write(_DRIVER)
            proc = subprocess.run(
                ["node", driver, args, MODEL_JS],
                capture_output=True, text=True, timeout=120, cwd=ROOT)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return json.loads(proc.stdout)

    def test_model_maps_every_payload(self):
        out = self._run_model()
        # Provider cards: real providers only, always renderable.
        for card in out["providers"]:
            self.assertIn(card["status"],
                          {"online", "degraded", "rate_limited", "offline",
                           "unknown"})
            self.assertIsInstance(card["modelCount"], int)
            self.assertNotIn("keys", card)
            self.assertNotIn("base_url", card)
        # Tool counting must agree with the registry payload exactly.
        self.assertEqual(out["toolTotal"], out["registryToolCount"])
        # Architecture map: the documented components, all with renderable lines.
        self.assertEqual(out["nodeIds"], out["defIds"])
        self.assertEqual(len(out["nodeIds"]), 13)
        for line in out["lines"]:
            self.assertTrue(line["k"], "a node line has no label")
            self.assertNotIn(line["v"], (None, "undefined"))
        # KPI cards: 8 cards, never `undefined`.
        self.assertEqual(len(out["kpis"]), 8)
        for kpi in out["kpis"]:
            self.assertTrue(kpi["label"] and kpi["value"] is not None, kpi)
        # Subsystem health renders one row per component.
        self.assertEqual([r["name"] for r in out["health"]],
                         ["API", "Gateway", "Router", "Providers",
                          "ToolRegistry", "Memory", "Workflow Engine",
                          "EventBus", "Web3", "Terminal"])
        # Gateway / Router are never described as a provider.
        self.assertTrue(out["gatewayRolesNotProvider"])
        self.assertTrue(out["routerRolesNotProvider"])

    def test_operations_derive_from_real_event_history(self):
        out = self._run_model()
        for op in out["ops"]:
            self.assertIn(op["status"], {"running", "completed", "failed"})
            self.assertTrue(op["id"])


_DRIVER = r"""
"use strict";
const fs = require("fs");
const p = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const M = require(process.argv[3]);

// Field names mirror astra_os.js refreshData(); every one must exist above.
const d = {
  health: p["/api/health"],
  providersRaw: p["/api/providers"],
  providerCards: M.providerCards(p["/api/providers"]),
  gateway: p["/api/gateway/health"],
  routerStats: p["/api/router/stats"],
  router: p["/api/router/status"],
  tools: p["/api/tools"] ? M.toolGroups(p["/api/tools"]) : null,
  memory: Array.isArray(p["/api/memory"]) ? p["/api/memory"] : null,
  memoryCount: Array.isArray(p["/api/memory"]) ? p["/api/memory"].length : null,
  memoryOk: Array.isArray(p["/api/memory"]),
  experiences: p["/api/experiences"],
  workflows: Array.isArray(p["/api/workflows"]) ? p["/api/workflows"] : null,
  workflowsOk: Array.isArray(p["/api/workflows"]),
  schedules: Array.isArray(p["/api/schedules"]) ? p["/api/schedules"] : null,
  tasks: Array.isArray(p["/api/tasks"]) ? p["/api/tasks"] : null,
  runtime: p["/api/runtime/status"],
  metrics: p["/api/metrics"],
  web3: p["/api/web3/transaction-policy"],
  agents: Array.isArray(p["/api/system-map"].agents) ? p["/api/system-map"].agents : [],
  security: p["/api/system-map"].security,
  eventsOk: true,
  sseState: "live",
  eventCount: p["/api/events?limit=200"].length,
};

const events = p["/api/events?limit=200"];
const ops = M.operationsFromEvents(events);
const nodes = M.buildNodes(d);
const by = Object.fromEntries(nodes.map((n) => [n.id, n]));
const nodeLines = nodes.flatMap((n) => n.lines.map((l) => ({
  k: String(l.k == null ? "" : l.k),
  v: l.v == null ? "Unavailable" : String(l.v),
})));

process.stdout.write(JSON.stringify({
  providers: d.providerCards,
  toolTotal: d.tools ? d.tools.total : 0,
  registryToolCount: (p["/api/tools"].tools || []).length,
  nodeIds: nodes.map((n) => n.id).sort(),
  defIds: M.NODE_DEFS.map((n) => n.id).sort(),
  lines: nodeLines,
  kpis: M.kpis(d, ops),
  health: M.subsystemHealth(d),
  ops: ops,
  gatewayRolesNotProvider: /NOT a provider/.test(by.gateway.lines[0].v),
  routerRolesNotProvider: /NOT a provider/.test(by.router.lines[0].v),
}));
"""


if __name__ == "__main__":
    unittest.main()




