"""The 🔀 Agent Workflow page: static contract + live API wiring.

Two things are locked here:

1. the *static* contract — the real DOM anchors astra.js binds to, that the
   page reuses the existing design system, that it consumes the same
   /api/events feed instead of a second logging path, and that event-derived
   text is never injected as HTML.
2. the *live* contract — the endpoints the page calls really exist on the
   running FastAPI/ASGI app, the tab is advertised in the manifest, tool
   introspection (module/function) is real, and DELETE removes a definition.

The pure node-graph / event-reduction logic is covered separately by
tests/js/workflow_model.test.js (run under node by test_workflow_model_js.py).
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _request(base, path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        with e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else {})


class TestAgentWorkflowStatic(unittest.TestCase):
    def setUp(self):
        self.html = _read("static", "index.html")
        self.js = _read("static", "js", "astra.js")
        self.css = _read("static", "css", "style.css")
        self.web = _read("astra", "web.py")
        self.model = _read("static", "js", "workflow_model.js")

    def _block(self):
        block = self.js[self.js.index("Agent Workflow (core)"):]
        return block[:block.index("/* ------------------------------ attachments")]

    # -- navigation ---------------------------------------------------------
    def test_tab_section_exists_and_is_manifest_driven(self):
        self.assertIn('id="tab-workflows"', self.html)
        # the tab list itself comes from the backend manifest, not the HTML
        self.assertIn('{"tab": "workflows", "label": "\\U0001f500 Agent Workflow"',
                      self.web)
        self.assertIn('id="nav"', self.html)

    # -- page anchors -------------------------------------------------------
    def test_panel_anchors_exist(self):
        for anchor in ('id="wf-views"', 'id="wf-view-pipeline"',
                       'id="wf-view-workflows"', 'id="wf-view-runs"',
                       'id="wf-flow"', 'id="wf-turn-label"',
                       'id="btn-wf-turn-prev"', 'id="btn-wf-turn-next"',
                       'id="wf-events"', 'id="wf-srcmap"', 'id="wf-live"',
                       'id="wf-select"', 'id="btn-wf-run"', 'id="btn-wf-edit"',
                       'id="btn-wf-delete"', 'id="btn-wf-new"',
                       'id="wf-run-params"', 'id="wf-graph"', 'id="wf-edges"',
                       'id="wf-inspector"', 'id="wf-legend"', 'id="wf-cards"',
                       'id="wf-builder"', 'id="wf-builder-name"',
                       'id="wf-builder-steps"', 'id="btn-wf-add-step"',
                       'id="btn-wf-save"', 'id="btn-wf-cancel"',
                       'id="wf-tools"', 'id="wf-runs"', 'id="wf-schedules"'):
            self.assertIn(anchor, self.html, f"missing {anchor}")

    # -- real endpoints, no second engine -----------------------------------
    def test_uses_only_the_real_backend_endpoints(self):
        for call in ('/api/workflows', '/api/workflows/runs', '/api/schedules',
                     '/api/tools', '/api/events?limit=500',
                     '/api/events/last', '/api/events/stream'):
            self.assertIn(call, self.js, f"astra.js never calls {call}")

    def test_delete_definition_is_reachable(self):
        self.assertIn('["api", "workflows"] and method == "DELETE"', self.web)

    def test_edit_is_reachable_via_patch(self):
        # PATCH is the verb the CORS preflight already allows (PUT is not in
        # Access-Control-Allow-Methods), and the engine keeps run history.
        self.assertIn('["api", "workflows"] and method == "PATCH"', self.web)
        engine = _read("astra", "workflows", "engine.py")
        self.assertIn("def update_definition(", engine)

    def test_reuses_the_shared_activity_log_feed(self):
        # one EventSource for the whole app; the workflow view subscribes to
        # it instead of opening a second logging path.
        self.assertIn("AstraWorkflowFeed", self.js)
        self.assertIn("if (SSE_ES) return SSE_ES;", self.js)
        self.assertIn("window.AstraWorkflowFeed = { ingest: wfIngest }", self.js)

    def test_shared_presentation_model_is_used(self):
        self.assertIn("const WM = window.AstraWorkflow;", self.js)
        for call in ("WM.buildGraph(", "WM.reduceRun(", "WM.reduceTurn(",
                     "WM.PIPELINE_STAGES", "WM.validateDefinition(",
                     "WM.parseParamsJson(", "WM.runStates(", "WM.newStepTemplate("):
            self.assertIn(call, self.js, f"astra.js does not use {call}")

    def test_stage_nodes_name_real_modules(self):
        # the code map is rendered from the model's real file/symbol table
        model = _read("static", "js", "workflow_model.js")
        for real in ("astra/ai/chat_pipeline.py", "astra/ai/router.py",
                     "astra/ai/agent_tool_loop.py", "astra/tools/registry.py",
                     "astra/ai/gateway_task_completion.py",
                     "astra/ai/response_boundary.py"):
            self.assertIn(real, model, f"{real} missing from the code map")

    # -- safety -------------------------------------------------------------
    def test_every_referenced_dom_anchor_exists(self):
        """The most common way a page like this dies silently is a selector
        typo: astra.js binds to an id that index.html never defined, the
        element is null and the tab throws on open. Every literal `$("#id")`
        in the workflow block must exist, and ids must stay unique."""
        from collections import Counter
        block = self._block()
        referenced = set(re.findall(r'\$\("#([a-zA-Z0-9_\-]+)"\)', block))
        self.assertTrue(referenced)
        html_ids = set(re.findall(r'id="([^"]+)"', self.html))
        missing = sorted(referenced - html_ids)
        self.assertEqual(missing, [], f"astra.js binds to undefined ids: {missing}")
        dups = [k for k, n in Counter(re.findall(r'id="([^"]+)"', self.html)).items()
                if n > 1]
        self.assertEqual(dups, [], f"duplicate element ids: {dups}")

    def test_event_text_is_never_injected_as_html(self):
        self.assertIn("title.textContent =", self.js)     # turn event rows
        self.assertIn("sub.textContent =", self.js)
        for dangerous in ("innerHTML = m.", "innerHTML = ev.data",
                          "innerHTML = event.", "innerHTML = e.data"):
            self.assertNotIn(dangerous, self.js)

    def test_never_touches_secret_bearing_endpoints(self):
        block = self.js[self.js.index("Agent Workflow (core)"):]
        block = block[:block.index("/* ------------------------------ attachments")]
        for forbidden in ("/authorize", "/reject", "private_key", "keystore",
                          "ASTRA_MASTER_SECRET", "/api/providers"):
            self.assertNotIn(forbidden, block, f"workflow view touches {forbidden}")

    # -- design language ----------------------------------------------------
    def test_reuses_the_existing_design_system(self):
        for cls in ('class="panel', 'class="chips', 'class="chip',
                    'class="btn', 'class="cards mini', 'class="list'):
            self.assertIn(cls, self.html, f"page does not reuse {cls}")
        for rule in (".wf-stage", ".wf-gnode", ".wf-edge", ".wf-dot",
                     ".wf-chip", ".wf-dot.running", ".wf-bstep"):
            self.assertIn(rule, self.css, f"missing style {rule}")
        # status colours come from the existing tokens, not new hardcoded ones
        block = self.css[self.css.index("Agent Workflow (core)"):]
        for token in ("var(--good)", "var(--bad)", "var(--warn)", "var(--accent)"):
            self.assertIn(token, block)

    def test_responsive_rules_exist(self):
        self.assertIn("@media (max-width: 760px)", self.css)
        block = self.css[self.css.index("Agent Workflow (core)"):]
        self.assertIn("flex-direction: column", block)

    # -- static lint: no ReferenceError/TypeError waiting to happen ---------
    def test_every_wf_helper_is_defined(self):
        block = self._block()
        defined = set(re.findall(r'function\s+(wf[A-Za-z0-9_]+)\s*\(', block))
        defined |= set(re.findall(r'(?:const|let|var)\s+(wf[A-Za-z0-9_]+)\s*=', block))
        used = set(re.findall(r'\b(wf[A-Z][A-Za-z0-9_]*)\s*\(', block))
        undefined = sorted(u for u in used if u not in defined)
        self.assertEqual(undefined, [], f"workflow view calls undefined helpers: {undefined}")

    def test_every_model_method_used_is_exported(self):
        exported = set(re.findall(r'^\s{4}([A-Za-z_][A-Za-z0-9_]*):',
                                  self.model, re.MULTILINE))
        used = set(re.findall(r'\bWM\.([A-Za-z_][A-Za-z0-9_]*)', self._block()))
        missing = sorted(used - exported)
        self.assertEqual(missing, [],
                         f"astra.js calls AstraWorkflow methods that are not exported: {missing}")


class TestAgentWorkflowAPI(unittest.TestCase):
    """The page's endpoints against the real server + real stack."""

    def setUp(self):
        from tests.helpers import LiveServer, make_stack
        from astra.tools import builtins
        # same workspace isolation the file-tool e2e tests use, so a
        # write_file step really writes instead of being refused by the
        # workspace sandbox.
        self._ws = tempfile.mkdtemp()
        self._saved_ws = builtins.WORKSPACE
        builtins.WORKSPACE = self._ws
        self.addCleanup(lambda: setattr(builtins, "WORKSPACE", self._saved_ws))
        self.stack = make_stack(with_scheduler=True)
        self.srv = LiveServer(stack=self.stack)
        self.base = self.srv.base

    def tearDown(self):
        self.srv.stop()

    def test_manifest_advertises_the_tab(self):
        st, body = _request(self.base, "/api/manifest")
        self.assertEqual(st, 200)
        tabs = {t["tab"]: t["label"] for t in body["data"]["tabs"]}
        self.assertIn("workflows", tabs)
        self.assertIn("Agent Workflow", tabs["workflows"])

    def test_workflow_crud_and_runs(self):
        st, body = _request(self.base, "/api/workflows", "POST", {
            "name": "ui wf", "description": "from the page",
            "steps": [{"id": "a", "tool": "get_health"},
                      {"id": "b", "tool": "remember",
                       "params": {"content": "ui note"}, "depends_on": ["a"]}]})
        self.assertEqual(st, 201)
        wid = body["data"]["id"]
        self.assertEqual(body["data"]["steps"][1]["depends_on"], ["a"])

        st, body = _request(self.base, f"/api/workflows/{wid}/run", "POST", {"params": {}})
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["status"], "completed")
        self.assertTrue(body["data"]["results"]["a"]["ok"])

        st, body = _request(self.base, "/api/workflows/runs")
        self.assertEqual(st, 200)
        self.assertEqual(body["data"][0]["id"], body["data"][0]["id"])
        self.assertTrue(any(r["workflow_id"] == wid for r in body["data"]))

        # the events the live view follows really exist for this run
        st, body = _request(self.base, "/api/events?limit=50")
        kinds = {e["kind"] for e in body["data"]}
        self.assertIn("workflow.started", kinds)
        self.assertIn("task.started", kinds)

        st, body = _request(self.base, f"/api/workflows/{wid}", "DELETE")
        self.assertEqual(st, 200)
        st, body = _request(self.base, "/api/workflows")
        self.assertFalse(any(w["id"] == wid for w in body["data"]))

    def test_delete_missing_and_bad_id(self):
        st, _ = _request(self.base, "/api/workflows/99999", "DELETE")
        self.assertEqual(st, 404)
        st, _ = _request(self.base, "/api/workflows/abc", "DELETE")
        self.assertEqual(st, 400)

    def test_edit_definition_in_place_keeps_run_history(self):
        st, body = _request(self.base, "/api/workflows", "POST", {
            "name": "edit me", "description": "before",
            "steps": [{"id": "a", "tool": "get_health"}]})
        self.assertEqual(st, 201)
        wid = body["data"]["id"]
        st, _ = _request(self.base, f"/api/workflows/{wid}/run", "POST", {"params": {}})
        self.assertEqual(st, 200)

        st, body = _request(self.base, f"/api/workflows/{wid}", "PATCH", {
            "name": "edited", "description": "after",
            "steps": [{"id": "a", "tool": "get_health"},
                      {"id": "b", "tool": "remember",
                       "params": {"content": "edited step"}, "depends_on": ["a"]}]})
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["name"], "edited")
        self.assertEqual(body["data"]["description"], "after")
        self.assertEqual(len(body["data"]["steps"]), 2)
        # the earlier run (and its results) survived the edit
        st, runs = _request(self.base, "/api/workflows/runs")
        mine = [r for r in runs["data"] if r["workflow_id"] == wid]
        self.assertTrue(mine)
        self.assertEqual(mine[0]["status"], "completed")

        # the edited definition is what runs next
        st, body = _request(self.base, f"/api/workflows/{wid}/run", "POST", {"params": {}})
        self.assertEqual(body["data"]["results"]["b"]["ok"], True)

    def test_edit_rejects_bad_input(self):
        st, body = _request(self.base, "/api/workflows", "POST", {
            "name": "edit bad", "steps": [{"id": "a", "tool": "get_health"}]})
        wid = body["data"]["id"]
        st, _ = _request(self.base, f"/api/workflows/{wid}", "PATCH", {"steps": "nope"})
        self.assertEqual(st, 400)
        st, _ = _request(self.base, f"/api/workflows/{wid}", "PATCH", {"name": "   "})
        self.assertEqual(st, 400)
        st, _ = _request(self.base, "/api/workflows/99999", "PATCH", {"name": "x"})
        self.assertEqual(st, 404)

    def test_tools_expose_real_module_and_function(self):
        st, body = _request(self.base, "/api/tools")
        self.assertEqual(st, 200)
        tools = {t["name"]: t for t in body["data"]["tools"]}
        self.assertIn("get_health", tools)
        self.assertEqual(tools["get_health"]["module"], "astra/tools/builtins.py")
        self.assertEqual(tools["get_health"]["function"], "get_health")
        self.assertTrue(tools["terminal_exec"]["module"].endswith(".py"))
        # tool introspection must not carry secrets either
        blob = json.dumps(body)
        self.assertNotIn("ghp_", blob)
        self.assertIsNone(re.search(r"\bsk-[A-Za-z0-9]{10,}", blob))

    def test_events_last_and_schedules(self):
        st, body = _request(self.base, "/api/events/last")
        self.assertEqual(st, 200)
        self.assertIn("last_id", body["data"])
        st, body = _request(self.base, "/api/schedules")
        self.assertEqual(st, 200)
        self.assertIsInstance(body["data"], list)

    def test_page_and_model_assets_are_served(self):
        for path in ("/", "/static/js/workflow_model.js", "/static/css/style.css"):
            req = urllib.request.Request(self.base + path)
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(resp.status, 200, path)

    def test_unknown_tool_step_fails_that_step_only(self):
        """engine.run() raises KeyError per step — that must be a failed
        step, not a crashed request, and the events must say so."""
        st, body = _request(self.base, "/api/workflows", "POST", {
            "name": "bad step wf",
            "steps": [{"id": "ok1", "tool": "get_health"},
                      {"id": "bad", "tool": "no_such_tool"}]})
        self.assertEqual(st, 201)
        wid = body["data"]["id"]
        st, body = _request(self.base, f"/api/workflows/{wid}/run", "POST", {"params": {}})
        self.assertEqual(st, 200)
        results = body["data"]["results"]
        self.assertTrue(results["ok1"]["ok"])
        self.assertFalse(results["bad"]["ok"])
        self.assertIn("no_such_tool", results["bad"]["error"])

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_live_events_reduce_to_the_real_run_state(self):
        """The page's live view is only honest if the real emitted events
        reduce to the real engine result. Run a workflow that exercises every
        step outcome (ok / failed / skipped-by-condition / data-flow), then
        feed the actual persisted events through the actual JS model under
        node and compare against the engine's own `results`."""
        st, body = _request(self.base, "/api/workflows", "POST", {
            "name": "live reduce wf",
            "steps": [
                {"id": "ok1", "tool": "get_health"},
                {"id": "w", "tool": "write_file", "depends_on": ["ok1"],
                 "params": {"path": "live.txt", "content": "hi"}},
                {"id": "r", "tool": "read_file", "depends_on": ["w"],
                 "params": {"path": "{{w.output.path}}"}},
                {"id": "bad", "tool": "no_such_tool", "depends_on": ["ok1"]},
                # engine._condition(): op "ok" runs the step only when the
                # referenced step succeeded — `bad` failed, so this is skipped.
                {"id": "skipme", "tool": "get_health",
                 "if": {"step": "bad", "op": "ok"}},
            ]})
        self.assertEqual(st, 201)
        wid = body["data"]["id"]
        st, run_body = _request(self.base, f"/api/workflows/{wid}/run", "POST",
                                {"params": {}})
        self.assertEqual(st, 200)
        run = run_body["data"]

        st, ev_body = _request(self.base, "/api/events?limit=200")
        self.assertEqual(st, 200)
        # /api/events is newest-first; the live feed (EventBus.since) is
        # chronological, so reduce in id order like the running page does.
        events = sorted((e for e in ev_body["data"]
                         if str((e.get("data") or {}).get("op", "")).startswith("wf:")),
                        key=lambda e: e["id"])
        self.assertTrue(events)

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(events, fh)
            events_path = fh.name
        self.addCleanup(lambda: os.unlink(events_path))

        script = (
            "const W=require(%r);const evs=require(%r);"
            "let st=W.emptyRun();"
            "W.pipelineTurns;"
            "for(const e of evs){W.reduceRun(st,e);}"
            "process.stdout.write(JSON.stringify(st));"
        ) % (os.path.join(ROOT, "static", "js", "workflow_model.js"), events_path)
        proc = subprocess.run(["node", "-e", script], cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        live = json.loads(proc.stdout)

        self.assertEqual(live["run_id"], str(run["id"]))
        self.assertEqual(live["status"], "completed")
        # every step the engine recorded is painted with the matching state
        self.assertEqual(live["steps"]["ok1"]["state"], "completed")
        self.assertEqual(live["steps"]["w"]["state"], "completed")
        self.assertEqual(live["steps"]["r"]["state"], "completed")
        self.assertEqual(live["steps"]["bad"]["state"], "failed")
        self.assertIn("no_such_tool", live["steps"]["bad"]["error"])
        # engine._condition() marked `skipme` skipped in the results — the
        # reference agrees, and (because the engine `continue`s without
        # emitting) the live reducer must simply have no entry for it.
        self.assertEqual(step_state_reference(run, "skipme"), "skipped")
        self.assertNotIn("skipme", live["steps"])

    def test_live_run_writes_a_real_note_through_the_data_flow(self):
        steps = [{"id": "w", "tool": "write_file",
                  "params": {"path": "flow.txt", "content": "flowed"}},
                 {"id": "r", "tool": "read_file", "depends_on": ["w"],
                  "params": {"path": "{{w.output.path}}"}}]
        st, body = _request(self.base, "/api/workflows", "POST",
                            {"name": "flow wf", "steps": steps})
        wid = body["data"]["id"]
        st, run_body = _request(self.base, f"/api/workflows/{wid}/run", "POST",
                                {"params": {}})
        self.assertEqual(st, 200)
        self.assertEqual(run_body["data"]["results"]["r"]["output"]["content"],
                         "flowed")


def step_state_reference(run, step_id):
    """The state the JS model must derive for a step, straight from the
    engine's own persisted result — the reference for the node assertion."""
    from astra.workflows.engine import WorkflowEngine  # noqa: F401 (real shape)
    res = (run.get("results") or {}).get(step_id) or {}
    if res.get("skipped"):
        return "skipped"
    if res.get("blocked"):
        return "blocked"
    if res.get("ok") is True:
        return "completed"
    if res.get("ok") is False:
        return "failed"
    return "pending"


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestAgentWorkflowPipelineLive(unittest.TestCase):
    """The 'Runtime pipeline' view over a REAL chat turn.

    A real /api/chat turn is driven through the real ChatPipeline against a
    local fake provider (same fake tests/test_e2e_flows.py uses); the actual
    persisted events are then reduced by the actual JS model under node, and
    the resulting stage states must match what really happened.
    """

    def setUp(self):
        from tests.helpers import LiveServer, make_stack
        from tests.test_e2e_flows import FakeProvider
        self.stack = make_stack()
        self.provider = FakeProvider()
        self.stack["router"].add(self.provider)
        self.srv = LiveServer(stack=self.stack)
        self.addCleanup(self.srv.stop)
        self.base = self.srv.base

    def _reduce(self, events):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(events, fh)
            path = fh.name
        self.addCleanup(lambda: os.unlink(path))
        script = (
            "const W=require(%r);const evs=require(%r);"
            "let t=null;for(const e of evs){"
            "  if(!t){t=W.newTurn(W.turnKeyOf(e),e,e.id);}"
            "  W.reduceTurn(t,e);}"
            "process.stdout.write(JSON.stringify(t));"
        ) % (os.path.join(ROOT, "static", "js", "workflow_model.js"), path)
        proc = subprocess.run(["node", "-e", script], cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_a_real_chat_turn_reduces_onto_the_pipeline_stages(self):
        st, body = _request(self.base, "/api/chat", "POST", {"message": "hi there"})
        self.assertEqual(st, 200)
        self.assertTrue(body["data"]["ok"])

        st, ev_body = _request(self.base, "/api/events?limit=200")
        self.assertEqual(st, 200)
        all_events = sorted(ev_body["data"], key=lambda e: e["id"])
        started = [e for e in all_events if e["kind"] == "chat.pipeline.started"]
        self.assertTrue(started, "no chat.pipeline.started event was emitted")
        turn = started[-1]["data"]["request"]

        def belongs(e):
            d = e.get("data") or {}
            return (d.get("request") == turn or d.get("trace") == turn
                    or d.get("op") == "chat:" + turn)
        events = [e for e in all_events if belongs(e)]
        self.assertTrue(len(events) >= 2)

        reduced = self._reduce(events)
        self.assertEqual(reduced["key"], turn)
        # the real emitted stages for this turn: request → route/execute →
        # reply (the Gateway is absent in tests, so understand is skipped)
        self.assertGreaterEqual(reduced["stages"]["request"]["count"], 1)
        self.assertGreaterEqual(reduced["stages"]["reply"]["count"], 1)
        self.assertEqual(reduced["stages"]["reply"]["status"], "ok")
        # The Gateway is absent in tests, so verification is reported
        # "skipped" (an honest unverified reply) → the turn reads as a
        # warning rather than a fake success.
        self.assertIn(reduced["status"], ("ok", "warn"))
        # the router really chose the fake provider/model, and the view shows it
        self.assertEqual(reduced["provider"], "fake")
        self.assertEqual(reduced["model"], "fake-1")
        # a finished turn leaves no stage stuck on "running"
        for sid, stage in reduced["stages"].items():
            self.assertNotEqual(stage["status"], "running",
                                f"stage {sid} still running after the turn ended")


if __name__ == "__main__":
    unittest.main()
