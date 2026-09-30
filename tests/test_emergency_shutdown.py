"""Emergency shutdown: a backend-authoritative latch that really blocks work.

Drives the REAL bootstrapped stack (no mocks of the thing under test) and the
real router, so these prove the latch is enforced at every chokepoint — chat,
tool registry, workflow engine — not just that a flag flips.
"""
from __future__ import annotations

import json
import unittest

from astra.store import Store
from astra.bootstrap import build
from astra.emergency import EmergencyShutdown, EmergencyShutdownActive
from astra.web import AstraSite, Request, WebApp
from astra.workflows.scheduler import SchedulerManager


def make_app():
    store = Store(":memory:")
    stack = build(store=store)
    SchedulerManager(store, stack["workflows"], stack["events"])
    site = AstraSite(("127.0.0.1", 0), store, stack["agent"], stack=stack)
    return store, stack, WebApp(site)


def call(app, method, path, body=None, **kw):
    req = Request(method, path, headers={}, body=body or {}, rid="t", **kw)
    resp = app.handle(req)
    return resp.status, json.loads(resp.body)


class EmergencyEnforcement(unittest.TestCase):
    def setUp(self):
        self.store, self.stack, self.app = make_app()
        self.em = self.stack["emergency"]

    def test_starts_disengaged_and_agent_runs(self):
        self.assertFalse(self.em.active)
        reply = self.stack["agent"].handle("hello")
        self.assertFalse((reply.get("data") or {}).get("emergency_shutdown"))

    def test_engaged_blocks_chat_turns(self):
        self.em.engage()
        reply = self.stack["agent"].handle("hello")
        self.assertFalse(reply["ok"])
        self.assertTrue(reply["data"]["emergency_shutdown"])

    def test_engaged_blocks_workflow_runs(self):
        self.stack["workflows"].create_definition("wf", steps=[]) \
            if hasattr(self.stack["workflows"], "create_definition") else None
        self.em.engage()
        with self.assertRaises(EmergencyShutdownActive):
            self.stack["workflows"].run(name="wf")

    def test_engaged_blocks_agent_tool_calls_but_not_plain_reads(self):
        from astra.tools.schemas import Tool
        from astra.core.permissions import Level
        reg = self.stack["registry"]
        ran = []
        reg.register(Tool(name="t_read", description="r", category="x",
                          fn=lambda a, c: ran.append("read") or "ok",
                          risk=Level.READ))
        reg.register(Tool(name="t_browser", description="b", category="x",
                          fn=lambda a, c: ran.append("browser") or "ok",
                          risk=Level.BROWSER_ACTION))
        reg.policy.granted.add("browser_action")

        class AgentCtx:
            agent_execution = True
        self.em.engage()
        # the Agent may do NOTHING
        out = reg.execute("t_read", {}, ctx=AgentCtx())
        self.assertEqual(out["decision"], "blocked")
        # a non-agent caller keeps plain reads (so the UI can still inspect)…
        self.assertTrue(reg.execute("t_read", {})["ok"])
        # …but no browser/system/financial/admin tool runs for anyone
        self.assertEqual(reg.execute("t_browser", {})["decision"], "blocked")
        self.assertEqual(ran, ["read"])
        self.em.release()
        self.assertTrue(reg.execute("t_browser", {})["ok"])

    def test_engage_stops_web3_and_reports_it_really(self):
        self.em.engage()
        self.assertTrue(self.stack["tx_manager"].stopped)
        # this endpoint used to always answer False (no `stopped` attribute)
        _, body = call(self.app, "GET", "/api/web3/transaction-policy")
        self.assertTrue(body["data"]["stopped"])
        self.em.release()
        self.assertFalse(self.stack["tx_manager"].stopped)

    def test_engage_reports_each_subsystem_honestly(self):
        result = self.em.engage()
        by = {s["subsystem"]: s["status"] for s in result["subsystems"]}
        self.assertEqual(by["web3"], "stopped")
        self.assertEqual(by["workflows"], "stopped")
        self.assertIn(by["scheduler"], ("blocked", "absent"))   # never "stopped"

    def test_missing_subsystems_are_reported_absent_not_stopped(self):
        em = EmergencyShutdown(Store(":memory:"), stack={})
        by = {s["subsystem"]: s["status"] for s in em.engage()["subsystems"]}
        self.assertTrue(all(v == "absent" for v in by.values()), by)

    def test_a_failing_subsystem_is_reported_failed_and_the_rest_still_stop(self):
        class Boom:
            def close_all(self):
                raise RuntimeError("nope")
        self.stack["terminal"] = Boom()
        result = self.em.engage()
        by = {s["subsystem"]: s["status"] for s in result["subsystems"]}
        self.assertEqual(by["terminal"], "failed")
        self.assertEqual(by["web3"], "stopped")
        self.assertIn("terminal", result["failed"])
        self.assertTrue(self.em.active)                # latch holds regardless

    def test_latch_survives_a_restart(self):
        self.em.engage()
        again = EmergencyShutdown(self.store, self.stack)
        self.assertTrue(again.active)
        again.release()
        self.assertFalse(EmergencyShutdown(self.store, self.stack).active)


class EmergencyRoutes(unittest.TestCase):
    def setUp(self):
        _, self.stack, self.app = make_app()

    def test_requires_explicit_confirm(self):
        for body in ({}, {"confirm": "true"}, {"confirm": 1}, {"confirm": False}):
            status, out = call(self.app, "POST", "/api/security/emergency-shutdown", body)
            self.assertEqual(status, 400, body)
            self.assertEqual(out["error_code"], "validation")
        self.assertFalse(self.stack["emergency"].active)

    def test_shutdown_then_status_then_release(self):
        status, out = call(self.app, "POST", "/api/security/emergency-shutdown",
                           {"confirm": True})
        self.assertEqual(status, 200)
        self.assertTrue(out["data"]["active"])
        _, st = call(self.app, "GET", "/api/security/status")
        self.assertEqual(st["data"]["posture"]["state"], "shutdown")
        self.assertTrue(st["data"]["emergency"]["active"])
        _, rel = call(self.app, "POST", "/api/security/emergency-release",
                      {"confirm": True})
        self.assertFalse(rel["data"]["active"])
        _, st = call(self.app, "GET", "/api/security/status")
        self.assertFalse(st["data"]["emergency"]["active"])

    def test_v1_prefix_and_auth_gate_apply(self):
        status, _ = call(self.app, "GET", "/api/v1/security/status")
        self.assertEqual(status, 200)
        self.app.site.operator_token = "s3cret-token-value"
        status, out = call(self.app, "POST", "/api/security/emergency-shutdown",
                           {"confirm": True})
        self.assertEqual(status, 401)
        self.assertFalse(self.stack["emergency"].active)

    def test_no_tool_exposes_the_latch_to_the_agent(self):
        names = {t["name"] for t in self.stack["registry"].list()}
        self.assertFalse({n for n in names if "emergency" in n or "shutdown" in n},
                         "the shutdown latch must not be reachable from the tool surface")


if __name__ == "__main__":
    unittest.main()
