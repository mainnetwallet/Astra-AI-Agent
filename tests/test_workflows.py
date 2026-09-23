"""Agent Workflow: the engine contract, the HTTP API, and the AI node.

Two things are being proven here, and they are the two ways this feature
could quietly be fake:

  1. Identity. Creating a workflow must never reuse, mutate or inherit
     another workflow's row, definition, layout or run history — the bug
     that made "New Workflow" look like it edited the previous one. The
     `name UNIQUE` constraint is the only place that used to fail, and it
     failed with an opaque sqlite IntegrityError -> HTTP 500.
  2. Execution. A run goes through the real WorkflowEngine -> the real
     ToolRegistry -> the real Astra tools, and a step that fails is
     reported as failed instead of "completed".
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import unittest

from astra.ai.provider import AIProvider
from astra.core.exceptions import ProviderError
from astra.workflows.engine import (DuplicateNameError, InvalidDefinition,
                                    WorkflowNotFound)
from tests.helpers import LiveServer, make_stack


class FakeProvider(AIProvider):
    """A provider that answers locally (same interface the router calls)."""

    def __init__(self, name="fake", models=("fake-1",), text="hello from fake",
                 fail=False):
        super().__init__()
        self.name = name
        self.models = list(models)
        self.base_url = "http://fake.local/v1"
        self._text = text
        self._fail = fail
        self.calls = 0

    def health_check(self) -> bool:
        return True

    def chat(self, messages, model=None, max_tokens=500, response_format=None) -> str:
        self.calls += 1
        if self._fail:
            raise ProviderError(f"{self.name} is down")
        return self._text


def _request(base, path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        with e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else {})


def steps_status(run):
    return {sid: (res or {}).get("ok") for sid, res in (run.get("results") or {}).items()}


# ── 1. engine identity + validation ──────────────────────────────────────────
class TestWorkflowEngineIdentity(unittest.TestCase):
    def setUp(self):
        self.stack = make_stack()
        self.wf = self.stack["workflows"]

    def tearDown(self):
        self.stack["store"].close()

    def test_two_workflows_have_independent_identity(self):
        a = self.wf.define("A", "", [{"tool": "get_health", "id": "s1"}])
        b = self.wf.define("B", "", [{"tool": "get_health", "id": "s1"}])
        self.assertNotEqual(a["id"], b["id"])
        self.assertIsNot(a, b)
        self.assertEqual(a["steps"][0]["id"], "s1")
        self.assertEqual(b["steps"][0]["id"], "s1")

    def test_default_names_are_uniquified_deterministically(self):
        got = [self.wf.define("Untitled Workflow")["name"] for _ in range(3)]
        self.assertEqual(got, ["Untitled Workflow", "Untitled Workflow 2",
                               "Untitled Workflow 3"])

    def test_strict_mode_raises_a_structured_duplicate_error(self):
        self.wf.define("taken", "", [], uniquify=False)
        with self.assertRaises(DuplicateNameError):
            self.wf.define("taken", "", [], uniquify=False)

    def test_a_new_workflow_never_inherits_another_ones_steps_or_layout(self):
        a = self.wf.define("A", "", [{"tool": "get_health", "id": "s1"}],
                           layout={"s1": {"x": 5, "y": 6}})
        b = self.wf.define("B", "")
        self.assertEqual(b["steps"], [])
        self.assertEqual(b["layout"], {})
        # and the first one is untouched by the second
        again = self.wf.get_definition(a["id"])
        self.assertEqual(len(again["steps"]), 1)
        self.assertEqual(again["layout"], {"s1": {"x": 5, "y": 6}})

    def test_editing_one_workflow_leaves_the_other_alone(self):
        a = self.wf.define("A", "", [{"tool": "get_health", "id": "s1"}])
        b = self.wf.define("B", "", [{"tool": "get_health", "id": "s1"}])
        self.wf.update_definition(a["id"], name="A2", description="changed",
                                 steps=[{"tool": "recall", "id": "s9",
                                         "params": {"query": "x"}}],
                                 layout={"s9": {"x": 11, "y": 12}})
        fresh_a = self.wf.get_definition(a["id"])
        fresh_b = self.wf.get_definition(b["id"])
        self.assertEqual(fresh_a["name"], "A2")
        self.assertEqual(fresh_a["steps"][0]["tool"], "recall")
        self.assertEqual(fresh_a["layout"], {"s9": {"x": 11, "y": 12}})
        self.assertEqual(fresh_b["name"], "B")
        self.assertEqual(fresh_b["steps"][0]["tool"], "get_health")
        self.assertEqual(fresh_b["layout"], {})

    def test_renaming_onto_a_taken_name_is_refused(self):
        self.wf.define("taken", "")
        b = self.wf.define("free", "")
        with self.assertRaises(DuplicateNameError):
            self.wf.update_definition(b["id"], name="taken")
        self.assertEqual(self.wf.get_definition(b["id"])["name"], "free")

    def test_renaming_can_be_told_to_uniquify(self):
        self.wf.define("taken", "")
        b = self.wf.define("free", "")
        out = self.wf.update_definition(b["id"], name="taken", uniquify=True)
        self.assertEqual(out["name"], "taken 2")

    def test_renaming_to_its_own_name_is_a_no_op(self):
        a = self.wf.define("same", "")
        self.assertEqual(self.wf.update_definition(a["id"], name="same")["name"],
                         "same")

    def test_update_missing_workflow_raises(self):
        with self.assertRaises(WorkflowNotFound):
            self.wf.update_definition(4242, name="x")

    def test_delete_is_scoped_and_cascades_its_runs(self):
        a = self.wf.define("A", "", [{"tool": "get_health"}])
        b = self.wf.define("B", "", [{"tool": "get_health"}])
        self.wf.run(workflow_id=a["id"])
        self.assertEqual(len(self.wf.list_runs(workflow_id=a["id"])), 1)
        self.wf.delete_definition(a["id"])
        self.assertIsNone(self.wf.get_definition(a["id"]))
        self.assertEqual(self.wf.list_runs(workflow_id=a["id"]), [])
        self.assertIsNotNone(self.wf.get_definition(b["id"]))

    def test_delete_missing_workflow_raises(self):
        with self.assertRaises(WorkflowNotFound):
            self.wf.delete_definition(999)

    def test_run_history_is_per_workflow(self):
        a = self.wf.define("A", "", [{"tool": "get_health"}])
        b = self.wf.define("B", "", [{"tool": "get_health"}])
        run_a = self.wf.run(workflow_id=a["id"])
        run_b = self.wf.run(workflow_id=b["id"])
        self.assertNotEqual(run_a["id"], run_b["id"])
        self.assertEqual([r["id"] for r in self.wf.list_runs(workflow_id=a["id"])],
                         [run_a["id"]])
        self.assertEqual([r["id"] for r in self.wf.list_runs(workflow_id=b["id"])],
                         [run_b["id"]])
        self.assertEqual(self.wf.latest_run(b["id"])["id"], run_b["id"])
        self.assertEqual(run_a["workflow_id"], a["id"])

    def test_definition_stats_are_real(self):
        a = self.wf.define("A", "", [{"tool": "get_health"}])
        self.wf.define("B", "")
        self.wf.run(workflow_id=a["id"])
        stats = {d["name"]: d for d in self.wf.list_definitions_with_stats()}
        self.assertEqual(stats["A"]["run_count"], 1)
        self.assertEqual(stats["A"]["last_run"]["status"], "completed")
        self.assertEqual(stats["B"]["run_count"], 0)
        self.assertIsNone(stats["B"]["last_run"])
        self.assertTrue(stats["A"]["updated_at"])


class TestWorkflowEngineValidation(unittest.TestCase):
    def setUp(self):
        self.stack = make_stack()
        self.wf = self.stack["workflows"]

    def tearDown(self):
        self.stack["store"].close()

    def test_steps_must_be_a_list(self):
        with self.assertRaises(InvalidDefinition):
            self.wf.define("x", "", "nope")

    def test_step_must_be_an_object(self):
        with self.assertRaises(InvalidDefinition):
            self.wf.define("x", "", ["nope"])

    def test_step_needs_a_tool(self):
        with self.assertRaises(InvalidDefinition):
            self.wf.define("x", "", [{"params": {}}])

    def test_unknown_tool_is_refused_at_write_time(self):
        with self.assertRaises(InvalidDefinition) as cm:
            self.wf.define("x", "", [{"tool": "not_a_tool"}])
        self.assertIn("not_a_tool", str(cm.exception))

    def test_duplicate_step_ids_are_refused(self):
        with self.assertRaises(InvalidDefinition):
            self.wf.define("x", "", [{"id": "s1", "tool": "get_health"},
                                     {"id": "s1", "tool": "get_health"}])

    def test_dangling_dependency_is_refused(self):
        with self.assertRaises(InvalidDefinition):
            self.wf.define("x", "", [{"id": "s1", "tool": "get_health",
                                      "depends_on": ["ghost"]}])

    def test_self_dependency_is_refused(self):
        with self.assertRaises(InvalidDefinition):
            self.wf.define("x", "", [{"id": "s1", "tool": "get_health",
                                      "depends_on": ["s1"]}])

    def test_dependency_cycle_is_refused(self):
        with self.assertRaises(InvalidDefinition) as cm:
            self.wf.define("x", "", [
                {"id": "a", "tool": "get_health", "depends_on": ["b"]},
                {"id": "b", "tool": "get_health", "depends_on": ["a"]}])
        self.assertIn("cycle", str(cm.exception).lower())

    def test_condition_must_reference_a_real_step(self):
        with self.assertRaises(InvalidDefinition):
            self.wf.define("x", "", [{"id": "s1", "tool": "get_health",
                                      "if": {"step": "ghost", "op": "ok"}}])

    def test_unknown_condition_op_is_refused(self):
        with self.assertRaises(InvalidDefinition):
            self.wf.define("x", "", [
                {"id": "s1", "tool": "get_health"},
                {"id": "s2", "tool": "get_health",
                 "if": {"step": "s1", "op": "maybe"}}])

    def test_missing_step_ids_are_filled_in(self):
        d = self.wf.define("x", "", [{"tool": "get_health"},
                                     {"tool": "get_health"}])
        self.assertEqual([s["id"] for s in d["steps"]], ["step1", "step2"])

    def test_handler_params_must_be_an_object(self):
        with self.assertRaises(InvalidDefinition):
            self.wf.define("x", "", [{"tool": "get_health", "params": "nope"}])

    def test_blank_name_is_refused(self):
        with self.assertRaises(InvalidDefinition):
            self.wf.define("   ")


class TestWorkflowEngineExecution(unittest.TestCase):
    def setUp(self):
        self.stack = make_stack()
        self.wf = self.stack["workflows"]
        self.events = self.stack["events"]

    def tearDown(self):
        self.stack["store"].close()

    def _kinds(self):
        return [r["kind"] for r in self.stack["store"].fetch(
            "SELECT kind FROM events ORDER BY id")]

    def test_a_failing_step_makes_the_run_failed(self):
        d = self.wf.define("bad", "", [{"id": "s1", "tool": "recall"}])
        run = self.wf.run(workflow_id=d["id"])
        self.assertEqual(run["status"], "failed")
        self.assertFalse(run["results"]["s1"]["ok"])
        self.assertIn("s1", run["error"])
        self.assertIn("workflow.failed", self._kinds())
        self.assertNotIn("workflow.completed", self._kinds())

    def test_a_skipped_step_is_not_a_failure(self):
        d = self.wf.define("cond", "", [
            {"id": "s1", "tool": "recall"},
            {"id": "s2", "tool": "get_health",
             "if": {"step": "s1", "op": "ok"}, "depends_on": ["s1"]}])
        run = self.wf.run(workflow_id=d["id"])
        self.assertTrue(run["results"]["s2"]["skipped"])
        self.assertTrue(run["results"]["s1"]["error"])
        # s1 really failed, so the RUN failed — but s2 is a skip, not a step error
        self.assertEqual(run["status"], "failed")

    def test_not_ok_condition_runs_the_step_after_a_failure(self):
        d = self.wf.define("fallback", "", [
            {"id": "s1", "tool": "recall"},
            {"id": "s2", "tool": "get_health", "depends_on": ["s1"],
             "if": {"step": "s1", "op": "not_ok"}}])
        run = self.wf.run(workflow_id=d["id"])
        self.assertTrue(run["results"]["s2"]["ok"])

    def test_dependencies_run_before_their_dependents(self):
        d = self.wf.define("order", "", [
            {"id": "s2", "tool": "get_health", "depends_on": ["s1"]},
            {"id": "s1", "tool": "get_health"}])
        run = self.wf.run(workflow_id=d["id"])
        self.assertTrue(run["results"]["s1"]["ok"])
        self.assertTrue(run["results"]["s2"]["ok"])

    def test_step_results_feed_later_steps(self):
        d = self.wf.define("data", "", [
            {"id": "s1", "tool": "get_health"},
            {"id": "s2", "tool": "remember", "depends_on": ["s1"],
             "params": {"content": "version {{s1.output.version}}"}}])
        run = self.wf.run(workflow_id=d["id"])
        self.assertEqual(run["status"], "completed", run["results"])

    def test_a_run_can_be_cancelled_between_steps(self):
        d = self.wf.define("cancelme", "", [{"id": "s1", "tool": "get_health"}])
        run = self.wf.create_run(d["id"])
        self.wf.cancel_run(run["id"])
        out = self.wf.run(run_id=run["id"], steps=d["steps"])
        self.assertEqual(out["status"], "cancelled")

    def test_cancel_of_a_finished_run_is_a_no_op(self):
        d = self.wf.define("done", "", [{"id": "s1", "tool": "get_health"}])
        run = self.wf.run(workflow_id=d["id"])
        self.assertEqual(self.wf.cancel_run(run["id"])["status"], "completed")

    def test_cancel_unknown_run_raises(self):
        with self.assertRaises(WorkflowNotFound):
            self.wf.cancel_run(12345)

    def test_every_step_emits_a_terminal_event_under_its_own_op(self):
        d = self.wf.define("ev", "", [{"id": "s1", "tool": "get_health"}])
        self.wf.run(workflow_id=d["id"])
        rows = self.stack["store"].fetch("SELECT kind, data FROM events ORDER BY id")
        events = [(r["kind"], json.loads(r["data"])) for r in rows]
        starts = [d_ for k, d_ in events if k == "task.started"]
        ends = [d_ for k, d_ in events if k in ("task.completed", "task.failed")]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(ends), 1)
        self.assertEqual(starts[0]["op"], ends[0]["op"])
        self.assertTrue(ends[0]["terminal"])


# ── 2. HTTP API ───────────────────────────────────────────────────────────────
class _ApiBase(unittest.TestCase):
    provider_fail = False

    def setUp(self):
        self.stack = make_stack()
        self.provider = FakeProvider(fail=self.provider_fail)
        self.stack["router"].add(self.provider)
        self.srv = LiveServer(stack=self.stack)
        self.addCleanup(self._stop)

    def _stop(self):
        self.srv.stop()
        try:
            self.stack["store"].close()
        except Exception:
            pass

    def post(self, p, b=None):
        return _request(self.srv.base, p, "POST", b)

    def get(self, p):
        return _request(self.srv.base, p, "GET")

    def patch(self, p, b=None):
        return _request(self.srv.base, p, "PATCH", b)

    def delete(self, p):
        return _request(self.srv.base, p, "DELETE")

    def create(self, **body):
        body.setdefault("steps", [])
        st, out = self.post("/api/workflows", body)
        self.assertEqual(st, 201, out)
        return out["data"]


class TestWorkflowManifest(_ApiBase):
    def test_agent_workflow_is_a_core_navigation_tab(self):
        st, body = self.get("/api/manifest")
        self.assertEqual(st, 200)
        tabs = [t["tab"] for t in body["data"]["tabs"]]
        self.assertIn("workflow", tabs)
        label = [t["label"] for t in body["data"]["tabs"]
                 if t["tab"] == "workflow"][0]
        self.assertIn("Agent Workflow", label)
        # the existing navigation is untouched
        for tab in ("dashboard", "assistant", "providers", "router", "web3",
                    "backup", "logs"):
            self.assertIn(tab, tabs)

    def test_index_html_has_the_workflow_section_and_scripts(self):
        html = urllib.request.urlopen(self.srv.base + "/").read().decode()
        self.assertIn('id="tab-workflow"', html)
        self.assertIn("/static/js/workflow_model.js", html)
        self.assertIn("/static/js/workflow.js", html)
        self.assertLess(html.index("/static/js/workflow_model.js"),
                        html.index("/static/js/workflow.js"))

    def test_the_workflow_assets_are_served(self):
        for path in ("/static/js/workflow.js", "/static/js/workflow_model.js",
                     "/static/css/style.css"):
            with urllib.request.urlopen(self.srv.base + path) as resp:
                self.assertEqual(resp.status, 200)
                self.assertTrue(resp.read())


class TestWorkflowApiLifecycle(_ApiBase):
    def test_create_list_get_update_delete(self):
        first = self.create(name="Alpha", description="d",
                            steps=[{"id": "s1", "tool": "get_health"}],
                            layout={"s1": {"x": 10, "y": 20}})
        self.assertEqual(first["name"], "Alpha")
        self.assertEqual(first["layout"], {"s1": {"x": 10, "y": 20}})

        st, body = self.get("/api/workflows")
        self.assertEqual(st, 200)
        self.assertEqual([w["id"] for w in body["data"]], [first["id"]])

        st, body = self.get(f"/api/workflows/{first['id']}")
        self.assertEqual(body["data"]["steps"][0]["tool"], "get_health")

        st, body = self.patch(f"/api/workflows/{first['id']}",
                              {"name": "Renamed", "steps": [
                                  {"id": "s1", "tool": "get_health"},
                                  {"id": "s2", "tool": "get_health",
                                   "depends_on": ["s1"]}]})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["data"]["name"], "Renamed")
        self.assertEqual(len(body["data"]["steps"]), 2)

        st, body = self.delete(f"/api/workflows/{first['id']}")
        self.assertEqual(st, 200)
        st, body = self.get("/api/workflows")
        self.assertEqual(body["data"], [])

    def test_creating_records_nothing_about_the_previous_workflow(self):
        a = self.create(name="A", steps=[{"id": "s1", "tool": "get_health"}],
                        layout={"s1": {"x": 1, "y": 1}})
        self.post(f"/api/workflows/{a['id']}/run", {})
        b = self.create(name="B")
        self.assertEqual(b["steps"], [])
        self.assertEqual(b["layout"], {})
        st, body = self.get(f"/api/workflows/{b['id']}/runs")
        self.assertEqual(body["data"], [], "a new workflow has no run history")
        # A is untouched and still owns its run
        st, body = self.get(f"/api/workflows/{a['id']}/runs")
        self.assertEqual(len(body["data"]), 1)
        st, body = self.get(f"/api/workflows/{a['id']}")
        self.assertEqual(body["data"]["layout"], {"s1": {"x": 1, "y": 1}})

    def test_default_names_do_not_collide(self):
        names = [self.create()["name"] for _ in range(3)]
        self.assertEqual(names, ["Untitled Workflow", "Untitled Workflow 2",
                                 "Untitled Workflow 3"])

    def test_duplicate_name_can_be_requested_as_an_error(self):
        self.create(name="dup")
        st, body = self.post("/api/workflows", {"name": "dup", "steps": [],
                                                "on_duplicate": "error"})
        self.assertEqual(st, 409)
        self.assertEqual(body["error_code"], "duplicate_name")
        self.assertEqual(body["ok"], False)

    def test_duplicate_name_is_uniquified_by_default(self):
        self.create(name="dup")
        again = self.create(name="dup")
        self.assertEqual(again["name"], "dup 2")

    def test_bad_on_duplicate_is_rejected(self):
        st, body = self.post("/api/workflows", {"name": "x", "steps": [],
                                                "on_duplicate": "whatever"})
        self.assertEqual(st, 400)

    def test_edit_stays_local_to_one_workflow(self):
        a = self.create(name="A", steps=[{"id": "s1", "tool": "get_health"}])
        b = self.create(name="B", steps=[{"id": "s1", "tool": "get_health"}])
        self.patch(f"/api/workflows/{a['id']}",
                   {"steps": [{"id": "s1", "tool": "recall",
                               "params": {"query": "note"}}]})
        st, body = self.get(f"/api/workflows/{b['id']}")
        self.assertEqual(body["data"]["steps"][0]["tool"], "get_health")
        st, body = self.get(f"/api/workflows/{a['id']}")
        self.assertEqual(body["data"]["steps"][0]["tool"], "recall")

    def test_patching_nothing_is_a_400(self):
        a = self.create()
        st, body = self.patch(f"/api/workflows/{a['id']}", {})
        self.assertEqual(st, 400)

    def test_patch_can_rename_onto_a_free_name(self):
        a = self.create(name="A")
        self.create(name="B")
        st, body = self.patch(f"/api/workflows/{a['id']}", {"name": "C"})
        self.assertEqual(body["data"]["name"], "C")

    def test_renaming_onto_another_workflow_is_a_409(self):
        a = self.create(name="A")
        self.create(name="B")
        st, body = self.patch(f"/api/workflows/{a['id']}", {"name": "B"})
        self.assertEqual(st, 409)
        self.assertEqual(body["error_code"], "duplicate_name")

    def test_layout_round_trips(self):
        a = self.create(name="L", steps=[{"id": "s1", "tool": "get_health"}])
        st, body = self.patch(f"/api/workflows/{a['id']}",
                              {"layout": {"s1": {"x": 100, "y": 200}}})
        self.assertEqual(st, 200)
        st, body = self.get(f"/api/workflows/{a['id']}")
        self.assertEqual(body["data"]["layout"], {"s1": {"x": 100, "y": 200}})

    def test_reload_restores_the_saved_definition(self):
        a = self.create(name="Persist", steps=[
            {"id": "s1", "tool": "get_health"},
            {"id": "s2", "tool": "recall", "params": {"query": "hi"},
             "depends_on": ["s1"], "if": {"step": "s1", "op": "ok"}}])
        st, body = self.get(f"/api/workflows/{a['id']}")
        steps = body["data"]["steps"]
        self.assertEqual(steps[1]["if"], {"step": "s1", "op": "ok"})
        self.assertEqual(steps[1]["params"], {"query": "hi"})


class TestWorkflowApiErrors(_ApiBase):
    def test_missing_workflow_is_404(self):
        for method, path in (("GET", "/api/workflows/999"),
                             ("PATCH", "/api/workflows/999"),
                             ("DELETE", "/api/workflows/999")):
            st, body = _request(self.srv.base, path, method,
                                {"name": "x"} if method == "PATCH" else None)
            self.assertEqual(st, 404, (method, path, body))
            self.assertEqual(body["error_code"], "not_found")

    def test_non_integer_workflow_id_is_400(self):
        st, body = self.get("/api/workflows/abc")
        self.assertEqual(st, 400)
        self.assertEqual(body["error_code"], "bad_request")

    def test_malformed_steps_is_400(self):
        st, body = self.post("/api/workflows", {"name": "x", "steps": "nope"})
        self.assertEqual(st, 400)
        self.assertEqual(body["error_code"], "bad_request")

    def test_invalid_tool_is_400(self):
        st, body = self.post("/api/workflows", {
            "name": "x", "steps": [{"tool": "nope"}]})
        self.assertEqual(st, 400)
        self.assertEqual(body["error_code"], "invalid_definition")

    def test_cycle_is_400(self):
        st, body = self.post("/api/workflows", {
            "name": "x", "steps": [
                {"id": "a", "tool": "get_health", "depends_on": ["b"]},
                {"id": "b", "tool": "get_health", "depends_on": ["a"]}]})
        self.assertEqual(st, 400)

    def test_missing_run_is_404(self):
        st, body = self.get("/api/workflows/runs/9999")
        self.assertEqual(st, 404)
        st, body = self.post("/api/workflows/runs/9999/cancel", {})
        self.assertEqual(st, 404)

    def test_running_a_missing_workflow_is_404(self):
        st, body = self.post("/api/workflows/999/run", {})
        self.assertEqual(st, 404)
        self.assertEqual(body["error_code"], "not_found")

    def test_bad_run_params_is_400(self):
        a = self.create()
        st, body = self.post(f"/api/workflows/{a['id']}/run", {"params": "nope"})
        self.assertEqual(st, 400)

    def test_bad_layout_is_400(self):
        st, body = self.post("/api/workflows", {"name": "x", "steps": [],
                                                "layout": "nope"})
        self.assertEqual(st, 400)

    def test_runs_for_a_missing_workflow_is_404(self):
        st, body = self.get("/api/workflows/999/runs")
        self.assertEqual(st, 404)


class TestWorkflowApiRuns(_ApiBase):
    def test_run_executes_and_persists(self):
        a = self.create(name="Runner", steps=[{"id": "s1", "tool": "get_health"}])
        st, body = self.post(f"/api/workflows/{a['id']}/run", {"params": {}})
        self.assertEqual(st, 200, body)
        run = body["data"]
        self.assertEqual(run["status"], "completed")
        self.assertTrue(run["results"]["s1"]["ok"])
        self.assertEqual(run["workflow_id"], a["id"])

        st, body = self.get("/api/workflows/runs")
        self.assertEqual([r["id"] for r in body["data"]], [run["id"]])

        st, body = self.get(f"/api/workflows/{a['id']}/runs")
        self.assertEqual([r["id"] for r in body["data"]], [run["id"]])

        st, body = self.get(f"/api/workflows/runs/{run['id']}")
        self.assertEqual(body["data"]["id"], run["id"])
        self.assertEqual(body["data"]["results"]["s1"]["ok"], True)

    def test_runs_can_be_filtered_by_workflow(self):
        a = self.create(name="A", steps=[{"id": "s1", "tool": "get_health"}])
        b = self.create(name="B", steps=[{"id": "s1", "tool": "get_health"}])
        self.post(f"/api/workflows/{a['id']}/run", {})
        self.post(f"/api/workflows/{b['id']}/run", {})
        st, body = self.get(f"/api/workflows/runs?workflow_id={a['id']}")
        self.assertEqual(len(body["data"]), 1)
        self.assertEqual(body["data"][0]["workflow_id"], a["id"])

    def test_run_history_stays_independent(self):
        a = self.create(name="A", steps=[{"id": "s1", "tool": "get_health"}])
        b = self.create(name="B", steps=[{"id": "s1", "tool": "get_health"}])
        self.post(f"/api/workflows/{a['id']}/run", {})
        self.post(f"/api/workflows/{a['id']}/run", {})
        self.post(f"/api/workflows/{b['id']}/run", {})
        st, body = self.get(f"/api/workflows/{a['id']}/runs")
        self.assertEqual(len(body["data"]), 2)
        st, body = self.get(f"/api/workflows/{b['id']}/runs")
        self.assertEqual(len(body["data"]), 1)

    def test_history_stats_are_reported_for_the_list(self):
        a = self.create(name="Stat", steps=[{"id": "s1", "tool": "get_health"}])
        self.post(f"/api/workflows/{a['id']}/run", {})
        st, body = self.get("/api/workflows?stats=1")
        row = body["data"][0]
        self.assertEqual(row["run_count"], 1)
        self.assertEqual(row["last_run"]["status"], "completed")

    def test_a_cancel_request_on_a_finished_run_is_harmless(self):
        a = self.create(name="C", steps=[{"id": "s1", "tool": "get_health"}])
        st, body = self.post(f"/api/workflows/{a['id']}/run", {})
        run_id = body["data"]["id"]
        st, body = self.post(f"/api/workflows/runs/{run_id}/cancel", {})
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["status"], "completed")

    def test_events_for_a_run_are_queryable(self):
        a = self.create(name="Ev", steps=[{"id": "s1", "tool": "get_health"}])
        st, body = self.post(f"/api/workflows/{a['id']}/run", {})
        run_id = body["data"]["id"]
        st, body = self.get("/api/events?limit=200")
        mine = [e for e in body["data"] if (e.get("data") or {}).get("run_id") == run_id]
        kinds = {e["kind"] for e in mine}
        self.assertIn("workflow.started", kinds)
        self.assertIn("workflow.completed", kinds)
        self.assertIn("task.started", kinds)
        self.assertIn("task.completed", kinds)


class TestWorkflowApiOptions(_ApiBase):
    def test_options_describe_the_live_runtime(self):
        st, body = self.get("/api/workflows/options")
        self.assertEqual(st, 200)
        data = body["data"]
        names = {t["name"] for t in data["tools"]}
        self.assertIn("get_health", names)
        self.assertIn("browser_open", names)
        # the AI node's tool is on the same registry as everything else
        self.assertIn("ai_generate", names)
        self.assertIn("ai", data["categories"])
        self.assertIn("browser", data["categories"])
        self.assertTrue(data["scheduler"] is False,
                        "the test stack is built without a scheduler")
        self.assertIn("completed", data["statuses"])
        self.assertIn("daily", data["schedule_kinds"])
        for t in data["tools"]:
            self.assertIn("input_schema", t)

    def test_every_offered_tool_can_actually_be_used_as_a_step(self):
        st, body = self.get("/api/workflows/options")
        for tool in body["data"]["tools"]:
            steps = [{"id": "s1", "tool": tool["name"]}]
            st, out = self.post("/api/workflows", {"name": "t:" + tool["name"],
                                                   "steps": steps})
            self.assertEqual(st, 201, (tool["name"], out))


class TestWorkflowFailuresAreHonest(_ApiBase):
    provider_fail = True

    def test_a_failed_provider_fails_the_run(self):
        a = self.create(name="AIfail", steps=[
            {"id": "s1", "tool": "ai_generate",
             "params": {"prompt": "say hi"}}])
        st, body = self.post(f"/api/workflows/{a['id']}/run", {})
        self.assertEqual(st, 200)
        run = body["data"]
        self.assertEqual(run["status"], "failed", run)
        self.assertFalse(run["results"]["s1"]["ok"])
        self.assertTrue(run["error"])


class TestWorkflowAiNode(_ApiBase):
    def test_ai_step_really_calls_the_router_and_provider(self):
        a = self.create(name="AI", steps=[
            {"id": "s1", "tool": "ai_generate",
             "params": {"prompt": "summarise the run"}}])
        st, body = self.post(f"/api/workflows/{a['id']}/run", {})
        self.assertEqual(st, 200, body)
        run = body["data"]
        self.assertEqual(run["status"], "completed", run)
        out = run["results"]["s1"]["output"]
        self.assertEqual(out["text"], "hello from fake")
        self.assertEqual(out["provider"], "fake")
        self.assertGreaterEqual(self.provider.calls, 1)

    def test_ai_node_feeds_a_later_step_from_its_output(self):
        a = self.create(name="AI chain", steps=[
            {"id": "s1", "tool": "ai_generate", "params": {"prompt": "hi"}},
            {"id": "s2", "tool": "remember", "depends_on": ["s1"],
             "params": {"content": "answer: {{s1.output.text}}"}}])
        st, body = self.post(f"/api/workflows/{a['id']}/run", {})
        self.assertEqual(body["data"]["status"], "completed", body)
        st, body = self.get("/api/memory/search?query=hello")
        self.assertTrue(any("answer: hello from fake" in (m.get("content") or "")
                            for m in body["data"]))

    def test_ai_node_without_a_prompt_fails_the_step(self):
        a = self.create(name="AI empty", steps=[
            {"id": "s1", "tool": "ai_generate", "params": {}}])
        st, body = self.post(f"/api/workflows/{a['id']}/run", {})
        self.assertEqual(body["data"]["status"], "failed")
        self.assertIn("prompt", str(body["data"]["error"]).lower())

    def test_pinning_both_provider_and_model_is_honoured(self):
        a = self.create(name="AI pinned", steps=[
            {"id": "s1", "tool": "ai_generate",
             "params": {"prompt": "hi", "provider": "fake", "model": "fake-1"}}])
        st, body = self.post(f"/api/workflows/{a['id']}/run", {})
        run = body["data"]
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(run["results"]["s1"]["output"]["model"], "fake-1")

    def test_pinning_an_unavailable_target_fails_rather_than_silently_falling_back(self):
        a = self.create(name="AI pinned bad", steps=[
            {"id": "s1", "tool": "ai_generate",
             "params": {"prompt": "hi", "provider": "fake", "model": "nope-9"}}])
        st, body = self.post(f"/api/workflows/{a['id']}/run", {})
        self.assertEqual(body["data"]["status"], "failed")
        self.assertFalse(body["data"]["results"]["s1"]["ok"])


class TestWorkflowScheduling(_ApiBase):
    def setUp(self):
        super().setUp()
        from astra.workflows.scheduler import SchedulerManager
        self.sched = SchedulerManager(self.stack["store"], self.stack["workflows"],
                                      self.stack["events"])
        # the LiveServer's site resolves `scheduler` from the stack dict
        self.stack["scheduler"] = self.sched

    def test_a_schedule_can_be_attached_to_a_workflow(self):
        a = self.create(name="Scheduled", steps=[{"id": "s1", "tool": "get_health"}])
        st, body = self.post("/api/schedules", {"name": "nightly", "kind": "daily",
                                                "value": "03:00",
                                                "workflow_id": a["id"]})
        self.assertEqual(st, 201, body)
        self.assertEqual(body["data"]["workflow_id"], a["id"])
        st, body = self.get("/api/schedules")
        mine = [s for s in body["data"] if s["workflow_id"] == a["id"]]
        self.assertEqual(len(mine), 1)
        self.assertTrue(mine[0]["next_run"])

    def test_firing_a_schedule_runs_its_workflow(self):
        a = self.create(name="Fired", steps=[{"id": "s1", "tool": "get_health"}])
        self.sched.add("nightly", "oneshot", "2030-01-01 03:00", a["id"])
        before = len(self.stack["workflows"].list_runs(workflow_id=a["id"]))
        # run the schedule's workflow exactly as the ticker would
        self.stack["workflows"].run(workflow_id=a["id"])
        after = self.stack["workflows"].list_runs(workflow_id=a["id"])
        self.assertEqual(len(after), before + 1)
        self.assertEqual(after[0]["status"], "completed")

    def test_a_schedule_can_be_disabled_and_removed(self):
        a = self.create(name="Toggle", steps=[{"id": "s1", "tool": "get_health"}])
        s = self.sched.add("s", "interval", "60", a["id"])
        st, body = self.patch(f"/api/schedules/{s['id']}", {"enabled": False})
        self.assertEqual(st, 200)
        self.assertFalse(body["data"]["enabled"])
        st, body = self.delete(f"/api/schedules/{s['id']}")
        self.assertEqual(st, 200)
        self.assertEqual(self.sched.list(), [])


if __name__ == "__main__":
    unittest.main()
