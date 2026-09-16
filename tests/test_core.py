"""Tests for the Personal-OS core subsystems (EventBus, TaskEngine, ToolRegistry,
Memory, Experience, Workflow, Scheduler, Planner, Orchestrator, Provider,
Config, Context/State/Timeutil, bootstrap)."""
import io, json, os, time, unittest
import http.server
import threading
from datetime import datetime, timedelta
from tests.helpers import make_stack, Store

# ── EventBus ──────────────────────────────────────────────────────────────────
class TestEventBus(unittest.TestCase):
    def setUp(self): self.stack = make_stack(); self.ev = self.stack["events"]

    def test_emit_and_history(self):
        rec = self.ev.emit("test.hello", agent="tester", message="hi")
        self.assertGreater(rec["id"], 0)
        self.assertEqual(rec["kind"], "test.hello")
        hist = self.ev.history(limit=5)
        self.assertTrue(any(e["kind"] == "test.hello" for e in hist))

    def test_since(self):
        first_id = self.ev.emit("first", agent="a")["id"]
        self.ev.emit("second", agent="a")
        since = self.ev.since(first_id)
        self.assertEqual(len(since), 1)
        self.assertEqual(since[0]["kind"], "second")

    def test_last_id(self):
        self.ev.emit("x"); self.ev.emit("y")
        self.assertGreaterEqual(self.ev.last_id(), 2)

    def test_count(self):
        before = self.ev.count()
        self.ev.emit("z")
        self.assertEqual(self.ev.count(), before + 1)

    def test_emit_arbitrary_kind_accepted(self):
        """EventBus is permissive: unknown kinds are stored (plugins may add
        their own event types at runtime)."""
        rec = self.ev.emit("custom.game_event", agent="plugin", score=99)
        self.assertEqual(rec["kind"], "custom.game_event")
        self.assertEqual(rec["data"]["score"], 99)


# ── TaskEngine ────────────────────────────────────────────────────────────────
class TestTaskEngine(unittest.TestCase):
    def setUp(self): self.stack = make_stack(); self.te = self.stack["tasks"]

    def test_create_and_get(self):
        t = self.te.create("hello", type="research", priority=2)
        self.assertEqual(t["goal"], "hello")
        self.assertEqual(t["status"], "pending")
        self.assertEqual(t["type"], "research")

    def test_mark_done(self):
        t = self.te.create("f")
        done = self.te.mark(t["id"], "done", result={"ok": True})
        self.assertEqual(done["status"], "done")

    def test_list_filters(self):
        self.te.create("a", type="research"); self.te.create("b", type="tool")
        research = self.te.list(type="research")
        self.assertEqual(len(research), 1)
        self.assertEqual(research[0]["type"], "research")

    def test_dependency_claim_ready(self):
        a = self.te.create("a"); b = self.te.create("b", dependencies=[a["id"]])
        # b is blocked by a; only a should be claimed
        claimed = self.te.claim_ready()
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["id"], a["id"])
        # mark a done, now b becomes ready
        self.te.mark(a["id"], "done")
        claimed2 = self.te.claim_ready()
        self.assertEqual(len(claimed2), 1)
        self.assertEqual(claimed2[0]["id"], b["id"])

    def test_subtree_cancel(self):
        root = self.te.create("root")
        child = self.te.create("child", parent_task_id=root["id"])
        self.te.cancel_subtree(root["id"])
        self.assertEqual(self.te.get(root["id"])["status"], "cancelled")
        self.assertEqual(self.te.get(child["id"])["status"], "cancelled")


# ── ToolRegistry ──────────────────────────────────────────────────────────────
class TestToolRegistry(unittest.TestCase):
    def setUp(self): self.stack = make_stack(); self.reg = self.stack["registry"]

    def test_builtin_tools_registered(self):
        names = [t["name"] for t in self.reg.list()]
        for expect in ("search_web", "remember", "recall", "get_health",
                       "create_task", "list_tasks", "read_file", "write_file",
                       "search_files", "wallet_balances", "fetch_url"):
            self.assertIn(expect, names)

    def test_execute_remember_and_recall(self):
        ctx = self.stack["orchestrator"]._tool_ctx()
        out = self.reg.execute("remember", {"content": "test memory 123",
                                             "category": "note"}, ctx)
        self.assertTrue(out.get("ok"))
        self.assertGreater(out["result"]["id"], 0)

    def test_execute_search_web_offline(self):
        ctx = self.stack["orchestrator"]._tool_ctx()
        out = self.reg.execute("search_web", {"query": "python test"}, ctx)
        self.assertIn("ok", out)
        # graceful: must never raise — returns a structured result either way
        self.assertIn("results", out["result"])
        self.assertIn("query", out["result"])

    def test_execute_search_web_offline_graceful_on_network_failure(self):
        """search_web must not raise when the network is down — it returns a
        structured offline result (Section 4: offline-graceful)."""
        import urllib.error
        import urllib.request
        from unittest import mock
        ctx = self.stack["orchestrator"]._tool_ctx()
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("offline")):
            out = self.reg.execute("search_web", {"query": "flip", "n": 3}, ctx)
        self.assertTrue(out.get("ok"))            # the tool itself succeeded
        res = out["result"]
        self.assertTrue(res.get("offline"))       # structured offline flag
        self.assertEqual(res.get("results"), [])
        self.assertEqual(res.get("count"), 0)
        self.assertEqual(res.get("query"), "flip")
        self.assertIn("network_unavailable", res.get("error", ""))

    def test_stats_recorded_on_success(self):
        """Successful executions are counted in tool stats (Section 7)."""
        out = self.reg.execute("get_health", {}, None)
        self.assertTrue(out["ok"])
        st = self.reg.stats("get_health")
        self.assertEqual(st["calls"], 1)
        self.assertEqual(st["errors"], 0)
        self.assertGreaterEqual(st["total_ms"], 0)
        self.assertEqual(out["duration_ms"], st["total_ms"])

    def test_stats(self):
        # stats() reflects both successes and failures
        self.reg._note("test_tool", errored=False, ms=50)
        self.reg._note("test_tool", errored=True, ms=100)
        st = self.reg.stats("test_tool")
        self.assertEqual(st["calls"], 2)
        self.assertEqual(st["errors"], 1)
        self.assertEqual(st["total_ms"], 150)
        # all_stats() returns the full dict
        all_st = self.reg.stats()
        self.assertIn("test_tool", all_st)


# ── Memory + Experience ───────────────────────────────────────────────────────
class TestMemory(unittest.TestCase):
    def setUp(self):
        self.stack = make_stack()
        self.mem = self.stack["memory"]
        self.exp = self.stack["experiences"]

    def test_save_search_forget(self):
        m = self.mem.save("rohan er wallet balance beshi", category="note")
        self.assertGreater(m["id"], 0)
        results = self.mem.search("wallet balance", k=5)
        self.assertTrue(any("wallet" in r["content"] for r in results))
        self.mem.forget(m["id"])
        self.assertIsNone(self.mem.get(m["id"]))

    def test_experience_add_recall(self):
        self.exp.add("check eligibility", strategy="research+browser",
                     failure="", success=True)
        hits = self.exp.recall("eligibility", k=1)
        self.assertEqual(len(hits), 1)
        stats = self.exp.stats()
        self.assertEqual(stats["total"], 1)


# ── WorkflowEngine ────────────────────────────────────────────────────────────
class TestWorkflow(unittest.TestCase):
    def setUp(self):
        self.stack = make_stack()
        self.wf = self.stack["workflows"]

    def test_define_and_run(self):
        d = self.wf.define("health check", "daily", [
            {"tool": "get_health", "name": "h"}])
        self.assertEqual(d["steps"][0]["tool"], "get_health")
        run = self.wf.run(workflow_id=d["id"])
        self.assertEqual(run["status"], "completed")
        self.assertIn("step1", run["results"])
        self.assertTrue(run["results"]["step1"]["ok"])

    def test_list_runs(self):
        d = self.wf.define("test", "x", [{"tool": "get_health", "name": "h"}])
        self.wf.run(workflow_id=d["id"])
        runs = self.wf.list_runs()
        self.assertEqual(len(runs), 1)

    def test_step_without_id_gets_auto_id(self):
        d = self.wf.define("auto", "", [{"tool": "get_health", "name": "h"}])
        run = self.wf.run(workflow_id=d["id"])
        self.assertIn("step1", run["results"])


# ── SchedulerManager ──────────────────────────────────────────────────────────
class TestScheduler(unittest.TestCase):
    def setUp(self):
        from astra.workflows.scheduler import SchedulerManager
        self.stack = make_stack(with_scheduler=False)
        self.sched = SchedulerManager(self.stack["store"], self.stack["workflows"],
                                      self.stack["events"])

    def test_add_list_set_enabled_delete(self):
        s = self.sched.add("morning", "daily", "09:00")
        self.assertTrue(s["enabled"])
        self.sched.set_enabled(s["id"], False)
        self.assertFalse(self.sched.get(s["id"])["enabled"])
        self.sched.delete(s["id"])
        self.assertIsNone(self.sched.get(s["id"]))

    def test_compute_next_run_daily(self):
        from astra.workflows.scheduler import compute_next_run
        now = datetime(2026, 9, 15, 8, 0, 0)
        nxt = compute_next_run("daily", "09:00", now=now)
        self.assertIn("2026-09-15", nxt)
        self.assertTrue(nxt.endswith("09:00:00") or nxt.endswith("09:00"))

    def test_compute_next_run_interval(self):
        from astra.workflows.scheduler import compute_next_run
        now = datetime(2026, 9, 15, 12, 0, 0)
        nxt = compute_next_run("interval", "60", after=now, now=now)
        expected = (now + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(nxt, expected)

    def test_compute_next_run_weekly(self):
        from astra.workflows.scheduler import compute_next_run
        # 2026-09-15 is a Tuesday → next Mon is Sep 21
        now = datetime(2026, 9, 15, 10, 0, 0)
        nxt = compute_next_run("weekly", "Mon 09:00", now=now)
        self.assertIn("2026-09-21", nxt)

    def test_compute_next_run_oneshot(self):
        from astra.workflows.scheduler import compute_next_run
        now = datetime(2026, 9, 15, 10, 0, 0)
        nxt = compute_next_run("oneshot", "2026-12-01 08:00", now=now)
        self.assertIn("2026-12-01", nxt)
        # past oneshot returns None
        self.assertIsNone(compute_next_run("oneshot", "2025-01-01 00:00", now=now))


# ── Planner (offline) ────────────────────────────────────────────────────────
class TestPlanner(unittest.TestCase):
    def setUp(self): self.stack = make_stack()

    def test_offline_wallet_balances(self):
        plan = self.stack["planner"].plan("amader wallet balance dekho")
        self.assertEqual(plan[0]["tool"], "wallet_balances")

    def test_offline_help(self):
        plan = self.stack["planner"].plan("help")
        self.assertEqual(plan[0]["tool"], "get_health")

    def test_offline_plan_my_day(self):
        plan = self.stack["planner"].plan("plan my day")
        # matches (today|what do i need|plan my day|…), produces list_tasks
        self.assertEqual(plan[0]["tool"], "list_tasks")

    def test_offline_research_url(self):
        plan = self.stack["planner"].plan("research https://example.com")
        self.assertEqual(plan[0]["tool"], "fetch_url")
        self.assertIn("example.com", plan[0]["params"]["url"])

    def test_answer_tool_for_unknown(self):
        plan = self.stack["planner"].plan("xkcd blorpberry 999")
        self.assertEqual(plan[0]["tool"], "answer")


# ── Planner + Astra AI Gateway: Request Intelligence wiring ─────────────────
class _FakeGatewayIntelligenceCall:
    """Stand-in for GatewayRequestIntelligence: records the raw goal it was
    given and returns a distinguishable rewritten string, so tests can
    prove the *enriched* text (not the raw one) is what reaches the
    existing Provider system."""

    def __init__(self, rewritten="USER TASK:\nDo the enriched thing.",
                enriched=True, connection="astra-gw-gemini"):
        self.calls = []
        self.rewritten = rewritten
        self.enriched = enriched
        self.connection = connection

    def process(self, raw_text, **kw):
        self.calls.append(raw_text)
        self.last_kwargs = kw
        if not self.enriched:
            return {"text": raw_text, "enriched": False,
                    "gateway_connection": "", "raw_text": raw_text}
        return {"text": self.rewritten, "enriched": True,
                "gateway_connection": self.connection, "raw_text": raw_text}


class _FakeRouterCapturesPrompt:
    """Stand-in for AstraRouter.route(): records the prompt text it
    received, so tests can inspect exactly what the (fake) Provider system
    was handed — without touching any real Provider adapter."""

    def __init__(self, reply_json):
        self.reply_json = reply_json
        self.received_prompts = []

    def route(self, messages):
        prompt = messages[0]["content"]
        self.received_prompts.append(prompt)
        return ("fake-provider", "fake-model", self.reply_json)


class TestPlannerGatewayIntelligence(unittest.TestCase):
    """Gateway Request Understanding/Enrichment sits between Planner and the
    existing Provider system: Planner._ai_steps() must run the raw goal
    through `gateway_intelligence.process()` first, then hand the *result*
    (not the raw goal) to `router.route()` — this is the
    Assistant -> Gateway -> Provider handoff from the spec."""

    def test_ai_steps_enriches_goal_via_gateway_before_provider(self):
        from astra.core.planner import Planner
        gw = _FakeGatewayIntelligenceCall()
        router = _FakeRouterCapturesPrompt(
            '{"steps":[{"id":"s1","tool":"answer","params":{"text":"done"}}]}')
        planner = Planner(router=router, tools=["answer"], gateway_intelligence=gw)
        steps = planner._ai_steps("svpb er task ta kore dao")
        self.assertEqual(gw.calls, ["svpb er task ta kore dao"])
        self.assertIn(gw.rewritten, router.received_prompts[0])
        self.assertNotIn("svpb er task ta kore dao", router.received_prompts[0])
        self.assertTrue(planner.last_gateway_enriched)
        self.assertEqual(planner.last_gateway_connection, "astra-gw-gemini")
        self.assertEqual(steps[0]["tool"], "answer")

    def test_ai_steps_falls_back_to_raw_goal_when_gateway_declines(self):
        """Graceful degradation: if the Gateway can't/doesn't improve the
        request, the raw goal still reaches the Provider system unchanged
        — enrichment is a quality improvement, never a dependency."""
        from astra.core.planner import Planner
        gw = _FakeGatewayIntelligenceCall(enriched=False)
        router = _FakeRouterCapturesPrompt(
            '{"steps":[{"id":"s1","tool":"answer","params":{"text":"done"}}]}')
        planner = Planner(router=router, tools=["answer"], gateway_intelligence=gw)
        planner._ai_steps("raw unclear goal")
        self.assertIn("raw unclear goal", router.received_prompts[0])
        self.assertFalse(planner.last_gateway_enriched)

    def test_ai_steps_works_without_gateway_intelligence_at_all(self):
        """No Gateway configured (gateway_intelligence=None, the default) —
        planning must behave exactly as it did before this layer existed."""
        from astra.core.planner import Planner
        router = _FakeRouterCapturesPrompt(
            '{"steps":[{"id":"s1","tool":"answer","params":{"text":"done"}}]}')
        planner = Planner(router=router, tools=["answer"])
        steps = planner._ai_steps("plain goal text")
        self.assertIn("plain goal text", router.received_prompts[0])
        self.assertEqual(steps[0]["tool"], "answer")
        self.assertFalse(planner.last_gateway_enriched)

    def test_gateway_intelligence_never_touches_the_provider_system(self):
        """Isolation: the Gateway layer only ever receives the raw goal
        text — it holds no reference to the router/Provider system, so
        Gateway -> ProviderRegistry can't happen even by accident. The
        Provider system (`router`) is still invoked exactly once, by
        Planner itself — the Gateway never executes the task."""
        from astra.core.planner import Planner
        gw = _FakeGatewayIntelligenceCall()
        router = _FakeRouterCapturesPrompt(
            '{"steps":[{"id":"s1","tool":"answer","params":{"text":"done"}}]}')
        planner = Planner(router=router, tools=["answer"], gateway_intelligence=gw)
        self.assertFalse(hasattr(gw, "router"))
        self.assertFalse(hasattr(gw, "providers"))
        planner._ai_steps("goal")
        self.assertEqual(len(router.received_prompts), 1)

    def test_ai_steps_passes_conversation_context_to_gateway(self):
        """Planner.plan(goal, ctx={"conversation_context": ...}) must reach
        gateway_intelligence.process() as `context`, so a short follow-up
        can be understood against recent prior conversation."""
        from astra.core.planner import Planner
        gw = _FakeGatewayIntelligenceCall()
        router = _FakeRouterCapturesPrompt(
            '{"steps":[{"id":"s1","tool":"answer","params":{"text":"done"}}]}')
        planner = Planner(router=router, tools=["answer"], gateway_intelligence=gw)
        planner.plan("eita ki?", ctx={"conversation_context": "prior turn about X"})
        self.assertEqual(gw.last_kwargs.get("context"), "prior turn about X")

    def test_ai_steps_context_defaults_to_empty_without_ctx(self):
        from astra.core.planner import Planner
        gw = _FakeGatewayIntelligenceCall()
        router = _FakeRouterCapturesPrompt(
            '{"steps":[{"id":"s1","tool":"answer","params":{"text":"done"}}]}')
        planner = Planner(router=router, tools=["answer"], gateway_intelligence=gw)
        planner.plan("plain goal")
        self.assertEqual(gw.last_kwargs.get("context"), "")

    def test_full_stack_gateway_intelligence_defaults_to_noop_pass_through(self):
        """End-to-end with the real bootstrap wiring and no GW_* config: the
        Gateway is absent, so `gateway_intelligence` degrades to a
        pass-through and normal offline/answer planning is unaffected."""
        stack = make_stack()
        planner = stack["planner"]
        self.assertIsNotNone(planner.gateway_intelligence)
        self.assertFalse(planner.gateway_intelligence.is_usable())
        plan = planner.plan("xkcd blorpberry 999")
        self.assertEqual(plan[0]["tool"], "answer")


# ── Orchestrator ──────────────────────────────────────────────────────────────
class TestOrchestrator(unittest.TestCase):
    def setUp(self): self.stack = make_stack(); self.orch = self.stack["orchestrator"]

    def test_submit_sync_health(self):
        r = self.orch.submit("get_health", sync=True)
        self.assertIn(r["status"], ("COMPLETED", "FAILED"))
        self.assertGreater(r["steps"], 0)

    def test_submit_sync_unknown_goal(self):
        r = self.orch.submit("random blorp 42", sync=True)
        self.assertIn(r["status"], ("COMPLETED", "FAILED"))

    def test_submit_async_and_state(self):
        r = self.orch.submit("get_health", sync=False)
        self.assertIn("execution_id", r)
        self.assertEqual(r["status"], "started")
        state = self.orch.state(r["execution_id"])
        self.assertIn(state["status"], ("IDLE", "PLANNING", "EXECUTING",
                                        "COMPLETED", "FAILED", "unknown"))

    def test_submit_conversation_context_reaches_planner(self):
        """submit(goal, context=...) must be persisted and handed to
        Planner.plan(ctx={"conversation_context": ...}) on both the first
        run and any replan — the recorded free-form path all the way from
        the API surface down to the Gateway's request-understanding step."""
        captured = {}
        real_plan = self.stack["planner"].plan

        def spy_plan(goal, ctx=None, **kw):
            captured["ctx"] = ctx
            return real_plan(goal, ctx=ctx, **kw)
        self.stack["planner"].plan = spy_plan
        self.orch.submit("xkcd blorpberry 999", sync=True,
                         context="earlier: discussed Notcoin airdrop")
        self.assertEqual(captured["ctx"].get("conversation_context"),
                         "earlier: discussed Notcoin airdrop")

    def test_submit_without_context_defaults_to_empty(self):
        captured = {}
        real_plan = self.stack["planner"].plan

        def spy_plan(goal, ctx=None, **kw):
            captured["ctx"] = ctx
            return real_plan(goal, ctx=ctx, **kw)
        self.stack["planner"].plan = spy_plan
        self.orch.submit("xkcd blorpberry 999", sync=True)
        self.assertEqual(captured["ctx"].get("conversation_context"), "")

    def test_recent_and_stats(self):
        self.orch.submit("get_health", sync=True)
        rec = self.orch.recent()
        self.assertGreater(len(rec), 0)
        stats = self.orch.stats()
        self.assertIn("total", stats)


# ── Config ────────────────────────────────────────────────────────────────────
class TestConfig(unittest.TestCase):
    """Config tests isolate from environment PORT/ASTRA_* by clearing them
    during the test, then restoring afterwards."""
    def setUp(self):
        self._saved = {}
        for k in ("PORT", "ASTRA_PORT"):
            if k in os.environ:
                self._saved[k] = os.environ.pop(k)

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ[k] = v

    def test_default_values(self):
        from astra.core.config import Config
        cfg = Config()
        self.assertEqual(cfg.getint("PORT", 8787), 8787)
        self.assertFalse(cfg.getbool("NO_BROWSER", False))

    def test_env_precedence(self):
        from astra.core.config import Config
        os.environ["ASTRA_PORT"] = "12345"
        cfg = Config()
        self.assertEqual(cfg.getint("PORT", 8787), 12345)

    def test_set_override(self):
        from astra.core.config import Config
        cfg = Config()
        cfg.set("MY_KEY", "my_val")
        self.assertEqual(cfg.get("MY_KEY"), "my_val")

    def test_all_snapshot(self):
        from astra.core.config import Config
        cfg = Config()
        snap = cfg.all()
        self.assertIn("port", snap)
        self.assertIn("plugins", snap)


# ── State / Timeutil ─────────────────────────────────────────────────────────
class TestStateTimeutil(unittest.TestCase):
    def test_execution_states(self):
        from astra.core.state import EXECUTION_STATES, valid_execution_state
        self.assertIn("EXECUTING", EXECUTION_STATES)
        self.assertTrue(valid_execution_state("COMPLETED"))
        self.assertFalse(valid_execution_state("GARBAGE"))

    def test_duration_ms(self):
        from astra.core.timeutil import duration_ms, ms_now
        t0 = ms_now()
        time.sleep(0.05)
        d = duration_ms(t0)
        self.assertGreater(d, 30)


# ── bootstrap.build() ─────────────────────────────────────────────────────────
class TestBootstrap(unittest.TestCase):
    def test_build_returns_all_keys(self):
        s = make_stack(with_scheduler=True)
        for key in ("config", "store", "plugins", "events", "policy",
                     "memory", "experiences", "tasks", "registry", "router",
                     "planner", "executor", "orchestrator", "workflows",
                     "scheduler", "agent"):
            self.assertIn(key, s)
        self.assertIsNotNone(s["scheduler"])

    def test_build_no_scheduler(self):
        s = make_stack(with_scheduler=False)
        self.assertIsNone(s["scheduler"])

    def test_agent_handle_uses_orchestrator(self):
        s = make_stack()
        reply = s["agent"].handle("get_health")
        self.assertIn("reply", reply)


# ── Web endpoints (system) ────────────────────────────────────────────────────
class TestWebSystem(unittest.TestCase):
    """Live HTTP tests against a real AstraServer on port 0 (ephemeral)."""
    def setUp(self):
        import threading, time
        from astra.web import AstraServer
        self.stack = make_stack(with_scheduler=True)
        self.srv = AstraServer(("127.0.0.1", 0), self.stack["store"],
                                self.stack["agent"], self.stack["plugins"],
                                stack=self.stack)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start(); time.sleep(0.3)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _get(self, p):
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{p}") as r:
            return json.loads(r.read())

    def _post(self, p, d):
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{p}",
            data=json.dumps(d).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def _patch(self, p, d):
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{p}",
            data=json.dumps(d).encode(), method="PATCH",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def test_health_ok(self):
        r = self._get("/api/health")
        self.assertTrue(r["data"]["ok"])

    def test_tools_count(self):
        r = self._get("/api/tools")
        self.assertGreaterEqual(len(r["data"]["tools"]), 13)

    def test_providers_has_no_offline(self):
        r = self._get("/api/providers")
        self.assertNotIn("offline", r["data"]["providers"])

    def test_memory_round_trip(self):
        self._post("/api/memory", {"content": "test memory", "category": "note"})
        r = self._get("/api/memory/search?query=memory&k=1")
        self.assertTrue(r["data"])
        self.assertIn("memory", r["data"][0]["content"])

    def test_tasks_create_and_list(self):
        self._post("/api/tasks", {"goal": "research eligibility", "type": "research"})
        r = self._get("/api/tasks")
        self.assertTrue(any(t["type"] == "research" for t in r["data"]))

    def test_workflow_define_and_run(self):
        r = self._post("/api/workflows",
                       {"name": "smoke wf", "steps": [{"tool": "get_health", "name": "h"}]})
        wf_id = r["data"]["id"]
        run = self._post(f"/api/workflows/{wf_id}/run", {"params": {}})
        self.assertEqual(run["data"]["status"], "completed")

    def test_schedule_add_disable(self):
        r = self._post("/api/schedules", {"name": "t", "kind": "daily", "value": "09:00"})
        sid = r["data"]["id"]
        r2 = self._patch(f"/api/schedules/{sid}", {"enabled": False})
        self.assertFalse(r2["data"]["enabled"])

    def test_agents_submit_and_history(self):
        r = self._post("/api/agents", {"goal": "get health", "sync": True})
        self.assertIn(r["data"]["status"], ("COMPLETED", "FAILED"))
        hist = self._get("/api/executions")
        self.assertGreater(len(hist["data"]), 0)

    def test_config_has_app_keys(self):
        r = self._get("/api/config")
        self.assertIn("port", r["data"])

    def test_manifest_tabs(self):
        r = self._get("/api/manifest")
        tabs = [t["tab"] for t in r["data"]["tabs"]]
        self.assertIn("live", tabs)
        self.assertIn("dashboard", tabs)
        self.assertIn("airdrop", tabs)

    def test_events_returns_list(self):
        r = self._get("/api/events")
        self.assertIsInstance(r["data"], list)


# ── Orchestrator recovery + idempotency (Sections 14–15) ─────────────────────
class TestOrchestratorRecovery(unittest.TestCase):
    def setUp(self):
        self.stack = make_stack()
        self.orch = self.stack["orchestrator"]
        self.store = self.stack["store"]

    def test_recover_stale_marks_running_as_failed(self):
        """Stranded PLANNING/EXECUTING/WAITING_USER executions are terminal-
        failed on restart (Section 14 + 51)."""
        # simulate an execution that was mid-flight when 'the process died'
        eid = "exec-deadbeef"
        self.store.exec(
            "INSERT INTO astra_executions (execution_id, goal, status) "
            "VALUES (?, 'delegated research', 'EXECUTING')", (eid,))
        recovered = self.orch.recover_stale()
        self.assertIn(eid, recovered)
        row = self.orch._row(eid)
        self.assertEqual(row["status"], "FAILED")
        self.assertIn("restart", row["error"])

    def test_recover_stale_keeps_completed(self):
        """Completed executions must never be touched by recovery."""
        r = self.orch.submit("get_health", sync=True)
        recovered = self.orch.recover_stale()
        self.assertNotIn(r["execution_id"], recovered)
        row = self.orch._row(r["execution_id"])
        self.assertEqual(row["status"], "COMPLETED")

    def test_resume_skips_completed_steps(self):
        """Resume is idempotent: already-successful steps aren't re-executed."""
        report = self.orch.submit("plan my day", sync=True)
        results = report["results"]
        done = {k for k, v in results.items() if v.get("ok")}
        self.assertGreater(len(done), 0)
        # simulate: run stuck at WAITING_USER on an already-succeeded step,
        # which is exactly what a crash between save and user-approve leaves
        step = report["plan"][0]
        self.store.exec(
            "UPDATE astra_executions SET status = 'WAITING_USER' "
            "WHERE execution_id = ?", (report["execution_id"],))
        self.store.exec(
            "UPDATE astra_executions SET pending_step = ? "
            "WHERE execution_id = ?",
            (json.dumps(step), report["execution_id"]))
        r2 = self.orch.resume(report["execution_id"], allow=True)
        self.assertEqual(r2["status"], "COMPLETED")
        # no step got a second result entry (no re-execution)
        self.assertEqual(len(r2["results"]), len(results))
        # and every step is still ok
        self.assertTrue(all(v.get("ok") for v in r2["results"].values()))


# ── Store lifecycle (Section 5) ──────────────────────────────────────────────
class TestStoreLifecycle(unittest.TestCase):
    def test_context_manager_closes(self):
        """Store used as a context manager auto-closes on __exit__."""
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with Store(":memory:") as s:
                s.exec("CREATE TABLE t (id INTEGER PRIMARY KEY)")
                s.exec("INSERT INTO t DEFAULT VALUES")
                rows = s.fetch("SELECT * FROM t")
                self.assertEqual(len(rows), 1)
            # connection should now be closed; further use must raise
            with self.assertRaises(Exception):
                s.fetch("SELECT * FROM t")

    def test_rollback_on_exec_error(self):
        """A failed exec() is rolled back — the database stays consistent."""
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with Store(":memory:") as s:
                s.exec("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT UNIQUE)")
                s.exec("INSERT INTO t (val) VALUES ('a')")
                # this INSERT violates UNIQUE and must fail
                with self.assertRaises(Exception):
                    s.exec("INSERT INTO t (val) VALUES ('a')")
                # the previous valid row must still be there
                rows = s.fetch("SELECT * FROM t")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["val"], "a")

    def test_close_is_idempotent(self):
        """Calling close() twice does not raise."""
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            s = Store(":memory:")
            s.close()
            s.close()   # must not raise


# ── EventBus kind validation (Section 3) ─────────────────────────────────────
class TestEventBusValidation(unittest.TestCase):
    def test_known_kind_no_warning(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            s = make_stack()
            s["events"].emit("agent.started", agent="t")   # must not warn

    def test_unknown_kind_warns(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("always")
            ev = make_stack()["events"]
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                ev.emit("nonexistent.event", agent="test")
                self.assertTrue(any("unknown event kind" in str(x.message)
                                    for x in w))

    def test_unknown_kind_still_persisted(self):
        """Unknown kinds are stored (permissive, but warned)."""
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ev = make_stack()["events"]
            rec = ev.emit("future.magic", agent="x", val=1)
            self.assertEqual(rec["kind"], "future.magic")
            hist = ev.history(limit=10)
            self.assertTrue(any(e["kind"] == "future.magic" for e in hist))


# ── AI Providers + Router (Phase 4) ───────────────────────────────────────────
class TestPhase4Providers(unittest.TestCase):
    def make_events(self):
        return make_stack()["events"]

    # -- _read_sse (SSE parser shared by both providers) -----------------------
    def test_sse_parser_anthropic_format(self):
        from astra.ai import provider as P
        fake = io.BytesIO(b"event: content_block_delta\n"
                          b'data: {"type":"content_block_delta","delta":{"text":"Hel"}}\n\n'
                          b"event: message_stop\n"
                          b'data: {"type":"message_stop"}\n\n')
        chunks = P._read_sse(fake)
        self.assertEqual([c["type"] for c in chunks],
                         ["content_block_delta", "message_stop"])
        self.assertEqual(chunks[0]["delta"]["text"], "Hel")

    def test_sse_parser_openai_format_with_done(self):
        from astra.ai import provider as P
        payload = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                   b"data: [DONE]\n\n"
                   b'data: {"choices":[{"delta":{"content":"IGNORED"}}]}\n\n')
        chunks = P._read_sse(io.BytesIO(payload))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "hi")

    def test_sse_parser_ignores_garbage(self):
        from astra.ai import provider as P
        payload = b"keep-alive: x\nnot-data at all\n\n" \
                  b"data: {not valid json}\n\n" \
                  b"data: {\"ok\":true}\n\n"
        chunks = P._read_sse(io.BytesIO(payload))
        self.assertEqual(chunks, [{"ok": True}])

    # -- OpenAICompatibleProvider chat() + stream() against a fake HTTP server -
    def _serve(self, handler):
        srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        return srv, port

    def test_openai_chat_parses_reply(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.dumps(
                    {"choices": [{"message": {"content": "hello from openai"}}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *a): pass
        srv, port = self._serve(H)
        try:
            from astra.ai.provider import OpenAICompatibleProvider
            p = OpenAICompatibleProvider(config={
                "AI_BASE_URL": f"http://127.0.0.1:{port}/v1",
                "AI_API_KEY": "sk-test",
            })
            self.assertEqual(p.chat([{"role": "user", "content": "hi"}]),
                             "hello from openai")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_openai_stream_yields_tokens_and_emits_events(self):
        body = (b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"B"}}]}\n\n'
                b"data: [DONE]\n\n")
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *a): pass
        srv, port = self._serve(H)
        try:
            from astra.ai.provider import OpenAICompatibleProvider
            events = self.make_events()
            p = OpenAICompatibleProvider(config={
                "AI_BASE_URL": f"http://127.0.0.1:{port}/v1",
                "AI_API_KEY": "sk-test",
            }, events=events)
            out = list(p.stream([{"role": "user", "content": "hi"}]))
            self.assertEqual(out, ["A", "B"])
            kinds = [e["kind"] for e in events.history(limit=20)]
            self.assertIn("ai.started", kinds)
            self.assertIn("ai.completed", kinds)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_claude_stream_parses_anthropic_delta(self):
        body = (b'data: {"type":"content_block_delta","delta":{"text":"ki"}}\n\n'
                b'data: {"type":"content_block_delta","delta":{"text":"korbo"}}\n\n'
                b'data: {"type":"message_stop"}\n\n')
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *a): pass
        srv, port = self._serve(H)
        try:
            from astra.ai.provider import ClaudeProvider
            events = self.make_events()
            p = ClaudeProvider(api_key="sk-test", events=events,
                               base_url=f"http://127.0.0.1:{port}/v1/messages")
            out = list(p.stream([{"role": "user", "content": "hi"}]))
            self.assertEqual(out, ["ki", "korbo"])
            kinds = [e["kind"] for e in events.history(limit=20)]
            self.assertIn("ai.completed", kinds)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_provider_network_error_emits_ai_failed(self):
        from astra.ai.provider import OpenAICompatibleProvider
        from astra.core.exceptions import ProviderError
        events = self.make_events()
        p = OpenAICompatibleProvider(config={
            "AI_BASE_URL": "http://127.0.0.1:1",  # unreachable port
            "AI_API_KEY": "sk-test",
        }, events=events)
        with self.assertRaises(ProviderError):
            list(p.stream([{"role": "user", "content": "hi"}]))
        kinds = [e["kind"] for e in events.history(limit=20)]
        self.assertIn("ai.failed", kinds)


class TestPhase4Router(unittest.TestCase):
    def test_router_falls_back_to_second_provider(self):
        from astra.ai.provider import AIProvider
        from astra.ai.router import AstraRouter
        from astra.core.exceptions import ProviderError

        class Bad(AIProvider):
            name = "bad"
            models = ["m"]
            def chat(self, messages, model=None, max_tokens=500):
                raise ProviderError("down")
            def health_check(self): return True

        class Good(AIProvider):
            name = "good"
            models = ["m"]
            def chat(self, messages, model=None, max_tokens=500):
                return "works"

        r = AstraRouter(providers=[Bad(), Good()], max_retries=0)
        name, model, reply = r.route([{"role": "user", "content": "hi"}])
        self.assertEqual(name, "good")
        self.assertEqual(reply, "works")

    def test_router_marks_unhealthy_provider_down(self):
        from astra.ai.provider import AIProvider
        from astra.ai.router import AstraRouter

        class AlwaysDown(AIProvider):
            name = "dead"
            models = ["m"]
            def chat(self, messages, model=None, max_tokens=500):
                return "never reached"
            def health_check(self): return False

        r = AstraRouter(providers=[AlwaysDown()], max_retries=0)
        name, model, reply = r.route([{"role": "user", "content": "hi"}])
        self.assertIsNone(name)
        self.assertIn("dead", r.stats()["down"])

    def test_router_stats_track_latency_and_calls(self):
        from astra.ai.provider import AIProvider
        from astra.ai.router import AstraRouter

        class Fast(AIProvider):
            name = "fast"
            models = ["m0"]
            def chat(self, messages, model=None, max_tokens=500):
                return "yo"

        r = AstraRouter(providers=[Fast()], max_retries=0, backoff_s=0.01)
        r.route([{"role": "user", "content": "x"}])
        h = r.health()["fast"]
        self.assertEqual(h["calls"], 1)
        self.assertGreater(len(h["models"]), 0)




# ── Memory 2.0 (Phase 5) ──────────────────────────────────────────────────────
class TestMemory2(unittest.TestCase):
    def setUp(self):
        self.stack = make_stack()
        self.mem = self.stack["memory"]

    def test_save_with_layer_and_importance(self):
        m = self.mem.save("notcoin listing 30 oct", layer="episodic",
                          importance=0.9, confidence=1.0)
        row = self.mem.get(m["id"])
        self.assertEqual(row["layer"], "episodic")
        self.assertAlmostEqual(row["importance"], 0.9)
        self.assertAlmostEqual(row["confidence"], 1.0)
        self.assertFalse(m.get("deduplicated"))

    def test_rejects_bad_layer(self):
        m = self.mem.save("fallback note", layer="not-a-layer")
        row = self.mem.get(m["id"])
        self.assertEqual(row["layer"], "long")

    def test_exact_duplicate_deduplicated_on_save(self):
        a = self.mem.save("hamster kombat claim reminder")
        b = self.mem.save("hamster kombat claim reminder", importance=0.95)
        self.assertTrue(b.get("deduplicated"))
        self.assertEqual(a["id"], b["id"])
        # same row, importance merged up to the max
        self.assertGreaterEqual(self.mem.get(a["id"])["importance"], 0.95)

    def test_search_ranks_high_importance_first(self):
        self.mem.save("test query low value", importance=0.1)
        self.mem.save("test query important item", importance=1.0)
        top = self.mem.search("test query", k=2)
        self.assertEqual(top[0]["importance"], 1.0)
        # ranks: important one first, low second

    def test_search_layer_and_min_importance_filter(self):
        self.mem.save("layer filter note", layer="short", importance=0.9)
        self.mem.save("layer filter note", layer="working", importance=0.1)
        only_short = self.mem.search("layer filter", layer="short")
        self.assertEqual(len(only_short), 1)
        self.assertEqual(only_short[0]["layer"], "short")
        important = self.mem.search("layer filter", min_importance=0.5)
        self.assertTrue(all(r["importance"] >= 0.5 for r in important))

    def test_touch_updates_recency_and_count(self):
        m = self.mem.save("touched memory fact")
        self.mem.touch(m["id"])
        row = self.mem.get(m["id"])
        self.assertEqual(row["access_count"], 1)
        self.assertTrue(row["last_accessed"])

    def test_recall_search_touches_memories(self):
        m = self.mem.save("recalled memory item")
        self.mem.search("recalled memory", k=5)
        self.assertEqual(self.mem.get(m["id"])["access_count"], 1)

    def test_deduplicate_collapses_legacy_duplicates(self):
        # legacy-style rows: same exact content inserted directly via SQL
        now = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        a = self.stack["store"].insert("astra_memories", content="dup content here",
                                       category="note", tags="", source="chat",
                                       created_at=now)
        b = self.stack["store"].insert("astra_memories", content="dup content here",
                                       category="note", tags="", source="chat",
                                       created_at=now)
        removed = self.mem.deduplicate()
        self.assertGreaterEqual(removed, 1)
        self.assertIsNone(self.mem.get(a))   # older one gone
        self.assertIsNotNone(self.mem.get(b))  # newest kept

    def test_stats_by_layer(self):
        self.mem.save("a working scratch", layer="working")
        self.mem.save("a longterm note", layer="long")
        st = self.mem.stats()
        self.assertEqual(st["total"], 2)
        self.assertEqual(st["by_layer"]["working"], 1)
        self.assertEqual(st["by_layer"]["long"], 1)

    def test_old_schema_db_self_heals_columns(self):
        from tests.helpers import Store as HStore
        store = HStore(":memory:")
        # install the OLD schema (no memory-2.0 columns)
        store.install("""
            CREATE TABLE astra_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                category TEXT DEFAULT 'note',
                tags TEXT DEFAULT '',
                source TEXT DEFAULT 'chat',
                created_at TEXT DEFAULT ''
            )""")
        store.insert("astra_memories", content="old style row", category="note",
                     tags="", source="chat", created_at="2020-01-01 00:00:00")
        events = make_stack()["events"]
        from astra.memory.memory import MemorySystem
        mem = MemorySystem(store, events)
        # columns were added in place; save + recall still work
        m = mem.save("modern row works", importance=0.8)
        row = mem.get(m["id"])
        self.assertEqual(row["importance"], 0.8)
        self.assertGreaterEqual(mem.search("modern")[0]["importance"], 0.8)


# -- Phase 6: ToolRegistry 2.0 ---------------------------------------------
from astra.core.exceptions import ValidationError as _VE
from astra.tools.schemas import Tool as _Tool


class TestRegistryTimeout(unittest.TestCase):
    def setUp(self):
        self.reg = make_stack()["registry"]

    def test_timeout_fires(self):
        def slow(args, ctx=None):
            time.sleep(10)
        self.reg.register(_Tool("slowtool", slow, timeout=0.1))
        with self.assertRaises(Exception) as cm:
            self.reg.execute("slowtool")
        self.assertIn("timed out", str(cm.exception).lower())

    def test_fast_tool_no_timeout(self):
        def fast(args, ctx=None):
            return {"done": True}
        self.reg.register(_Tool("fasttool", fast, timeout=5.0))
        res = self.reg.execute("fasttool")
        self.assertTrue(res["ok"])
        self.assertTrue(res["result"]["done"])


class TestRegistryRetry(unittest.TestCase):
    def setUp(self):
        self.reg = make_stack()["registry"]
        self.call_count = 0

    def test_retry_succeeds_on_third_attempt(self):
        def flaky(args, ctx=None):
            self.call_count += 1
            if self.call_count < 3:
                from astra.core.exceptions import AstraError
                raise AstraError("transient failure")
            return {"ok": True}
        self.reg.register(_Tool("flakytool", flaky, retries=3,
                                retry_backoff_s=0.01))
        res = self.reg.execute("flakytool")
        self.assertTrue(res["ok"])
        self.assertEqual(self.call_count, 3)

    def test_exhausted_retries_raises(self):
        def always_fail(args, ctx=None):
            self.call_count += 1
            from astra.core.exceptions import AstraError
            raise AstraError("permanent failure")
        self.reg.register(_Tool("failtool", always_fail, retries=2,
                                retry_backoff_s=0.01))
        with self.assertRaises(Exception) as cm:
            self.reg.execute("failtool")
        self.assertEqual(self.call_count, 3)  # 1 original + 2 retries
        self.assertIn("permanent failure", str(cm.exception))

    def test_validation_error_not_retried(self):
        def bad_args(args, ctx=None):
            return {}
        self.reg.register(_Tool("stricttool", bad_args, retries=3,
                                input={"x": {"required": True,
                                             "type": "string"}},
                                strict=True, retry_backoff_s=0.01))
        with self.assertRaises(_VE):
            self.reg.execute("stricttool", {"y": "wrong"})


class TestRegistryRateLimit(unittest.TestCase):
    def setUp(self):
        self.reg = make_stack()["registry"]

    def test_rate_limit_enforced(self):
        def instant(args, ctx=None):
            return {"t": time.perf_counter()}
        # 60 RPM = 1 call/sec, so the second call must wait ~0.5s
        self.reg.register(_Tool("ratelimit", instant, rate_limit_per_min=60))
        r1 = self.reg.execute("ratelimit")
        r2 = self.reg.execute("ratelimit")
        elapsed = r2["result"]["t"] - r1["result"]["t"]
        self.assertGreaterEqual(elapsed, 0.3)


class TestRegistryStrictArgs(unittest.TestCase):
    def setUp(self):
        self.reg = make_stack()["registry"]

    def test_strict_rejects_unknown_args(self):
        def dummy(args, ctx=None):
            return {"ok": True}
        self.reg.register(_Tool("strictdummy", dummy,
                                input={"x": {"type": "string"}},
                                strict=True))
        with self.assertRaises(_VE) as cm:
            self.reg.execute("strictdummy", {"x": "ok", "extra_arg": 123})
        self.assertIn("unknown argument", str(cm.exception))

    def test_non_strict_allows_unknown_args(self):
        def dummy(args, ctx=None):
            return {"ok": True}
        self.reg.register(_Tool("loosedummy", dummy,
                                input={"x": {"type": "string"}},
                                strict=False))
        res = self.reg.execute("loosedummy", {"x": "ok", "extra": 123})
        self.assertTrue(res["ok"])


class TestRegistryStats(unittest.TestCase):
    def setUp(self):
        self.reg = make_stack()["registry"]

    def test_stats_tracking(self):
        def work(args, ctx=None):
            time.sleep(0.02)   # ensure measurable duration
            return {"v": 1}
        self.reg.register(_Tool("worktool", work))
        self.reg.execute("worktool")
        self.reg.execute("worktool")
        st = self.reg.stats("worktool")
        self.assertEqual(st["calls"], 2)
        self.assertEqual(st["errors"], 0)
        self.assertGreater(st["total_ms"], 0)
        self.assertGreater(st["average_duration"], 0)
        self.assertNotEqual(st["last_called"], "")

    def test_error_stats(self):
        def boom(args, ctx=None):
            from astra.core.exceptions import AstraError
            raise AstraError("kaboom")
        self.reg.register(_Tool("boomtool", boom, retries=0))
        with self.assertRaises(Exception):
            self.reg.execute("boomtool")
        st = self.reg.stats("boomtool")
        self.assertEqual(st["calls"], 1)
        self.assertEqual(st["errors"], 1)

    def test_stats_all(self):
        def a(args, ctx=None):
            return {}
        def b(args, ctx=None):
            return {}
        self.reg.register(_Tool("stata", a))
        self.reg.register(_Tool("statb", b))
        self.reg.execute("stata")
        self.reg.execute("statb")
        all_stats = self.reg.stats()
        self.assertIn("stata", all_stats)
        self.assertIn("statb", all_stats)



if __name__ == "__main__":
    unittest.main()