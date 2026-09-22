"""End-to-end functional flows: real HTTP API over the real stack.

Unlike the unit tests, these drive the running FastAPI/ASGI app (LiveServer)
against a *real* AstraRouter + ChatPipeline, with a fake provider used only
where a network call to a real model would otherwise be needed. The point is
to prove the components are wired together: request -> route -> handler ->
agent/pipeline -> router -> provider -> normalized response -> JSON, plus the
persistence/feature endpoints the front end actually calls.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import unittest

from astra.ai.provider import AIProvider
from astra.core.exceptions import ProviderError
from tests.helpers import make_stack, LiveServer


class FakeProvider(AIProvider):
    """A provider that answers locally — same interface the router calls."""

    def __init__(self, name="fake", models=("fake-1", "fake-2"),
                 text="hello from fake", fail=False, base_url="http://fake.local/v1"):
        super().__init__()
        self.name = name
        self.models = list(models)
        self.base_url = base_url
        self._text = text
        self._fail = fail
        self.calls = 0

    def health_check(self) -> bool:
        return True

    def chat(self, messages, model=None, max_tokens=500,
             response_format=None) -> str:
        self.calls += 1
        if self._fail:
            raise ProviderError(f"{self.name} is down")
        return self._text


def _request(base, path, method="GET", body=None, token=""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        with e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else {})


class _Base(unittest.TestCase):
    def setUp(self):
        self.stack = make_stack()
        self.provider = FakeProvider()
        self.stack["router"].add(self.provider)
        self.srv = LiveServer(stack=self.stack)
        self.addCleanup(self._stop)

    def _stop(self):
        self.srv.stop()
        try:
            self.stack["store"].close()
        except Exception:
            pass

    def post(self, path, body=None, token=""):
        return _request(self.srv.base, path, "POST", body, token)

    def get(self, path, token=""):
        return _request(self.srv.base, path, "GET", None, token)

    def delete(self, path, token=""):
        return _request(self.srv.base, path, "DELETE", None, token)

    def patch(self, path, body=None, token=""):
        return _request(self.srv.base, path, "PATCH", body, token)


class TestChatFlow(_Base):
    def test_chat_request_reaches_provider_and_returns_normalized_reply(self):
        st, body = self.post("/api/chat", {"message": "hello there"})
        self.assertEqual(st, 200)
        data = body["data"]
        self.assertTrue(data["ok"])
        self.assertEqual(data["reply"], "hello from fake")
        self.assertEqual(data["data"]["served_by"], "fake/fake-1")
        self.assertGreaterEqual(self.provider.calls, 1)
        self.assertIn("conversation_id", data)

    def test_chat_is_persisted_to_history(self):
        self.post("/api/chat", {"message": "remember me"})
        st, body = self.get("/api/chat/history")
        self.assertEqual(st, 200)
        roles = [m["role"] for m in body["data"]["messages"]]
        self.assertIn("user", roles)
        self.assertIn("ai", roles)
        texts = [m["text"] for m in body["data"]["messages"]]
        self.assertIn("remember me", texts)
        self.assertIn("hello from fake", texts)

    def test_chat_failover_when_first_provider_fails(self):
        dead = FakeProvider(name="dead", models=("dead-1",), fail=True,
                            base_url="http://dead.local/v1")
        self.stack["router"].add(dead)
        st, body = self.post("/api/chat", {"message": "still works?"})
        self.assertEqual(st, 200)
        self.assertTrue(body["data"]["ok"])
        self.assertEqual(body["data"]["reply"], "hello from fake")

    def test_no_provider_is_graceful_not_500(self):
        stack = make_stack()
        srv = LiveServer(stack=stack)
        try:
            st, body = _request(srv.base, "/api/chat", "POST", {"message": "hi"})
            self.assertEqual(st, 200)
            self.assertFalse(body["data"]["ok"])
            self.assertIn("Provider", body["data"]["reply"])
        finally:
            srv.stop()
            stack["store"].close()

    def test_empty_message_does_not_error(self):
        st, body = self.post("/api/chat", {"message": ""})
        self.assertEqual(st, 200)
        self.assertFalse(body["data"]["ok"])

    def test_conversations_lifecycle(self):
        st, body = self.post("/api/chat/conversations", {})
        self.assertEqual(st, 200)
        cid = body["data"]["id"]
        st, listing = self.get("/api/chat/conversations")
        self.assertEqual(st, 200)
        self.assertTrue(any(c["id"] == cid for c in listing["data"]))
        st, _ = self.get(f"/api/chat/conversations/{cid}")
        self.assertEqual(st, 200)
        st, _ = self.delete(f"/api/chat/conversations/{cid}")
        self.assertEqual(st, 200)

    def test_chat_history_bad_param_is_400(self):
        st, body = self.get("/api/chat/history?after_id=not-an-int")
        self.assertEqual(st, 400)
        self.assertFalse(body["ok"])


class TestFeatureEndpoints(_Base):
    def test_memory_crud_and_search(self):
        st, body = self.post("/api/memory", {"content": "the sky is blue",
                                             "category": "fact"})
        self.assertEqual(st, 201)
        mid = body["data"]["id"]
        st, body = self.get("/api/memory?limit=10")
        self.assertEqual(st, 200)
        self.assertTrue(any(m["id"] == mid for m in body["data"]))
        st, body = self.get("/api/memory/search?query=sky")
        self.assertEqual(st, 200)
        self.assertGreaterEqual(len(body["data"]), 1)
        st, _ = self.delete(f"/api/memory/{mid}")
        self.assertEqual(st, 200)

    def test_memory_requires_content(self):
        st, body = self.post("/api/memory", {"content": "   "})
        self.assertEqual(st, 400)
        self.assertFalse(body["ok"])

    def test_tasks_create_list_get(self):
        st, body = self.post("/api/tasks", {"goal": "do a thing",
                                            "type": "manual"})
        self.assertEqual(st, 201)
        tid = body["data"]["id"]
        st, body = self.get("/api/tasks")
        self.assertEqual(st, 200)
        self.assertTrue(any(t["id"] == tid for t in body["data"]))
        st, body = self.get(f"/api/tasks/{tid}")
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["id"], tid)

    def test_task_missing_goal_is_400(self):
        st, _ = self.post("/api/tasks", {"goal": ""})
        self.assertEqual(st, 400)

    def test_workflow_define_run_and_runs(self):
        st, body = self.post("/api/workflows", {
            "name": "e2e", "steps": [{"tool": "get_health", "name": "h"}]})
        self.assertEqual(st, 201)
        wid = body["data"]["id"]
        st, body = self.post(f"/api/workflows/{wid}/run", {"params": {}})
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["status"], "completed")
        st, body = self.get("/api/workflows/runs")
        self.assertEqual(st, 200)
        self.assertTrue(len(body["data"]) >= 1)

    def test_workflow_bad_steps_is_400(self):
        st, _ = self.post("/api/workflows", {"name": "x", "steps": "nope"})
        self.assertEqual(st, 400)

    def test_workflow_runs_context_dependent_tool(self):
        """A workflow step naming a context-dependent tool (remember) must
        actually run — previously ctx was None and the step errored."""
        st, body = self.post("/api/workflows", {
            "name": "mem step",
            "steps": [{"id": "s1", "tool": "remember",
                       "params": {"content": "workflow memory note"}}]})
        self.assertEqual(st, 201)
        wid = body["data"]["id"]
        st, body = self.post(f"/api/workflows/{wid}/run", {"params": {}})
        self.assertEqual(st, 200)
        step = body["data"]["results"]["s1"]
        self.assertTrue(step.get("ok"), step)
        # and the memory is really persisted
        st, body = self.get("/api/memory/search?query=workflow")
        self.assertEqual(st, 200)
        self.assertTrue(any("workflow memory note" in (m.get("content") or "")
                            for m in body["data"]))

    def test_workflow_runs_file_tools(self):
        """write_file then read_file as workflow steps (the Files feature)."""
        import tempfile
        from astra.tools import builtins
        ws = tempfile.mkdtemp()
        saved = builtins.WORKSPACE
        builtins.WORKSPACE = ws
        self.addCleanup(lambda: setattr(builtins, "WORKSPACE", saved))
        st, body = self.post("/api/workflows", {
            "name": "files",
            "steps": [
                {"id": "w", "tool": "write_file",
                 "params": {"path": "note.txt", "content": "hello file"}},
                {"id": "r", "tool": "read_file", "depends_on": ["w"],
                 "params": {"path": "{{w.output.path}}"}},
            ]})
        self.assertEqual(st, 201)
        wid = body["data"]["id"]
        st, body = self.post(f"/api/workflows/{wid}/run", {"params": {}})
        self.assertEqual(st, 200)
        self.assertTrue(body["data"]["results"]["w"].get("ok"))
        self.assertEqual(body["data"]["results"]["r"]["output"]["content"],
                         "hello file")

    def test_sse_stream_returns_event_stream(self):
        import socket
        import urllib.request
        req = urllib.request.Request(
            self.srv.base + "/api/events/stream?after_id=0")
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
            # emit an event, then read at least one SSE frame
            self.post("/api/chat", {"message": "sse ping"})
            buf = b""
            try:
                while b"\n\n" not in buf:
                    chunk = resp.read(1)
                    if not chunk:
                        break
                    buf += chunk
            except (socket.timeout, TimeoutError):
                pass
            self.assertIn(b"data:", buf)

    def test_schedules_disabled_is_structured_400(self):
        """Scheduler off (the default): GET is an empty list, POST is a
        structured 400 — not a bare 404 that hides why nothing was created."""
        st, body = self.get("/api/schedules")
        self.assertEqual(st, 200)
        self.assertEqual(body["data"], [])
        st, body = self.post("/api/schedules", {"name": "s", "kind": "daily",
                                                "value": "09:00"})
        self.assertEqual(st, 400)
        self.assertEqual(body["error_code"], "bad_request")

    def test_schedules_crud_with_scheduler_enabled(self):
        stack = make_stack(with_scheduler=True)
        srv = LiveServer(stack=stack)
        self.addCleanup(stack["store"].close)
        self.addCleanup(stack["scheduler"].stop)
        self.addCleanup(srv.stop)
        base = srv.base
        st, body = _request(base, "/api/schedules", "POST",
                            {"name": "s", "kind": "daily", "value": "09:00"})
        self.assertEqual(st, 201)
        sid = body["data"]["id"]
        st, body = _request(base, "/api/schedules", "GET")
        self.assertEqual(st, 200)
        self.assertTrue(any(s["id"] == sid for s in body["data"]))
        st, body = _request(base, f"/api/schedules/{sid}", "PATCH",
                            {"enabled": False})
        self.assertEqual(st, 200)
        self.assertFalse(body["data"]["enabled"])
        st, _ = _request(base, f"/api/schedules/{sid}", "DELETE")
        self.assertEqual(st, 200)

    def test_tools_listing_and_health(self):
        st, body = self.get("/api/tools")
        self.assertEqual(st, 200)
        names = {t["name"] for t in body["data"]["tools"]}
        self.assertIn("remember", names)
        self.assertIn("get_health", names)
        st, body = self.get("/api/health")
        self.assertEqual(st, 200)
        self.assertTrue(body["data"]["ok"])

    def test_events_and_export_import(self):
        self.post("/api/chat", {"message": "generate an event"})
        st, body = self.get("/api/events?limit=50")
        self.assertEqual(st, 200)
        self.assertIsInstance(body["data"], list)
        st, body = self.get("/api/export")
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["_app"], "Astra AI Agent")
        st, body = self.post("/api/import", {"data": {"_app": "x"}})
        self.assertEqual(st, 200)


class TestProviderAndRouterApi(_Base):
    def test_providers_list_shows_routable_provider(self):
        st, body = self.get("/api/providers")
        self.assertEqual(st, 200)
        self.assertIn("fake", body["data"]["providers"])

    def test_per_model_provider_test_uses_real_provider(self):
        st, body = self.post("/api/v1/providers/fake/test/fake-1")
        self.assertEqual(st, 200)
        self.assertTrue(body["data"]["ok"])
        self.assertEqual(body["data"]["model"], "fake-1")

    def test_provider_admin_reset_health(self):
        st, body = self.post("/api/v1/providers/fake/reset-health")
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["provider"], "fake")

    def test_provider_disable_then_enable(self):
        st, _ = self.post("/api/v1/providers/fake/disable")
        self.assertEqual(st, 200)
        st, body = self.get("/api/providers")
        self.assertIn("fake", body["data"]["down"])
        st, _ = self.post("/api/v1/providers/fake/enable")
        self.assertEqual(st, 200)

    def test_models_and_router_stats(self):
        st, body = self.get("/api/v1/models")
        self.assertEqual(st, 200)
        self.assertIn("models", body["data"])
        st, body = self.get("/api/v1/router/stats")
        self.assertEqual(st, 200)
        self.assertIn("routing", body["data"])
        self.assertIn("task", body["data"])

    def test_gateway_health_reports_not_configured(self):
        st, body = self.get("/api/gateway/health")
        self.assertEqual(st, 200)
        self.assertIn("state", body["data"])


class TestWeb3Api(_Base):
    def test_policy_is_readable(self):
        st, body = self.get("/api/v1/web3/transaction-policy")
        self.assertEqual(st, 200)
        self.assertIn(body["data"]["mode"], ("CONFIRM", "AUTO"))
        self.assertIn("policy", body["data"])

    def test_transactions_list_is_empty_and_safe(self):
        st, body = self.get("/api/v1/web3/transactions")
        self.assertEqual(st, 200)
        self.assertIn("transactions", body["data"])

    def test_policy_mode_change_requires_operator_token(self):
        # no ASTRA_TOKEN set -> mode changes are refused (operator only)
        st, body = self.post("/api/v1/web3/transaction-policy/mode",
                             {"mode": "AUTO"})
        self.assertEqual(st, 403)

    def test_policy_mode_change_with_token(self):
        self.srv.site.operator_token = "sekrit"
        st, body = self.post("/api/v1/web3/transaction-policy/mode",
                             {"mode": "AUTO"}, token="sekrit")
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["mode"], "AUTO")

    def test_reject_unknown_tx_is_4xx_not_500(self):
        self.srv.site.operator_token = "sekrit"
        st, body = self.post("/api/v1/web3/transactions/tx-nope/reject",
                             {"reason": "n/a"}, token="sekrit")
        self.assertIn(st, (400, 404))
        self.assertFalse(body["ok"])

    def test_prepare_list_reject_flow(self):
        """Full CONFIRM-mode lifecycle short of signing: prepare (no funds,
        no broadcast), read it back, reject it, and refuse a second reject."""
        self.srv.site.operator_token = "sekrit"
        from astra.web3.policy import TxRequest
        rec = self.stack["tx_manager"].create(
            TxRequest("default", "0x" + "35" * 20, 10 ** 15))
        tx_id = rec["tx_id"]
        self.assertEqual(rec["status"], "PREPARED")
        st, body = self.get(f"/api/v1/web3/transactions/{tx_id}", token="sekrit")
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["tx_id"], tx_id)
        st, body = self.post(f"/api/v1/web3/transactions/{tx_id}/reject",
                             {"reason": "e2e"}, token="sekrit")
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["status"], "REJECTED")
        # a second reject of the now-terminal tx is refused, not silently ok
        st, body = self.post(f"/api/v1/web3/transactions/{tx_id}/reject",
                             {"reason": "again"}, token="sekrit")
        self.assertEqual(st, 400)
        self.assertFalse(body["ok"])

    def test_authorize_unknown_tx_is_4xx(self):
        self.srv.site.operator_token = "sekrit"
        st, body = self.post("/api/v1/web3/transactions/tx-nope/authorize",
                             {}, token="sekrit")
        self.assertIn(st, (400, 404))
        self.assertFalse(body["ok"])

    def test_auth_gate_rejects_missing_token(self):
        self.srv.site.operator_token = "sekrit"
        st, body = self.get("/api/health")
        self.assertEqual(st, 401)
        st, _ = self.get("/api/health", token="sekrit")
        self.assertEqual(st, 200)


class TestToolsAndResearch(_Base):
    def test_fetch_url_refuses_private_target(self):
        out = self.stack["registry"].execute(
            "fetch_url", {"url": "http://127.0.0.1:9/secret"}, ctx=None)
        self.assertTrue(out["ok"])           # the tool returns, never raises
        text = out["result"]["text"]
        self.assertIn("problem", text.lower())

    def test_get_health_tool_reports_running_stack(self):
        out = self.stack["registry"].execute("get_health", {}, ctx=None)
        self.assertTrue(out["ok"])
        self.assertTrue(out["result"])

    def test_invalid_json_body_is_not_500(self):
        import urllib.request
        req = urllib.request.Request(
            self.srv.base + "/api/memory", data=b"{not json",
            method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(resp.status, 400)
        except urllib.error.HTTPError as e:
            with e:
                self.assertLess(e.code, 500)

    def test_models_refresh_endpoint(self):
        st, body = self.post("/api/v1/models/refresh", {})
        self.assertEqual(st, 200)

    def test_generated_document_is_downloadable(self):
        """generate_document -> artifact -> the exact URL the front end
        builds for a download must serve the bytes back."""
        import urllib.request
        out = self.stack["registry"].execute(
            "generate_document",
            {"content": "hello artifact", "format": "txt", "title": "E2E Doc"},
            ctx=None)
        self.assertTrue(out["ok"])
        art = out["result"]["artifact"]
        url = (self.srv.base + "/api/v1/artifacts/"
               + art["id"] + "/" + art["filename"])
        with urllib.request.urlopen(url, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn(b"hello artifact", resp.read())

    def test_artifact_path_traversal_is_refused(self):
        st, _ = self.get("/api/v1/artifacts/x/..%2F..%2Fetc%2Fpasswd")
        self.assertIn(st, (400, 403, 404))
        self.assertNotEqual(st, 200)


class TestFrontendBackendContract(_Base):
    """Every endpoint the SPA calls must resolve to a real backend route.

    The backend answers an unmatched /api path with error "unknown route";
    a matched route that legitimately has nothing to return (missing chat,
    tx, artifact) says something else. So "not unknown route" is proof the
    SPA is not calling a dead endpoint.
    """

    # (method, path, body) — mirrors the fetch/api() calls in
    # static/js/astra.js. Dynamic ids use the fake provider / live ids.
    CALLS = [
        ("GET", "/api/manifest", None),
        ("GET", "/api/dashboard", None),
        ("GET", "/api/chat/history", None),
        ("GET", "/api/chat/history?after_id=0", None),
        ("POST", "/api/chat/conversations", {}),
        ("GET", "/api/chat/conversations", None),
        ("POST", "/api/chat/resume", {"execution_id": "x", "allow": True}),
        ("POST", "/api/chat", {"message": "hi"}),
        ("GET", "/api/export", None),
        ("POST", "/api/import", {"data": {}}),
        ("GET", "/api/events?limit=500", None),
        ("GET", "/api/events?limit=30", None),
        ("GET", "/api/events/last", None),
        # 🔀 Agent Workflow tab (astra.js loaders.workflows)
        ("GET", "/api/workflows", None),
        ("POST", "/api/workflows", {"name": "contract wf", "steps": []}),
        ("GET", "/api/workflows/runs", None),
        ("PATCH", "/api/workflows/999999", {"name": "x"}),
        ("DELETE", "/api/workflows/999999", None),
        ("GET", "/api/schedules", None),
        ("GET", "/api/tools", None),
        ("GET", "/api/providers", None),
        ("POST", "/api/v1/providers/fake/reset-health", {}),
        ("POST", "/api/v1/providers/fake/test/fake-1", {}),
        ("POST", "/api/v1/gateway/astra-gw-gemini/test/gemini-pro", {}),
        ("GET", "/api/v1/models", None),
        ("GET", "/api/v1/router/stats", None),
        ("GET", "/api/v1/web3/transaction-policy", None),
        ("GET", "/api/v1/web3/transactions", None),
    ]

    def test_every_spa_endpoint_is_routed(self):
        for method, path, body in self.CALLS:
            st, resp = _request(self.srv.base, path, method, body)
            self.assertNotEqual(
                resp.get("error"), "unknown route",
                f"{method} {path} -> no backend route (status {st})")
            self.assertLess(st, 500, f"{method} {path} -> {st} {resp}")

    def test_chat_conversation_id_path_is_routed(self):
        st, body = self.post("/api/chat/conversations", {})
        cid = body["data"]["id"]
        st, body = _request(self.srv.base, f"/api/chat/conversations/{cid}", "GET")
        self.assertEqual(st, 200)
        st, body = _request(self.srv.base, f"/api/chat/conversations/{cid}", "DELETE")
        self.assertEqual(st, 200)


if __name__ == "__main__":
    unittest.main()
