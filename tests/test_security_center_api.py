"""GET /api/security/status: real data only, never a secret, never a fake score."""
from __future__ import annotations

import json
import unittest

from astra.bootstrap import build
from astra.store import Store
from astra.web import AstraSite, Request, WebApp

KEY = "sk-ZZZZYYYYXXXXWWWWVVVV1234"


def status(app=None, mutate=None):
    store = Store(":memory:")
    stack = build(store=store)
    site = AstraSite(("127.0.0.1", 0), store, stack["agent"], stack=stack)
    if mutate:
        mutate(site, stack)
    app = WebApp(site)
    hdr = {"X-Astra-Token": site.operator_token} if site.operator_token else {}
    r = app.handle(Request("GET", "/api/security/status", headers=hdr, rid="t"))
    assert r.status == 200, r.body
    return json.loads(r.body)["data"], site, stack


class StatusPayload(unittest.TestCase):
    def test_no_score_is_invented(self):
        d, *_ = status()
        self.assertIsNone(d["posture"]["score"])

    def test_tool_counts_match_the_real_registry(self):
        d, _, stack = status()
        tools = stack["registry"].list()
        t = d["tools"]
        self.assertEqual(t["total"], len(tools))
        self.assertEqual(t["confirmation_required"], sum(1 for x in tools if x["requires_confirmation"]))
        self.assertEqual(t["agent_forbidden"], sum(1 for x in tools if x["agent_forbidden"]))
        self.assertEqual(t["high"] + t["medium"] + t["low"], t["total"])

    def test_controls_reflect_real_config(self):
        d, *_ = status()
        c = {x["id"]: x for x in d["controls"]}
        self.assertEqual(c["auth"]["status"], "Open")            # ASTRA_TOKEN unset
        d2, *_ = status(mutate=lambda s, st: setattr(s, "operator_token", "tok"))
        self.assertEqual({x["id"]: x for x in d2["controls"]}["auth"]["status"], "Protected")
        d3, *_ = status(mutate=lambda s, st: setattr(s, "rate_limiter", None))
        self.assertEqual({x["id"]: x for x in d3["controls"]}["rate_limit"]["status"], "Disabled")

    def test_web3_rows_come_from_the_real_policy(self):
        d, *_ = status()
        rows = {r["k"]: r for r in d["web3_rows"]}
        self.assertEqual(rows["Confirmation Mode"]["v"], "CONFIRM")
        self.assertEqual(rows["Emergency Stop"]["v"], "Not engaged")

    def test_unverifiable_runtime_facts_are_null_not_assumed_protected(self):
        d, *_ = status(mutate=lambda s, st: st.pop("runtime"))
        rows = {r["k"]: r for r in d["runtime_rows"]}
        self.assertIsNone(rows["Agent Runtime"]["v"])
        self.assertEqual(rows["Agent Runtime"]["level"], "na")
        self.assertIsNone(rows["Process Isolation"]["v"])

    def test_exposed_open_api_with_auto_mode_is_critical(self):
        def m(site, stack):
            stack["web3_policy"].set_mode("AUTO")
            site.cfg().get = (lambda k, d=None, _g=site.cfg().get: "0.0.0.0" if k == "BIND" else _g(k, d))
        d, *_ = status(mutate=m)
        self.assertGreaterEqual(d["posture"]["counts"]["critical"], 1)
        self.assertEqual(d["posture"]["state"], "at_risk")

    def test_no_secret_reaches_the_payload(self):
        def m(site, stack):
            site.operator_token = KEY
            site._allowed_origins = {"https://" + KEY + ".example"}
        d, *_ = status(mutate=m)
        blob = json.dumps(d)
        self.assertNotIn(KEY, blob)

    def test_credentials_are_counts_only(self):
        d, *_ = status()
        self.assertEqual(set(d["credentials"]), {"keys", "healthy", "failed", "providers"})

    def test_system_map_still_serves_the_same_security_rows(self):
        store = Store(":memory:"); stack = build(store=store)
        app = WebApp(AstraSite(("127.0.0.1", 0), store, stack["agent"], stack=stack))
        r = app.handle(Request("GET", "/api/system-map", headers={}, rid="t"))
        keys = [x["k"] for x in json.loads(r.body)["data"]["security"]]
        self.assertEqual(keys, ["Authentication", "Rate limiting", "CORS", "Request ID",
                                "Secret redaction", "SSRF protection", "Body size limit", "Environment"])


if __name__ == "__main__":
    unittest.main()
