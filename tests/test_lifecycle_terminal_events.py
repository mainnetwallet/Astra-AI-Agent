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
from astra.ai.router import RoutingResult
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
        d = wf.define("bad", "daily", [{"tool": "no_such_tool", "name": "b"}])
        wf.run(workflow_id=d["id"])
        rows = stack["store"].fetch(
            "SELECT kind, data FROM events ORDER BY id ASC")
        events = [(r["kind"], json.loads(r["data"])) for r in rows]
        started = [d_ for k, d_ in events if k == "task.started"][0]
        failed = [d_ for k, d_ in events if k == "task.failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["op"], started["op"])
        self.assertTrue(failed[0]["terminal"])


if __name__ == "__main__":
    unittest.main()
