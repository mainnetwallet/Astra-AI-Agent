"""HOST terminal FALLBACK + Assistant-Chat approval (spec §1-§19).

Execution priority is fixed and absolute:

  1. **Astra Agent Runtime** — PRIMARY, always tried first, NO permission.
  2. **HOST terminal** — FALLBACK only, and only after the user explicitly
     allows that exact command in the Assistant Chat.

There is no silent host fallback: a failed runtime command never authorises
the host, and a denied approval never runs. These tests pin that end to end:

  * ApprovalManager: a request executes nothing; Allow executes the STORED
    command exactly once (a double click / page refresh / replay is a
    no-op); Deny and expiry never execute; approvals are scoped to a
    conversation/request; the resumer continues the SAME logical operation.
  * HostTerminalFallback / `host_terminal_request`: the Agent's ONLY host
    surface — NOT `agent_forbidden` (the Agent may *ask*) but it executes
    NOTHING, while the raw `terminal_exec` family stays `agent_forbidden`
    and unadvertised in the model catalog.
  * Gateway decision: `host_fallback` degrades to the runtime when no
    fallback exists; `agent_runtime` is never approval-gated.
  * Events: every host event carries `environment="host"` + its approval id;
    runtime execution carries `environment="agent_runtime"`.
  * The web approval API and the chat-card write-back.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.ai.agent_tool_loop import AgentToolLoop, build_tool_catalog
from astra.ai.capability_context import execution_policy_block
from astra.ai.chat_pipeline import ChatPipeline
from astra.ai.gateway_contract import (ENVIRONMENT_HOST_FALLBACK,
                                       ENVIRONMENT_RUNTIME,
                                       ProviderExecutionDecision)
from astra.chat_log import ChatLog
from astra.core.context import ToolContext
from astra.core.permissions import Policy
from astra.runtime.tools import register_runtime_tools
from astra.store import Store
from astra.terminal import TerminalManager, register_terminal_tools
from astra.terminal.approval import (ENVIRONMENT_HOST, STATUS_COMPLETED,
                                     STATUS_DENIED, STATUS_EXPIRED,
                                     STATUS_FAILED, STATUS_PENDING,
                                     ApprovalManager)
from astra.terminal.fallback import (HostTerminalFallback,
                                     register_fallback_tools)
from astra.tools.builtins import register_builtins
from astra.tools.registry import ToolRegistry

from tests.helpers import LocalRuntimeStub, ScriptedBrain


class Bus:
    def __init__(self):
        self.rows = []

    def emit(self, kind, agent="", **data):
        self.rows.append({"kind": kind, "agent": agent, "data": data})

    def kinds(self):
        return [r["kind"] for r in self.rows]

    def find(self, kind):
        return [r for r in self.rows if r["kind"] == kind]


class FakeHost:
    """A stand-in executor: records what it was asked to run and never
    touches the host. Exactly the shape `terminal_exec` returns."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"status": "completed", "exit_code": 0,
                                 "stdout": "ok\n", "stderr": "",
                                 "process_id": "proc-1"}

    def __call__(self, request):
        self.calls.append((request.command, request.cwd))
        return dict(self.result)


def _stack(*, events=None, executor=None):
    policy = Policy(granted=["read", "low_risk_write", "browser_action",
                             "system_action"])
    reg = ToolRegistry(policy=policy, events=events)
    register_builtins(reg)
    host = TerminalManager(events=events)
    register_terminal_tools(reg, host)
    runtime = LocalRuntimeStub(events=events)
    register_runtime_tools(reg, runtime)
    approvals = ApprovalManager(events=events, ttl_s=60)
    fallback = HostTerminalFallback(approvals, registry=reg, terminal=host,
                                    runtime=runtime, events=events)
    register_fallback_tools(reg, fallback)
    if executor is not None:
        approvals.set_executor(executor)
    return reg, host, runtime, approvals, fallback


# ── ApprovalManager: the ONE authorisation point ────────────────────────────

class ApprovalManagerTests(unittest.TestCase):
    def setUp(self):
        self.bus = Bus()
        self.exec = FakeHost()
        self.mgr = ApprovalManager(events=self.bus, ttl_s=60)
        self.mgr.set_executor(self.exec)

    def test_request_records_and_executes_nothing(self):
        req = self.mgr.request(command="echo hi", cwd="/tmp", reason="why",
                               conversation_id=7, request_id="r1")
        self.assertEqual(req.status, STATUS_PENDING)
        self.assertEqual(self.exec.calls, [])          # nothing ran
        self.assertIn("host_terminal.approval_requested", self.bus.kinds())
        ev = self.bus.find("host_terminal.approval_requested")[0]["data"]
        self.assertEqual(ev["environment"], ENVIRONMENT_HOST)
        self.assertEqual(ev["approval_id"], req.approval_id)

    def test_allow_executes_exactly_once(self):
        req = self.mgr.request(command="echo once", cwd="/tmp",
                               conversation_id=7, request_id="r1")
        resolved, _resumed = self.mgr.decide(req.approval_id, True)
        self.assertEqual(resolved.status, STATUS_COMPLETED)
        self.assertEqual(self.exec.calls, [("echo once", "/tmp")])
        # a second click / refresh / SSE replay must NOT run it again
        again, resumed2 = self.mgr.decide(req.approval_id, True)
        self.assertEqual(again.status, STATUS_COMPLETED)
        self.assertIsNone(resumed2)
        self.assertEqual(self.exec.calls, [("echo once", "/tmp")])
        self.assertEqual(self.bus.kinds().count("host_terminal.started"), 1)

    def test_deny_never_executes(self):
        req = self.mgr.request(command="rm -rf /", cwd="/",
                               conversation_id=7, request_id="r1")
        resolved, _resumed = self.mgr.decide(req.approval_id, False)
        self.assertEqual(resolved.status, STATUS_DENIED)
        self.assertEqual(self.exec.calls, [])
        self.assertIn("host_terminal.approval_denied", self.bus.kinds())
        self.assertNotIn("host_terminal.started", self.bus.kinds())

    def test_expiry_blocks_execution(self):
        req = self.mgr.request(command="echo late", conversation_id=7,
                               request_id="r1")
        req.expires_at = time.time() - 1.0          # already stale
        self.mgr.decide(req.approval_id, True)
        self.assertEqual(self.exec.calls, [])
        self.assertEqual(req.status, STATUS_EXPIRED)

    def test_unknown_id_is_a_clean_none(self):
        self.assertEqual(self.mgr.decide("ap-does-not-exist", True),
                         (None, None))
        self.assertIsNone(self.mgr.get("ap-does-not-exist"))

    def test_resumer_continues_the_same_operation(self):
        seen = {}

        def resumer(request, operation, result, allowed):
            seen["operation"] = operation
            seen["result"] = result
            seen["allowed"] = allowed
            return {"reply": "continued", "action": "none", "ok": True,
                    "data": {}}

        self.mgr.set_resumer(resumer)
        req = self.mgr.request(command="echo hi", conversation_id=7,
                               request_id="r1", session_id="conv-7")
        self.mgr.attach_operation(req.approval_id, {"message": "do it",
                                                    "criteria": ["c1"]})
        _r, resumed = self.mgr.decide(req.approval_id, True)
        self.assertEqual(resumed["reply"], "continued")
        self.assertTrue(seen["allowed"])
        self.assertEqual(seen["result"]["exit_code"], 0)
        self.assertEqual(seen["operation"]["message"], "do it")
        self.assertEqual(seen["operation"]["criteria"], ["c1"])

    def test_deny_still_produces_a_resumed_reply(self):
        seen = {}

        def resumer(request, operation, result, allowed):
            seen["allowed"] = allowed
            seen["result"] = result
            return {"reply": "explaining", "action": "none", "ok": True,
                    "data": {}}

        self.mgr.set_resumer(resumer)
        req = self.mgr.request(command="echo hi", conversation_id=7)
        self.mgr.decide(req.approval_id, False)
        self.assertFalse(seen["allowed"])
        self.assertIsNone(seen["result"])

    def test_failed_execution_is_reported_not_claimed(self):
        self.exec.result = {"status": "failed", "exit_code": 1,
                            "stdout": "", "stderr": "boom"}
        req = self.mgr.request(command="false", conversation_id=7)
        resolved, _ = self.mgr.decide(req.approval_id, True)
        self.assertEqual(resolved.status, STATUS_FAILED)
        self.assertIn("boom", resolved.error)

    def test_scoped_to_conversation(self):
        a = self.mgr.request(command="a", conversation_id=1)
        b = self.mgr.request(command="b", conversation_id=2)
        self.assertEqual([r.approval_id for r in self.mgr.pending(1)],
                         [a.approval_id])
        self.assertEqual([r.approval_id for r in self.mgr.pending(2)],
                         [b.approval_id])
        self.assertIsNone(self.mgr.pending_for(99))

    def test_different_command_needs_a_different_approval(self):
        """`decide` runs the STORED command, so a second, different command
        can never ride an existing approval."""
        first = self.mgr.request(command="echo one", conversation_id=7)
        self.mgr.decide(first.approval_id, True)
        self.assertEqual(self.exec.calls, [("echo one", "")])
        second = self.mgr.request(command="echo two", conversation_id=7)
        self.assertNotEqual(first.approval_id, second.approval_id)
        self.assertEqual(self.exec.calls, [("echo one", "")])
        self.mgr.decide(second.approval_id, True)
        self.assertEqual(self.exec.calls, [("echo one", ""),
                                           ("echo two", "")])

    def test_unwired_executor_fails_closed(self):
        mgr = ApprovalManager(events=self.bus, ttl_s=60)
        req = mgr.request(command="echo hi", conversation_id=7)
        resolved, _ = mgr.decide(req.approval_id, True)
        self.assertEqual(resolved.status, STATUS_FAILED)
        self.assertFalse(resolved.executed)


# ── the Agent-facing fallback tool ──────────────────────────────────────────

class FallbackToolTests(unittest.TestCase):
    def test_request_tool_is_not_agent_forbidden_but_executes_nothing(self):
        bus = Bus()
        exec_ = FakeHost()
        reg, host, runtime, approvals, fb = _stack(events=bus, executor=exec_)
        try:
            tool = reg.get("host_terminal_request")
            self.assertIsNotNone(tool)
            self.assertFalse(tool.agent_forbidden)   # the Agent may *ask*
            ctx = ToolContext(agent_execution=True, terminal=host,
                              runtime=runtime, approvals=approvals,
                              fallback=fb, execution_scope="7",
                              request_id="req-1", runtime_session_id="conv-7")
            out = reg.execute("host_terminal_request",
                              {"command": "echo host-only", "cwd": "/tmp",
                               "reason": "runtime cannot do this"}, ctx=ctx)
            self.assertTrue(out["ok"])
            result = out["result"]
            self.assertEqual(result["decision"], "approval_required")
            self.assertFalse(result["executed"])
            self.assertEqual(exec_.calls, [])        # NOTHING ran
            self.assertTrue(result["approval"]["approval_id"])
            # scoped to THIS conversation + request
            self.assertEqual(result["approval"]["conversation_id"], "7")
            self.assertEqual(result["approval"]["request_id"], "req-1")
        finally:
            host.close_all()
            runtime.close_all()

    def test_raw_host_terminal_stays_agent_forbidden(self):
        reg, host, runtime, _approvals, _fb = _stack()
        try:
            for name in ("terminal_exec", "terminal_start", "terminal_stop",
                         "terminal_kill", "terminal_status"):
                self.assertTrue(reg.get(name).agent_forbidden, name)
            out = reg.execute("terminal_exec",
                              {"command": "touch astra_host_must_not_exist"},
                              ctx=ToolContext(agent_execution=True,
                                              terminal=host))
            self.assertFalse(out["ok"])
            self.assertEqual(out["decision"], "blocked")
        finally:
            host.close_all()
            runtime.close_all()

    def test_catalog_advertises_the_request_but_not_raw_exec(self):
        reg, host, runtime, _a, _f = _stack()
        try:
            catalog = build_tool_catalog(reg)
            self.assertIn("host_terminal_request", catalog)
            for name in ("terminal_exec", "terminal_start", "terminal_kill"):
                self.assertNotIn(name, catalog)
        finally:
            host.close_all()
            runtime.close_all()

    def test_unwired_fallback_reports_unavailable(self):
        reg, host, runtime, approvals, fb = _stack()
        try:
            dead = HostTerminalFallback(None, registry=reg)
            out = dead.request_command(command="echo x")
            self.assertFalse(out["ok"])
            self.assertEqual(out["decision"], "unavailable")
            self.assertFalse(out["executed"])
        finally:
            host.close_all()
            runtime.close_all()

    def test_agent_loop_host_request_executes_nothing(self):
        """A scripted model that asks for host access gets an
        `approval_required` result — and no host command runs."""
        bus = Bus()
        exec_ = FakeHost()
        reg, host, runtime, approvals, fb = _stack(events=bus, executor=exec_)
        try:
            brain = ScriptedBrain([
                json.dumps({"action": "tool", "tool": "host_terminal_request",
                            "args": {"command": "echo host-only",
                                     "cwd": "/tmp",
                                     "reason": "runtime cannot do this"},
                            "thought": "ask the user"}),
                json.dumps({"action": "final",
                            "answer": "approval is waiting in the chat"}),
            ])
            loop = AgentToolLoop(reg, terminal=host, runtime=runtime,
                                 events=bus, approvals=approvals,
                                 fallback=fb)
            res = loop.run("do a host-only thing", brain, system_prompt="",
                           session_id="conv-7", scope="7", trace="req-9")
            self.assertTrue(res.ok)
            step = res.steps[0]
            self.assertEqual(step.result.get("decision"), "approval_required")
            self.assertEqual(exec_.calls, [])        # nothing ran on the host
            pending = approvals.pending_for_request("req-9")
            self.assertIsNotNone(pending)
        finally:
            host.close_all()
            runtime.close_all()

    def test_failed_runtime_command_never_touches_the_host(self):
        """A runtime failure is information, not an authorisation (§15):
        nothing runs on the host and no approval is auto-created."""
        bus = Bus()
        exec_ = FakeHost()
        reg, host, runtime, approvals, fb = _stack(events=bus, executor=exec_)
        try:
            brain = ScriptedBrain([
                json.dumps({"action": "tool", "tool": "runtime_command",
                            "args": {"command": "false"},
                            "thought": "run the tests"}),
                json.dumps({"action": "final", "answer": "the tests failed"}),
            ])
            loop = AgentToolLoop(reg, terminal=host, runtime=runtime,
                                 events=bus, approvals=approvals, fallback=fb)
            res = loop.run("run the tests", brain, system_prompt="",
                           session_id="conv-7", scope="7", trace="req-fail")
            self.assertTrue(res.ok)
            self.assertFalse(res.steps[0].ok, "the runtime command failed")
            self.assertEqual(exec_.calls, [])        # NOTHING ran on the host
            self.assertIsNone(approvals.pending_for_request("req-fail"))
            self.assertNotIn("host_terminal.started", bus.kinds())
            self.assertNotIn("host_terminal.approval_requested", bus.kinds())
        finally:
            host.close_all()
            runtime.close_all()


# ── Gateway execution decision ──────────────────────────────────────────────

class GatewayDecisionTests(unittest.TestCase):
    def _decision(self):
        return ProviderExecutionDecision.from_dict({
            "required": True, "capability": "terminal",
            "environment": "host_fallback", "approval_required": True,
            "intent": "run a host-only command"})

    def test_host_fallback_dropped_when_unavailable(self):
        n = self._decision().normalized(["terminal"],
                                        host_fallback_available=False)
        self.assertEqual(n.environment, ENVIRONMENT_RUNTIME)
        self.assertFalse(n.approval_required)
        self.assertIn("runtime", n.reason.lower())

    def test_host_fallback_kept_when_available(self):
        n = self._decision().normalized(["terminal"],
                                        host_fallback_available=True)
        self.assertEqual(n.environment, ENVIRONMENT_HOST_FALLBACK)
        self.assertTrue(n.approval_required)

    def test_agent_runtime_is_never_approval_gated(self):
        d = ProviderExecutionDecision.from_dict({
            "required": True, "capability": "terminal",
            "environment": "agent_runtime", "approval_required": True})
        n = d.normalized(["terminal"], host_fallback_available=True)
        self.assertEqual(n.environment, ENVIRONMENT_RUNTIME)
        self.assertFalse(n.approval_required)

    def test_unknown_environment_degrades_to_runtime(self):
        d = ProviderExecutionDecision.from_dict({
            "required": True, "capability": "terminal",
            "environment": "somewhere-else"})
        self.assertEqual(d.normalized(["terminal"]).environment,
                         ENVIRONMENT_RUNTIME)

    def test_context_block_states_the_approval_rule(self):
        block = self._decision().context_block()
        self.assertIn("host_terminal_request", block)
        self.assertIn("approval", block.lower())

    def test_policy_block_reports_pending_approvals(self):
        block = execution_policy_block(
            runtime_available=True, runtime_status="running",
            host_fallback_available=True,
            pending_approvals=("Host terminal fallback — pending user "
                               "approval:\n- approval_id=ap-1 status=pending"))
        self.assertIn("PRIMARY", block)
        self.assertIn("FALLBACK", block)
        self.assertIn("ap-1", block)


# ── events carry their environment ──────────────────────────────────────────

class EventEnvironmentTests(unittest.TestCase):
    def test_host_events_are_tagged_host(self):
        bus = Bus()
        mgr = ApprovalManager(events=bus, ttl_s=60)
        mgr.set_executor(FakeHost())
        req = mgr.request(command="echo hi", conversation_id=7)
        mgr.decide(req.approval_id, True)
        for kind in ("host_terminal.approval_requested",
                     "host_terminal.approval_allowed",
                     "host_terminal.started", "host_terminal.completed"):
            rows = bus.find(kind)
            self.assertTrue(rows, kind)
            self.assertEqual(rows[0]["data"].get("environment"),
                             ENVIRONMENT_HOST, kind)
            self.assertTrue(rows[0]["data"].get("approval_id"), kind)

    def test_runtime_events_are_tagged_agent_runtime(self):
        from astra.runtime.engine import RuntimeEngine
        if not RuntimeEngine().available():
            self.skipTest("Agent Runtime (proot) unavailable")
        from astra.runtime.manager import RuntimeManager
        bus = Bus()
        base = tempfile.mkdtemp(prefix="astra_evt_",
                                dir=os.path.expanduser("~"))
        rt = None
        try:
            rt = RuntimeManager(events=bus, base_dir=base).default()
            rt.start()
            rt.exec_command("echo hi", session_id="e")
            for kind in ("terminal.started", "terminal.completed"):
                rows = bus.find(kind)
                self.assertTrue(rows, kind)
                self.assertEqual(rows[-1]["data"].get("environment"),
                                 ENVIRONMENT_RUNTIME, kind)
        finally:
            if rt is not None:
                rt.close_all()
            shutil.rmtree(base, ignore_errors=True)


# ── chat card + pipeline wiring ─────────────────────────────────────────────

class ChatApprovalCardTests(unittest.TestCase):
    def test_attach_approval_renders_the_card_and_remembers_the_op(self):
        bus = Bus()
        mgr = ApprovalManager(events=bus, ttl_s=60)
        pipe = ChatPipeline(None, None, approvals=mgr)
        req = mgr.request(command="echo hi", cwd="/tmp", reason="why",
                          conversation_id=7, request_id="r1")
        out = pipe._attach_approval(
            {"reply": "", "action": "none", "ok": True, "data": {}},
            "r1", 7, "conv-7", "original message",
            [{"role": "user", "content": "hi"}], ["c1"])
        self.assertEqual(out["action"], "host_approval")
        self.assertTrue(out["data"]["host_execution"])
        self.assertEqual(out["data"]["approval"]["approval_id"],
                         req.approval_id)
        op = mgr.operation(req.approval_id)
        self.assertEqual(op["message"], "original message")
        self.assertEqual(op["session_id"], "conv-7")
        self.assertEqual(op["criteria"], ["c1"])

    def test_attach_approval_is_a_noop_without_a_request(self):
        mgr = ApprovalManager(ttl_s=60)
        pipe = ChatPipeline(None, None, approvals=mgr)
        reply = {"reply": "normal", "action": "none", "ok": True, "data": {}}
        self.assertIs(pipe._attach_approval(reply, "r-none", 7, "s", "m", [],
                                            []), reply)


class ChatLogWriteBackTests(unittest.TestCase):
    def test_final_decision_is_written_back_into_the_card(self):
        store = Store(":memory:")
        log = ChatLog(store)
        rid = log.add_reply({"reply": "waiting", "action": "host_approval",
                             "ok": True,
                             "data": {"approval": {"approval_id": "ap-1",
                                                   "status": "pending"}}})
        self.assertTrue(log.update_message_meta(
            rid, {"approval": {"approval_id": "ap-1", "status": "denied"}}))
        history = log.history()
        card = [m for m in history["messages"] if m["id"] == rid][0]
        self.assertEqual(card["data"]["approval"]["status"], "denied")
        self.assertEqual(card["action"], "host_approval")
        store.close()

    def test_meta_writer_refuses_empty_payloads(self):
        store = Store(":memory:")
        log = ChatLog(store)
        rid = log.add_reply({"reply": "x", "action": "none", "data": {}})
        self.assertFalse(log.update_message_meta(rid, {}))
        self.assertFalse(log.update_message_meta(rid, {"approval": {}}))
        store.close()


# ── web approval API ────────────────────────────────────────────────────────

class ApprovalApiTests(unittest.TestCase):
    def setUp(self):
        from astra.web import AstraSite, WebApp
        from tests.helpers import make_stack
        self.stack = make_stack()
        self.approvals = self.stack["approvals"]
        self.exec = FakeHost()
        self.approvals.set_executor(self.exec)
        self.site = AstraSite(("127.0.0.1", 0), self.stack["store"],
                              self.stack["agent"], stack=self.stack)
        self.app = WebApp(self.site)

    def tearDown(self):
        self.stack["store"].close()

    def _call(self, method, path, body=None):
        from astra.web import Request
        kw = {}
        if body is not None:
            kw["body"] = body
            kw["headers"] = {"Content-Type": "application/json"}
        resp = self.app.handle(Request(method, path, **kw))
        return resp.status, json.loads(resp.body.decode("utf-8"))

    def test_create_get_list_resolve(self):
        status, body = self._call("POST", "/api/terminal/approval",
                                  {"command": "echo hi", "cwd": "/tmp",
                                   "reason": "why"})
        self.assertEqual(status, 201)
        approval = body["data"]["approval"]
        ap_id = approval["approval_id"]
        self.assertEqual(approval["status"], "pending")
        self.assertEqual(self.exec.calls, [])

        status, body = self._call("GET", "/api/terminal/approval/" + ap_id)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["approval"]["approval_id"], ap_id)

        status, body = self._call("GET", "/api/terminal/approvals")
        self.assertEqual(status, 200)
        self.assertTrue(any(a["approval_id"] == ap_id
                            for a in body["data"]["approvals"]))

        status, body = self._call("POST", "/api/terminal/approval/" + ap_id,
                                  {"decision": "allow"})
        self.assertEqual(status, 200)
        self.assertEqual(self.exec.calls, [("echo hi", "/tmp")])
        self.assertIn(body["data"]["data"]["approval"]["status"],
                      ("approved", "completed"))

    def test_resolve_is_exactly_once(self):
        _s, body = self._call("POST", "/api/terminal/approval",
                              {"command": "echo once"})
        ap_id = body["data"]["approval"]["approval_id"]
        self._call("POST", "/api/terminal/approval/" + ap_id,
                   {"decision": "allow"})
        self._call("POST", "/api/terminal/approval/" + ap_id,
                   {"decision": "allow"})
        self.assertEqual(self.exec.calls, [("echo once", "")])

    def test_deny_never_executes(self):
        _s, body = self._call("POST", "/api/terminal/approval",
                              {"command": "echo nope"})
        ap_id = body["data"]["approval"]["approval_id"]
        _s, body = self._call("POST", "/api/terminal/approval/" + ap_id,
                              {"decision": "deny"})
        self.assertEqual(self.exec.calls, [])
        self.assertEqual(body["data"]["data"]["approval"]["status"], "denied")

    def test_errors(self):
        status, _ = self._call("POST", "/api/terminal/approval", {})
        self.assertEqual(status, 400)
        status, _ = self._call("GET", "/api/terminal/approval/ap-nope")
        self.assertEqual(status, 404)
        _s, body = self._call("POST", "/api/terminal/approval",
                              {"command": "echo x"})
        ap_id = body["data"]["approval"]["approval_id"]
        status, _ = self._call("POST", "/api/terminal/approval/" + ap_id,
                               {"decision": "maybe"})
        self.assertEqual(status, 400)


# ── conversation isolation (audit fix) ──────────────────────────────────────

class ApprovalConversationIsolationTests(unittest.TestCase):
    """Allow/Deny must resume into the approval's OWN conversation, even when
    the user has switched to a different chat before deciding.

    Regression for the audit finding: `_terminal_approval_resolve` used
    `chat_log.current_id`, so resolving from another open conversation
    redirected the continuation (and the card write-back) into that chat.
    """

    def setUp(self):
        from astra.web import AstraSite, WebApp
        from tests.helpers import make_stack
        self.stack = make_stack()
        self.approvals = self.stack["approvals"]
        self.exec = FakeHost()
        self.approvals.set_executor(self.exec)
        self.seen = {}

        def resumer(request, operation, result, allowed):
            self.seen = {"request": request, "operation": operation,
                         "result": result, "allowed": allowed}
            return {"reply": "continued", "action": "none", "ok": True,
                    "data": {}}

        self.approvals.set_resumer(resumer)
        self.site = AstraSite(("127.0.0.1", 0), self.stack["store"],
                              self.stack["agent"], stack=self.stack)
        self.app = WebApp(self.site)
        self.log = self.site.chat_log

    def tearDown(self):
        self.stack["store"].close()

    def _call(self, method, path, body=None):
        from astra.web import Request
        kw = {}
        if body is not None:
            kw["body"] = body
            kw["headers"] = {"Content-Type": "application/json"}
        resp = self.app.handle(Request(method, path, **kw))
        return resp.status, json.loads(resp.body.decode("utf-8"))

    def _create_in(self, conv_id, command="echo hi"):
        _s, body = self._call("POST", "/api/terminal/approval",
                              {"command": command,
                               "conversation_id": conv_id})
        return body["data"]["approval"]

    def _open_second_conversation(self):
        # Give the current chat a message so new_conversation() opens a NEW
        # thread instead of reusing the (still empty) current one, then switch.
        self.log.add_user("seed", conversation_id=self.log.current_id)
        return self.log.new_conversation()

    def test_allow_resumes_into_the_bound_conversation_not_the_open_one(self):
        conv_a = self.log.current_id
        approval = self._create_in(conv_a)
        ap_id = approval["approval_id"]
        self.assertEqual(approval["conversation_id"], conv_a)

        conv_b = self._open_second_conversation()
        self.assertNotEqual(conv_a, conv_b)
        self.assertEqual(self.log.current_id, conv_b)
        before_a = len(self.log.history(conversation_id=conv_a)["messages"])
        before_b = len(self.log.history(conversation_id=conv_b)["messages"])

        status, resp = self._call("POST", "/api/terminal/approval/" + ap_id,
                                  {"decision": "allow"})
        self.assertEqual(status, 200)
        self.assertEqual(resp["data"]["conversation_id"], conv_a,
                         "the reply reports the ORIGINAL conversation")
        self.assertEqual(self.exec.calls, [("echo hi", "")])

        msgs_a = self.log.history(conversation_id=conv_a)["messages"]
        msgs_b = self.log.history(conversation_id=conv_b)["messages"]
        self.assertEqual(len(msgs_a), before_a + 1, "reply written to A")
        self.assertEqual(len(msgs_b), before_b, "B is untouched")
        self.assertEqual(self.log.current_id, conv_b, "open chat unchanged")

        # the SAME logical operation was resumed, bound to A
        self.assertEqual(self.seen["request"].conversation_id, conv_a)
        self.assertTrue(self.seen["allowed"])

        # the card metadata write-back is bound to a message inside A
        ap = self.approvals.get(ap_id)
        self.assertIsNotNone(ap.message_id)
        self.assertIn(ap.message_id, [m["id"] for m in msgs_a])
        self.assertNotIn(ap.message_id, [m["id"] for m in msgs_b])

    def test_deny_resumes_into_the_bound_conversation(self):
        conv_a = self.log.current_id
        approval = self._create_in(conv_a, command="echo nope")
        ap_id = approval["approval_id"]
        conv_b = self._open_second_conversation()
        before_a = len(self.log.history(conversation_id=conv_a)["messages"])
        before_b = len(self.log.history(conversation_id=conv_b)["messages"])

        status, resp = self._call("POST", "/api/terminal/approval/" + ap_id,
                                  {"decision": "deny"})
        self.assertEqual(status, 200)
        self.assertEqual(resp["data"]["conversation_id"], conv_a)
        self.assertEqual(self.exec.calls, [], "deny never runs the command")

        msgs_a = self.log.history(conversation_id=conv_a)["messages"]
        msgs_b = self.log.history(conversation_id=conv_b)["messages"]
        self.assertEqual(len(msgs_a), before_a + 1, "denial reply written to A")
        self.assertEqual(len(msgs_b), before_b, "B is untouched")
        self.assertEqual(self.log.current_id, conv_b)
        self.assertFalse(self.seen["allowed"])

    def test_same_conversation_flow_still_works(self):
        conv_a = self.log.current_id
        approval = self._create_in(conv_a, command="echo same")
        before = len(self.log.history(conversation_id=conv_a)["messages"])
        status, resp = self._call(
            "POST", "/api/terminal/approval/" + approval["approval_id"],
            {"decision": "allow"})
        self.assertEqual(status, 200)
        self.assertEqual(resp["data"]["conversation_id"], conv_a)
        self.assertEqual(self.exec.calls, [("echo same", "")])
        msgs = self.log.history(conversation_id=conv_a)["messages"]
        self.assertEqual(len(msgs), before + 1)


if __name__ == "__main__":
    unittest.main()
