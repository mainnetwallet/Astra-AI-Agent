"""Lifecycle correlation contract for the Activity Log.

Every start/request event a subsystem emits must be closed by a terminal event
carrying the SAME correlation id (`op`/`request`/`trace`) so the timeline
updates one row in place instead of leaving a stale "… running" row. These
tests pin the backend half of that contract; the frontend reconciliation rules
are covered by tests/js/log_model.test.js.
"""
from __future__ import annotations

import json
import unittest

from astra.ai.chat_pipeline import ChatPipeline
from astra.ai.router import RoutingRequest, RoutingResult
from astra.ai.gateway_task_completion import GatewayTaskCompletionSupervisor
from tests.helpers import make_stack

TARGETS = [
    {"provider": "groq", "model": "llama-fast", "capabilities": ["chat"],
     "quality": "fast", "context_window": 8000},
]


def understand(final_request="", provider="groq", model="llama-fast"):
    return json.dumps({"final_request": final_request, "was_incomplete": False,
                       "provider": provider, "model": model,
                       "criteria": ["answers"], "reason": "best fit"})


class Bus:
    def __init__(self):
        self.rows = []

    def emit(self, kind, agent="", **data):
        self.rows.append({"kind": kind, "agent": agent, "data": data})
        return {"kind": kind, "agent": agent, "data": data}

    def by(self, kind):
        return [r for r in self.rows if r["kind"] == kind]


class GW:
    def __init__(self, replies=(), usable=True, supervise=None):
        self.replies = list(replies)
        self.usable = usable
        self._supervise = supervise
        self.traces = []

    def is_usable(self):
        return self.usable

    def chat(self, messages, model=None, max_tokens=500, category=None,
             trace=""):
        self.traces.append(trace)
        r = self.replies.pop(0)
        return r

    def supervise_task(self, port, target, messages, result, contract, *,
                       evidence=None, semantic_verifier=None, max_tokens=500):
        if self._supervise is not None:
            return self._supervise()
        return GatewayTaskCompletionSupervisor().supervise(
            port, target, messages, result, contract, evidence=evidence,
            semantic_verifier=semantic_verifier, max_tokens=max_tokens)


class RT:
    def __init__(self, out):
        self.out = out

    def available_targets(self):
        return list(TARGETS)

    def route_request(self, req):
        if self.out is None:
            return RoutingResult(ok=False, error="all providers failed")
        return RoutingResult(ok=True, text=self.out, provider="groq",
                             model="llama-fast")


def _runs(bus, starts, terminals):
    """Assert every started op is closed by exactly one terminal event."""
    started = {r["data"].get("op") for r in bus.by(starts)}
    finished = {r["data"].get("op") for r in bus.by(terminals)}
    return started, finished


class TestChatPipelineTerminalEvents(unittest.TestCase):
    def _emit_kinds(self, pipe, bus, msg="hello"):
        out = pipe.run(msg)
        return out

    def test_happy_path_closes_the_started_row(self):
        bus = Bus()
        pipe = ChatPipeline(GW([understand(), json.dumps(
            {"verdict": "complete", "missing": [], "action": "fix",
             "instructions": ""})]), RT("hi there"), events=bus)
        pipe.run("hello")
        starts = bus.by("chat.pipeline.started")
        self.assertEqual(len(starts), 1)
        req = starts[0]["data"]["request"]
        self.assertEqual(starts[0]["data"]["op"], f"chat:{req}")
        term = bus.by("chat.pipeline.finished")
        self.assertEqual(len(term), 1)
        self.assertEqual(term[0]["data"]["op"], f"chat:{req}")
        self.assertEqual(term[0]["data"]["trace"], req)
        self.assertTrue(term[0]["data"]["terminal"])

    def test_router_failure_closes_the_started_row(self):
        bus = Bus()
        pipe = ChatPipeline(GW([understand()]), RT(None), events=bus)
        pipe.run("hello")
        term = bus.by("chat.pipeline.failed")
        self.assertEqual(len(term), 1)
        self.assertTrue(term[0]["data"]["terminal"])
        self.assertEqual(term[0]["data"]["op"],
                         bus.by("chat.pipeline.started")[0]["data"]["op"])

    def test_gateway_unusable_path_still_emits_a_terminal(self):
        bus = Bus()
        pipe = ChatPipeline(GW(usable=False), RT("hi"), events=bus)
        out = pipe.run("hello")
        self.assertTrue(out["ok"])
        term = bus.by("chat.pipeline.finished")
        self.assertEqual(len(term), 1, "unverified pass-through must terminate")
        self.assertTrue(term[0]["data"]["terminal"])
        self.assertEqual(term[0]["data"]["op"],
                         bus.by("chat.pipeline.started")[0]["data"]["op"])

    def test_verification_crash_closes_the_started_row(self):
        bus = Bus()

        def boom():
            raise RuntimeError("supervisor down")

        pipe = ChatPipeline(GW([understand()], supervise=boom), RT("answer"),
                            events=bus)
        pipe.run("hello")
        term = bus.by("chat.pipeline.verify_error")
        self.assertEqual(len(term), 1)
        self.assertTrue(term[0]["data"]["terminal"])
        self.assertEqual(term[0]["data"]["op"],
                         bus.by("chat.pipeline.started")[0]["data"]["op"])

    def test_gateway_calls_carry_the_request_trace(self):
        bus = Bus()
        gw = GW([understand(), json.dumps(
            {"verdict": "complete", "missing": [], "action": "fix",
             "instructions": ""})])
        pipe = ChatPipeline(gw, RT("hi"), events=bus)
        pipe.run("hello")
        req = bus.by("chat.pipeline.started")[0]["data"]["request"]
        # both Gateway calls (understand + verify) share the turn's trace
        self.assertTrue(req)
        self.assertEqual(gw.traces[0], req)


class TestWorkflowTerminalEvents(unittest.TestCase):
    def test_step_start_is_closed_by_its_terminal(self):
        stack = make_stack()
        wf = stack["workflows"]
        d = wf.define("health", "daily", [{"tool": "get_health", "name": "h"}])
        wf.run(workflow_id=d["id"])
        rows = stack["store"].fetch(
            "SELECT kind, data FROM events ORDER BY id ASC")
        events = [(r["kind"], json.loads(r["data"])) for r in rows]
        starts = [d_ for k, d_ in events if k == "task.started"]
        self.assertEqual(len(starts), 1)
        op = starts[0]["op"]
        self.assertTrue(op.startswith("wf:"))
        terminals = [(k, d_) for k, d_ in events
                     if k in ("task.completed", "task.failed")]
        self.assertEqual(len(terminals), 1, "every step start must terminate")
        self.assertEqual(terminals[0][0], "task.completed")
        self.assertEqual(terminals[0][1]["op"], op)
        self.assertTrue(terminals[0][1]["terminal"])

    def test_workflow_start_and_completion_share_an_op(self):
        stack = make_stack()
        wf = stack["workflows"]
        d = wf.define("health", "daily", [{"tool": "get_health", "name": "h"}])
        wf.run(workflow_id=d["id"])
        rows = stack["store"].fetch(
            "SELECT kind, data FROM events ORDER BY id ASC")
        events = [(r["kind"], json.loads(r["data"])) for r in rows]
        started = [d_ for k, d_ in events if k == "workflow.started"][0]
        done = [d_ for k, d_ in events if k == "workflow.completed"][0]
        self.assertEqual(started["op"], done["op"])
        self.assertTrue(done["terminal"])

    def test_failing_step_emits_task_failed_with_same_op(self):
        stack = make_stack()
        wf = stack["workflows"]
        # A *runtime* step failure: `recall` is a registered tool that raises
        # when its required argument is missing. (An unregistered tool name
        # is now refused at write time by WorkflowEngine.define — see
        # tests/test_workflows.py — so this test drives the run-time path
        # with a real tool that fails while executing.)
        d = wf.define("bad", "daily", [{"tool": "recall", "name": "b"}])
        wf.run(workflow_id=d["id"])
        rows = stack["store"].fetch(
            "SELECT kind, data FROM events ORDER BY id ASC")
        events = [(r["kind"], json.loads(r["data"])) for r in rows]
        started = [d_ for k, d_ in events if k == "task.started"][0]
        failed = [d_ for k, d_ in events if k == "task.failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["op"], started["op"])
        self.assertTrue(failed[0]["terminal"])


class _BoomRouter:
    """Router that raises unexpectedly (a bug, not an honest routing error)."""

    def available_targets(self):
        return list(TARGETS)

    def route_request(self, req):
        raise RuntimeError("router exploded")


class TestUnexpectedErrorsStillTerminate(unittest.TestCase):
    def test_unexpected_step_error_still_closes_the_request(self):
        bus = Bus()
        pipe = ChatPipeline(GW([understand()]), _BoomRouter(), events=bus)
        with self.assertRaises(RuntimeError):
            pipe.run("hello")
        term = bus.by("chat.pipeline.failed")
        self.assertEqual(len(term), 1,
                         "an unexpected error must still emit a terminal event")
        self.assertTrue(term[0]["data"]["terminal"])
        self.assertEqual(term[0]["data"]["op"],
                         bus.by("chat.pipeline.started")[0]["data"]["op"])
        self.assertIn("RuntimeError", term[0]["data"]["error"])


class TestRouterGatewayCorrelation(unittest.TestCase):
    """The fallback + gateway-recovery events must carry the ROUTE's op/trace
    so the Activity Log refines one "Agent Router" row instead of appending
    orphan rows for each internal step."""

    def _run(self):
        from astra.ai.gateway import AstraAIGateway
        from astra.ai.router import AstraRouter
        from astra.core.events import EventBus
        from astra.store import Store
        from tests.test_gateway_runtime_wiring import RecordingProvider
        store = Store(":memory:")
        bus = EventBus(store)
        gw = AstraAIGateway(connections=[], store=store, events=bus)
        router = AstraRouter(
            providers=[
                RecordingProvider("groq", ["m1"],
                                  fail_models={"m1": (9, "rate limit exceeded")}),
                RecordingProvider("gemini", ["m2"])],
            gateway=gw)
        router.attach_events(bus)
        rr = router.route_request(RoutingRequest(
            task_type="simple_chat",
            messages=[{"role": "user", "content": "hi"}]))
        return rr, bus.history(limit=200)

    def test_fallback_carries_the_route_op_and_precedes_the_decision(self):
        rr, rows = self._run()
        self.assertTrue(rr.ok)
        start = [r for r in rows if r["kind"] == "router.request"][0]
        op = start["data"]["op"]
        self.assertTrue(op)
        decisions = [r for r in rows if r["kind"] == "router.decision"]
        self.assertEqual(len(decisions), 1, "one terminal decision per route")
        self.assertEqual(decisions[0]["data"]["op"], op)
        self.assertTrue(decisions[0]["data"]["terminal"])
        fallbacks = [r for r in rows if r["kind"] == "router.fallback"]
        self.assertEqual(len(fallbacks), 1)
        self.assertEqual(fallbacks[0]["data"]["op"], op,
                         "fallback must refine the route's own row")
        self.assertEqual(fallbacks[0]["data"]["trace"],
                         decisions[0]["data"]["trace"])
        self.assertLess(fallbacks[0]["id"], decisions[0]["id"],
                        "fallback must be emitted before the route terminal")

    def test_gateway_recovery_events_carry_the_route_op(self):
        _, rows = self._run()
        start = [r for r in rows if r["kind"] == "router.request"][0]
        op = start["data"]["op"]
        recovery = [r for r in rows
                    if r["kind"] in ("gateway.target_cooldown",
                                     "gateway.execution_failed",
                                     "gateway.execution_completed",
                                     "gateway.execution_recovered")]
        self.assertTrue(recovery, "a failed attempt must report recovery state")
        for r in recovery:
            self.assertEqual(r["data"].get("op"), op,
                             "%s must carry the route op" % r["kind"])

    def test_every_route_op_is_closed(self):
        _, rows = self._run()
        starts = {r["data"]["op"] for r in rows if r["kind"] == "router.request"}
        terminals = {r["data"]["op"] for r in rows
                     if r["kind"] == "router.decision"}
        self.assertTrue(starts)
        self.assertEqual(starts, terminals,
                         "no route operation may be left running")


class TestEventBusStartupReconciliation(unittest.TestCase):
    """A previous run's in-flight operations can never finish; the next app
    start must close them so the Activity Log shows no permanent RUNNING row."""

    def test_stale_root_is_closed_and_its_child_is_covered(self):
        from astra.core.events import EventBus
        from astra.store import Store
        store = Store(":memory:")
        bus = EventBus(store)
        bus.emit("chat.pipeline.started", op="chat:dead", request="dead",
                 trace="dead")
        # the child shares the request's trace -> the root's own terminal
        # already resolves it, so it must NOT get a second, duplicate one
        bus.emit("ai.started", op="ai-1", trace="dead")
        bus.emit("tool.failed", op="done-1", tool="x", terminal=True)

        next_run = EventBus(store)          # the app starting again
        closed = next_run.reconcile_stale_operations()
        self.assertEqual(closed, 1)
        rows = next_run.history(limit=50)
        interrupted = [r for r in rows if r["kind"] == "operation.interrupted"]
        self.assertEqual([r["data"]["op"] for r in interrupted], ["chat:dead"])
        self.assertTrue(interrupted[0]["data"]["terminal"])
        self.assertEqual(interrupted[0]["data"]["original_kind"],
                         "chat.pipeline.started")
        # running again changes nothing: the operation is already closed
        self.assertEqual(next_run.reconcile_stale_operations(), 0)

    def test_unrelated_stale_starts_each_get_their_own_terminal(self):
        from astra.core.events import EventBus
        from astra.store import Store
        store = Store(":memory:")
        bus = EventBus(store)
        bus.emit("tool.started", op="T1", tool="a")
        bus.emit("astra_gateway.request", op="G1", trace="other")
        bus.emit("operation.interrupted", op="G1", terminal=True)
        next_run = EventBus(store)
        self.assertEqual(next_run.reconcile_stale_operations(), 1)
        ops = {r["data"].get("op") for r in next_run.history(limit=50)
               if r["kind"] == "operation.interrupted"}
        self.assertEqual(ops, {"T1", "G1"})

    def test_recovered_operation_is_not_reported_as_interrupted(self):
        from astra.core.events import EventBus
        from astra.store import Store
        store = Store(":memory:")
        bus = EventBus(store)
        bus.emit("router.request", op="R", trace="T")
        bus.emit("ai.failed", op="R", retrying=True, terminal=False)
        bus.emit("router.decision", op="R", terminal=True)
        self.assertEqual(bus.reconcile_stale_operations(), 0)


if __name__ == "__main__":
    unittest.main()
