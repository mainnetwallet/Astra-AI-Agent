"""Tests for the Personal-OS core subsystems (EventBus, TaskEngine, ToolRegistry,
Memory, Experience, Workflow, Scheduler, Planner, Orchestrator, Provider,
Config, Context/State/Timeutil, bootstrap)."""
import json, os, time, unittest
from datetime import datetime, timedelta
from helpers import make_stack, Store

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

    def test_stats(self):
        # Stats are only incremented on error/ask paths (not successful runs)
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

    def test_providers_has_offline(self):
        r = self._get("/api/providers")
        self.assertIn("offline", r["data"]["providers"])

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


if __name__ == "__main__":
    unittest.main()
