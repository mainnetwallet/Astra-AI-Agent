"""AgentRouter.org live-test layer, health endpoint, routing-decision event
logging, and secret redaction.

Mirrors the style of tests/test_ai.py (fakes instead of real HTTP) and
tests/test_web.py (real AstraServer on an ephemeral port for endpoint tests).
The live-test HTTP layer itself is exercised by mocking urllib.request.urlopen
per tests/test_core.py's established pattern — never real network calls.
"""
from __future__ import annotations

import io
import json
import socket
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from astra.core.config import Config
from astra.ai.credentials import CredentialPool
from astra.ai.router import AgentRouter, RoutingRequest
from astra.core.exceptions import ProviderError
from astra.ai.provider import AIProvider
from astra.ai import agentrouter_livecheck as live
from astra.web import AstraServer
from tests.helpers import make_stack


def _cfg(**env):
    cfg = Config()
    for k, v in env.items():
        cfg.set(k, v)
    return cfg


def _http_response(payload: dict, status: int = 200):
    raw = json.dumps(payload).encode("utf-8")
    resp = io.BytesIO(raw)
    resp.status = status
    return resp


class _CtxResp:
    """Minimal context-manager wrapper so mocked urlopen supports `with`."""
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self._resp

    def __exit__(self, *a):
        return False


# ---------------------------------------------------------------------------
# 1. Configuration states
# ---------------------------------------------------------------------------
class TestAgentRouterConfiguration(unittest.TestCase):
    def test_missing_api_key_is_not_configured(self):
        cfg = _cfg(AGENTROUTER_API_KEYS="")
        result = live.check_agentrouter(cfg)
        self.assertFalse(result["configured"])
        self.assertFalse(result["reachable"])
        self.assertFalse(result["authenticated"])
        self.assertEqual(result["status"], "NOT_CONFIGURED")
        self.assertIsNone(result["model"])

    def test_multiple_keys_and_models_configured(self):
        cfg = _cfg(AGENTROUTER_API_KEYS="k1,k2,k3",
                   AGENTROUTER_MODELS="deepseek-v4-flash,glm-5.3")
        with mock.patch.object(urllib.request, "urlopen",
                               return_value=_CtxResp(_http_response(
                                   {"choices": [{"message": {"content": "pong"}}]}))):
            result = live.check_agentrouter(cfg, test_all_keys=True, test_all_models=True)
        self.assertTrue(result["configured"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["keys"]), 3)
        self.assertEqual(len(result["models"]), 2)
        for k in result["keys"]:
            self.assertIn(k["status"], ("success", "failed"))
            self.assertNotIn("k1", json.dumps(k))
            self.assertNotIn("k2", json.dumps(k))
            self.assertNotIn("k3", json.dumps(k))


# ---------------------------------------------------------------------------
# 2. Live-test layer: every failure state, mocked HTTP only
# ---------------------------------------------------------------------------
class TestAgentRouterLiveStates(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg(AGENTROUTER_API_KEYS="secret-key-123",
                        AGENTROUTER_MODELS="deepseek-v4-flash")

    def _run(self):
        return live.check_agentrouter(self.cfg)

    def test_200_success(self):
        with mock.patch.object(urllib.request, "urlopen",
                               return_value=_CtxResp(_http_response(
                                   {"choices": [{"message": {"content": "pong"}}]}))):
            r = self._run()
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["reachable"])
        self.assertTrue(r["authenticated"])
        self.assertIsInstance(r["latency_ms"], int)

    def test_401_unauthorized(self):
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        with mock.patch.object(urllib.request, "urlopen", side_effect=err):
            r = self._run()
        self.assertEqual(r["status"], "HTTP_401")
        self.assertTrue(r["reachable"])
        self.assertFalse(r["authenticated"])
        self.assertIn("401", r["detail"])

    def test_403_forbidden(self):
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b"{}"))
        with mock.patch.object(urllib.request, "urlopen", side_effect=err):
            r = self._run()
        self.assertEqual(r["status"], "HTTP_403")
        self.assertFalse(r["authenticated"])

    def test_429_rate_limit(self):
        err = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(b"{}"))
        with mock.patch.object(urllib.request, "urlopen", side_effect=err):
            r = self._run()
        self.assertEqual(r["status"], "HTTP_429")
        self.assertTrue(r["reachable"])
        self.assertTrue(r["authenticated"])   # rate-limited, not an auth problem

    def test_500_server_error(self):
        err = urllib.error.HTTPError("u", 500, "Internal Server Error", {}, io.BytesIO(b"{}"))
        with mock.patch.object(urllib.request, "urlopen", side_effect=err):
            r = self._run()
        self.assertEqual(r["status"], "HTTP_5XX")

    def test_timeout(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=socket.timeout()):
            r = self._run()
        self.assertEqual(r["status"], "TIMEOUT")
        self.assertFalse(r["reachable"])

    def test_network_failure(self):
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("no route")):
            r = self._run()
        self.assertEqual(r["status"], "NETWORK_ERROR")
        self.assertFalse(r["reachable"])

    def test_dns_failure(self):
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=urllib.error.URLError(socket.gaierror("nodename"))):
            r = self._run()
        self.assertEqual(r["status"], "DNS_ERROR")
        self.assertFalse(r["reachable"])

    def test_invalid_response(self):
        with mock.patch.object(urllib.request, "urlopen",
                               return_value=_CtxResp(_http_response({"nonsense": True}))):
            r = self._run()
        self.assertEqual(r["status"], "INVALID_RESPONSE")

    def test_invalid_json(self):
        bad = io.BytesIO(b"not json")
        bad.status = 200
        with mock.patch.object(urllib.request, "urlopen", return_value=_CtxResp(bad)):
            r = self._run()
        self.assertEqual(r["status"], "INVALID_RESPONSE")


# ---------------------------------------------------------------------------
# 3. Security: secrets never appear anywhere in the result
# ---------------------------------------------------------------------------
class TestAgentRouterSecretRedaction(unittest.TestCase):
    def test_key_never_leaves_result_on_any_outcome(self):
        secret = "ghp_SUPERSECRETVALUE1234567890"
        cfg = _cfg(AGENTROUTER_API_KEYS=secret, AGENTROUTER_MODELS="m1")
        outcomes = [
            _CtxResp(_http_response({"choices": [{"message": {"content": "ok"}}]})),
            urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"{}")),
            urllib.error.URLError("boom"),
        ]
        for outcome in outcomes:
            side_effect = outcome if isinstance(outcome, Exception) else None
            with mock.patch.object(urllib.request, "urlopen",
                                   return_value=(None if side_effect else outcome),
                                   side_effect=side_effect):
                r = live.check_agentrouter(cfg)
            blob = json.dumps(r)
            self.assertNotIn(secret, blob)
            self.assertNotIn("Bearer", blob)

    def test_events_never_carry_the_secret(self):
        secret = "ghp_ANOTHERSECRETVALUE0987654321"
        cfg = _cfg(AGENTROUTER_API_KEYS=secret)
        stack = make_stack()
        events = stack["events"]
        with mock.patch.object(urllib.request, "urlopen",
                               return_value=_CtxResp(_http_response(
                                   {"choices": [{"message": {"content": "ok"}}]}))):
            live.check_agentrouter(cfg, events=events)
        hist = events.history(limit=20)
        blob = json.dumps(hist)
        self.assertNotIn(secret, blob)
        kinds = {e["kind"] for e in hist}
        self.assertIn("agentrouter.success", kinds)


# ---------------------------------------------------------------------------
# 4. Routing decision visibility: a routing decision logs provider/model/task
# ---------------------------------------------------------------------------
class _FakeProvider(AIProvider):
    name = "fakeprov"
    models = ["fake-model"]

    def __init__(self):
        super().__init__()
        self.pool = _TruthyPool()

    def chat(self, messages, model=None, max_tokens=500):
        return "hello from fake"

    def health_check(self):
        return True


class _TruthyPool:
    def __bool__(self):
        return True


class TestRoutingDecisionEvents(unittest.TestCase):
    def test_route_request_emits_router_decision_with_provider_model_task(self):
        stack = make_stack()
        events = stack["events"]
        router = AgentRouter([_FakeProvider()], max_retries=0)
        router.attach_events(events)
        req = RoutingRequest(task_type="simple_chat", messages=[{"role": "user", "content": "hi"}])
        rr = router.route_request(req)
        self.assertTrue(rr.ok)
        hist = events.history(limit=20)
        decisions = [e for e in hist if e["kind"] == "router.decision"]
        self.assertTrue(decisions, "expected a router.decision event")
        d = decisions[0]["data"]
        self.assertEqual(d.get("provider"), "fakeprov")
        self.assertEqual(d.get("model"), "fake-model")
        self.assertEqual(d.get("task"), "simple_chat")
        requests_ = [e for e in hist if e["kind"] == "router.request"]
        self.assertTrue(requests_, "expected a router.request event")


# ---------------------------------------------------------------------------
# 5. UI/API: the endpoint exists and responds
# ---------------------------------------------------------------------------
class TestAgentRouterHealthEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stack = make_stack()
        stack["config"].set("AGENTROUTER_API_KEYS", "")
        from astra.agent import Agent
        agent = Agent(stack.get("plugins", []) or [])
        cls.server = AstraServer(("127.0.0.1", 0), stack["store"], agent,
                                 getattr(agent, "plugins", []) or [], stack=stack)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_health_endpoint_not_configured(self):
        with urllib.request.urlopen(self.base + "/api/agentrouter/health") as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode())
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["status"], "NOT_CONFIGURED")
        self.assertFalse(body["data"]["configured"])

    def test_health_endpoint_post_also_works(self):
        req = urllib.request.Request(
            self.base + "/api/agentrouter/health", method="POST",
            data=b"{}", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode())
        self.assertTrue(body["ok"])
        self.assertIn("status", body["data"])


if __name__ == "__main__":
    unittest.main()
