"""Regression: a successful tool execution must never end in the generic
"internal formatting issue" fallback.

Bug: "Check RPC status for Ethereum." ran real RPC commands, then the model's
final reply was protocol-shaped (empty `final`, truncated JSON, ...). The tool
loop returned that raw protocol as the answer and `response_boundary` turned
it into FALLBACK_TEXT, so the user never saw the RPC findings.

Layers covered: response boundary, AgentToolLoop final-answer contract,
ChatPipeline (Gateway -> loop -> verify -> boundary) and HTTP /api/chat.
"""
import json
import os
import sys
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.agent import Agent
from astra.ai.agent_tool_loop import AgentToolLoop, summarize_tool_steps
from astra.ai.chat_pipeline import UNDERSTAND_SYSTEM_PROMPT
from astra.ai.response_boundary import FALLBACK_TEXT, sanitize_final_response
from astra.core.permissions import Policy
from astra.runtime.tools import register_runtime_tools
from astra.tools.builtins import register_builtins
from astra.tools.registry import ToolRegistry
from tests.helpers import LocalRuntimeStub, LiveServer, ScriptedBrain
from tests.test_gateway_provider_tool_architecture import (Harness, final,
                                                           tool_call, verdict)

FAKE_TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
USER_MSG = "Check RPC status for Ethereum."

# What the model emits when it "finishes" with nothing usable.
EMPTY_FINAL = json.dumps({"action": "final", "answer": ""})
TRUNCATED_TOOL = ('{"action": "tool", "tool": "runtime_command", "args": '
                  '{"command": "curl -s https://cloudflare-eth.com -d')
NAMELESS_TOOL = json.dumps({"action": "tool", "tool": "", "args": {},
                            "session_id": "abc", "thought": "hm"})
PROTOCOL_BAD_FINALS = (EMPTY_FINAL, TRUNCATED_TOOL, NAMELESS_TOOL)

RECOVERED = ("Ethereum RPC status: eth.drpc.org and 1rpc.io respond "
             "(block 21000000); eth.llamarpc.com returned HTTP 525.")


def web3_understand(final_request=USER_MSG, *, required=True,
                    capability="web3"):
    return json.dumps({
        "final_request": final_request, "was_incomplete": False,
        "task_type": "web3",
        "provider": "groq", "model": "llama-fast",
        "targets": [{"provider": "groq", "model": "llama-fast"}],
        "criteria": ["the RPC endpoints were actually checked",
                     "the real results were reported"],
        "reason": "live chain state",
        "execution": {"required": required, "capability": capability,
                      "environment": "agent_runtime",
                      "approval_required": False,
                      "intent": "check Ethereum RPC endpoint status"}})


class RpcRuntimeStub(LocalRuntimeStub):
    """Runtime whose process backend returns canned RPC results (no network).
    The runtime tools, ToolRegistry and AgentToolLoop stay the real code."""

    RESULTS = (
        ("eth.drpc.org", 0, '{"result":"0x14fb180"}'),
        ("1rpc.io", 0, '{"result":"0x14fb181"}'),
        ("llamarpc", 22, "curl: (22) The requested URL returned error: 525"),
        ("cloudflare-eth", 0, '{"error":{"code":-32000,"message":"rpc error"}}'),
        ("ankr", 0, '{"error":"authentication required"}'),
    )

    def exec_command(self, command, *, session_id="", timeout=None,
                     rows=24, cols=80):
        self.commands.append((str(session_id), str(command)))
        for needle, code, out in self.RESULTS:
            if needle in command:
                return {"ok": code == 0,
                        "status": "completed" if code == 0 else "failed",
                        "session_id": session_id or "s", "runtime": "test",
                        "command": command, "cwd": "/workspace",
                        "exit_code": code, "stdout": out, "stderr": "",
                        "duration_ms": 3, "truncated": False, "blob_id": ""}
        return {"ok": True, "status": "completed", "session_id": "s",
                "runtime": "test", "command": command, "cwd": "/workspace",
                "exit_code": 0, "stdout": "", "stderr": "",
                "duration_ms": 1, "truncated": False, "blob_id": ""}


def rpc_harness(brain_replies, *, gateway=None):
    h = Harness(gateway if gateway is not None
                else [web3_understand(), verdict("complete")], brain_replies)
    h.runtime = RpcRuntimeStub(events=h.stack["events"])
    h.pipeline.runtime = h.runtime
    return h


def rpc_calls():
    return [tool_call("curl -s https://eth.drpc.org -d '{\"method\":\"eth_blockNumber\"}'"),
            tool_call("curl -s https://1rpc.io/eth -d '{\"method\":\"eth_blockNumber\"}'"),
            tool_call("curl -s https://eth.llamarpc.com -d '{\"method\":\"eth_blockNumber\"}'")]


def assert_clean(tc, reply):
    tc.assertTrue(reply.strip())
    low = reply.lower()
    tc.assertNotEqual(reply, FALLBACK_TEXT)
    tc.assertNotIn("internal formatting issue", low)
    for leak in ('"action"', "session_id", '"thought"', '"tool"', '"args"',
                 "{\"action", "trace"):
        tc.assertNotIn(leak, low)


# ── 1. response boundary ───────────────────────────────────────────────────
class ResponseBoundaryTests(unittest.TestCase):
    def test_valid_final_wrapper_is_unwrapped(self):
        self.assertEqual(sanitize_final_response(
            '{"action":"final","answer":"RPC is healthy."}'), "RPC is healthy.")

    def test_tool_protocol_only_never_leaks(self):
        raw = ('{"action":"tool","tool":"runtime_command","args":'
               '{"command":"ls"},"thought":"x","session_id":"s1"}')
        out = sanitize_final_response(raw)
        self.assertNotIn("runtime_command", out)
        self.assertNotIn("session_id", out)
        self.assertNotIn('"action"', out)

    def test_protocol_plus_prose_keeps_prose(self):
        raw = ('Block is 21000000.\n{"action":"tool","tool":"t","args":{},'
               '"thought":"x"}')
        out = sanitize_final_response(raw)
        self.assertEqual(out, "Block is 21000000.")

    def test_truncated_protocol_never_leaks(self):
        out = sanitize_final_response(TRUNCATED_TOOL)
        self.assertNotIn("runtime_command", out)
        self.assertNotIn("curl", out)
        self.assertNotIn('"action"', out)

    def test_truncated_final_salvages_the_answer(self):
        out = sanitize_final_response(
            '{"action": "final", "answer": "eth.drpc.org is at block 21000000')
        self.assertIn("eth.drpc.org is at block 21000000", out)
        self.assertNotIn("action", out)

    def test_recovered_summary_replaces_generic_fallback(self):
        # E: tools succeeded, model's protocol wrapper stripped -> use the
        # deterministic recovery text, NOT the formatting-error fallback.
        for bad in PROTOCOL_BAD_FINALS + ("",):
            out = sanitize_final_response(bad, fallback=RECOVERED)
            self.assertEqual(out, RECOVERED, bad)

    def test_without_recovery_generic_fallback_is_still_the_last_resort(self):
        self.assertEqual(sanitize_final_response(NAMELESS_TOOL), FALLBACK_TEXT)

    def test_recovery_text_is_redacted_too(self):
        out = sanitize_final_response(
            EMPTY_FINAL, fallback=f"Result used {FAKE_TOKEN} here")
        self.assertNotIn(FAKE_TOKEN, out)

    def test_secret_in_reply_is_redacted(self):
        out = sanitize_final_response(f"token is {FAKE_TOKEN}")
        self.assertNotIn(FAKE_TOKEN, out)

    def test_recovery_text_never_carries_protocol(self):
        out = sanitize_final_response(EMPTY_FINAL, fallback=NAMELESS_TOOL + " ok")
        self.assertNotIn("session_id", out)


# ── 2. AgentToolLoop final-answer contract ─────────────────────────────────
def _loop_stack():
    reg = ToolRegistry(policy=Policy(granted=["read", "low_risk_write",
                                              "browser_action",
                                              "system_action"]))
    register_builtins(reg)
    rt = RpcRuntimeStub()
    register_runtime_tools(reg, rt)
    return reg, rt


def _run_loop(replies, **kw):
    reg, rt = _loop_stack()
    brain = ScriptedBrain(list(replies))
    loop = AgentToolLoop(reg, runtime=rt, **kw)
    res = loop.run(USER_MSG, brain, system_prompt="You are Astra.",
                   session_id="s1", scope="scope-1")
    return res, brain, loop


class ToolLoopFinalContractTests(unittest.TestCase):
    def test_valid_final_answer_is_used(self):
        res, brain, _ = _run_loop(rpc_calls()[:1] + [final("RPC is healthy.")])
        self.assertEqual(res.text, "RPC is healthy.")
        self.assertEqual(len(brain.calls), 2)          # no recovery call

    def test_plain_text_after_tools_is_used(self):
        res, _, _ = _run_loop(rpc_calls()[:1] + ["Block 21000000 on drpc."])
        self.assertEqual(res.text, "Block 21000000 on drpc.")

    def test_prose_around_malformed_protocol_is_preserved(self):
        res, brain, _ = _run_loop(
            rpc_calls()[:1] + ["drpc is up at block 21000000.\n" + TRUNCATED_TOOL])
        self.assertEqual(res.text, "drpc is up at block 21000000.")
        self.assertEqual(len(brain.calls), 2)

    def test_truncated_final_salvages_answer(self):
        res, _, _ = _run_loop(rpc_calls()[:1] + [
            '{"action": "final", "answer": "drpc is up, block 21000000'])
        self.assertIn("drpc is up, block 21000000", res.text)
        self.assertNotIn("action", res.text)

    def test_protocol_only_triggers_one_bounded_recovery_call(self):
        for bad in PROTOCOL_BAD_FINALS:
            res, brain, _ = _run_loop(rpc_calls()[:2] + [bad, RECOVERED])
            self.assertEqual(res.text, RECOVERED, bad)
            self.assertEqual(len(brain.calls), 4, bad)   # 2 tools+bad+recovery
            self.assertEqual(res.tool_calls, 2)
            ask = brain.calls[3][-1]["content"]
            self.assertIn("plain", ask.lower())
            # same conversation: the earlier tool results are still there
            self.assertIn("Tool result", json.dumps(brain.calls[3]))

    def test_recovery_reply_that_is_a_tool_call_is_not_executed(self):
        reg, rt = _loop_stack()
        brain = ScriptedBrain(rpc_calls()[:1] + [EMPTY_FINAL,
                                                 rpc_calls()[1]])
        res = AgentToolLoop(reg, runtime=rt).run(
            USER_MSG, brain, system_prompt="s", session_id="s1", scope="k")
        self.assertEqual(len(rt.commands), 1)            # 2nd curl NOT run
        assert_clean(self, res.text)
        self.assertIn("eth.drpc.org", res.text)          # deterministic summary

    def test_recovery_is_bounded_and_falls_back_to_deterministic_summary(self):
        res, brain, _ = _run_loop(
            rpc_calls() + [EMPTY_FINAL, NAMELESS_TOOL, "never asked again"])
        self.assertEqual(len(brain.calls), 5)            # 3 + bad + ONE recovery
        assert_clean(self, res.text)
        self.assertIn("eth.drpc.org", res.text)
        self.assertIn("525", res.text)
        self.assertNotIn("never asked again", res.text)

    def test_recovery_can_be_disabled(self):
        res, brain, _ = _run_loop(rpc_calls()[:1] + [EMPTY_FINAL, RECOVERED],
                                  max_final_recovery_attempts=0)
        self.assertEqual(len(brain.calls), 2)
        assert_clean(self, res.text)                     # still a safe summary
        self.assertIn("eth.drpc.org", res.text)

    def test_recovery_failure_from_caller_falls_back_to_summary(self):
        res, _, _ = _run_loop(rpc_calls()[:1] + [EMPTY_FINAL,
                                                 RuntimeError("boom")])
        self.assertTrue(res.ok)
        assert_clean(self, res.text)
        self.assertIn("eth.drpc.org", res.text)

    def test_max_steps_exhausted_never_returns_raw_tool_protocol(self):
        res, _, _ = _run_loop(rpc_calls()[:1] * 3,
                              max_steps=2)
        self.assertEqual(res.stopped_reason, "max_steps")
        assert_clean(self, res.text)

    def test_events_show_final_recovery_without_secrets(self):
        from tests.test_agent_tool_loop import Bus
        bus = Bus()
        _run_loop(rpc_calls()[:1] + [EMPTY_FINAL, RECOVERED], events=bus)
        rows = [r for r in bus.rows if r["kind"] == "agent.final_recovery"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["data"]["attempt"], 1)
        self.assertIn("reason", rows[0]["data"])
        self.assertNotIn(FAKE_TOKEN, json.dumps(rows))

    def test_no_tools_ran_keeps_existing_plain_text_behaviour(self):
        res, brain, _ = _run_loop(["just chatting"])
        self.assertEqual(res.text, "just chatting")
        self.assertEqual(len(brain.calls), 1)


class DeterministicSummaryTests(unittest.TestCase):
    def test_summary_uses_real_results_and_hides_internals(self):
        res, _, _ = _run_loop(rpc_calls() + [EMPTY_FINAL, EMPTY_FINAL],
                              max_final_recovery_attempts=1)
        s = summarize_tool_steps(res.steps)
        self.assertIn("eth.drpc.org", s)
        self.assertIn("0x14fb180", s)
        self.assertIn("525", s)
        for leak in ("session_id", "runtime_command", '"action"', "trace"):
            self.assertNotIn(leak, s)

    def test_summary_redacts_secrets(self):
        reg, rt = _loop_stack()
        cmd = f"curl -H 'Authorization: Bearer {FAKE_TOKEN}' https://eth.drpc.org"
        brain = ScriptedBrain([tool_call(cmd), EMPTY_FINAL, EMPTY_FINAL])
        res = AgentToolLoop(reg, runtime=rt).run(
            USER_MSG, brain, system_prompt="s", session_id="s", scope="k")
        self.assertNotIn(FAKE_TOKEN, res.text)
        self.assertNotIn(FAKE_TOKEN, summarize_tool_steps(res.steps))


# ── 3. full pipeline ───────────────────────────────────────────────────────
class RpcStatusPipelineTests(unittest.TestCase):
    def _assert_rpc_findings(self, reply):
        assert_clean(self, reply)
        self.assertIn("eth.drpc.org", reply)

    def test_gateway_web3_classification_is_kept_and_verified(self):
        h = rpc_harness(rpc_calls() + [final(RECOVERED)])
        out = h.run(USER_MSG)
        self.assertEqual(out["data"]["task_type"], "web3")
        self.assertTrue(out["data"]["execution"]["required"])
        self.assertEqual(out["data"]["execution"]["capability"], "web3")
        # web3 is a verified type, never silently downgraded to a NO_VERIFY one
        self.assertEqual(out["data"]["verification"]["status"], "COMPLETE")
        self.assertEqual(out["reply"], RECOVERED)

    def test_protocol_only_final_after_tools_recovers_real_findings(self):
        for bad in PROTOCOL_BAD_FINALS:
            h = rpc_harness(rpc_calls() + [bad, RECOVERED])
            out = h.run(USER_MSG)
            self.assertTrue(out["ok"], bad)
            self.assertEqual(out["reply"], RECOVERED, bad)
            self.assertIn("agent.final_recovery", h.kinds(), bad)

    def test_recovery_failure_still_delivers_a_deterministic_summary(self):
        h = rpc_harness(rpc_calls() + [EMPTY_FINAL, EMPTY_FINAL])
        out = h.run(USER_MSG)
        self.assertTrue(out["ok"])
        self._assert_rpc_findings(out["reply"])
        self.assertIn("525", out["reply"])

    def test_wallet_balances_is_not_called_for_rpc_status(self):
        h = rpc_harness(rpc_calls() + [final(RECOVERED)])
        h.run(USER_MSG)
        called = [r["data"].get("tool") for r in h.rows
                  if r["kind"] == "agent.tool_call"]
        self.assertTrue(called)
        self.assertNotIn("wallet_balances", called)
        self.assertEqual(set(called), {"runtime_command"})

    def test_verifier_sees_execution_evidence(self):
        h = rpc_harness(rpc_calls() + [final(RECOVERED)])
        h.run(USER_MSG)
        content = h.verify_user_content()
        self.assertIn("Gateway execution decision: required", content)
        self.assertIn("REQUIRES real tool execution", content)
        self.assertIn("runtime_command", content)

    def test_no_tool_evidence_is_not_complete_for_live_check(self):
        # A textual answer with NO tool run must not satisfy a live check.
        h = rpc_harness([final("Ethereum RPC is fine, trust me."),
                         final("still nothing")],
                        gateway=[web3_understand(),
                                 verdict("complete"), verdict("complete"),
                                 verdict("complete")])
        out = h.run(USER_MSG)
        self.assertNotEqual(out["data"]["verification"]["status"], "COMPLETE")

    def test_reply_never_carries_internal_machinery(self):
        h = rpc_harness(rpc_calls() + [EMPTY_FINAL, RECOVERED])
        out = h.run(USER_MSG)
        assert_clean(self, out["reply"])
        self.assertNotIn(FAKE_TOKEN, json.dumps(out))


class GatewayExecutionClassificationTests(unittest.TestCase):
    def test_understand_prompt_has_general_live_state_rule(self):
        p = UNDERSTAND_SYSTEM_PROMPT
        for word in ("CHECK", "VERIFY", "TEST", "QUERY"):
            self.assertIn(word, p)
        self.assertIn("live", p.lower())
        self.assertIn("web3", p)
        # a general rule, not one hardcoded sentence
        self.assertNotIn("Check RPC status for Ethereum", p)
        # explaining how RPC works stays text-only
        self.assertIn("explain", p.lower())

    def test_web3_is_an_allowed_task_type_in_the_prompt(self):
        self.assertIn("web3", UNDERSTAND_SYSTEM_PROMPT.split("Use exactly ONE of:")[1]
                      .split(".")[0])

    def test_non_execution_web3_explanation_stays_text_only(self):
        h = rpc_harness(["RPC is a remote procedure call interface."],
                        gateway=[web3_understand("Explain how RPC works.",
                                                 required=False, capability=""),
                                 verdict("complete")])
        out = h.run("Explain how RPC works.")
        self.assertFalse(out["data"]["execution"]["required"])
        self.assertEqual(out["data"]["task_type"], "web3")


# ── 4. HTTP end to end ─────────────────────────────────────────────────────
class ApiChatRegressionTests(unittest.TestCase):
    def _post(self, srv, message):
        req = urllib.request.Request(
            f"{srv.base}/api/chat",
            data=json.dumps({"message": message}).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    def _serve(self, brain_replies):
        h = rpc_harness(brain_replies)
        h.stack["agent"] = Agent(pipeline=h.pipeline)
        srv = LiveServer(stack=h.stack)
        self.addCleanup(srv.stop)
        return h, srv

    def test_api_chat_protocol_shaped_final_after_tools(self):
        h, srv = self._serve(rpc_calls() + [EMPTY_FINAL, RECOVERED])
        body = self._post(srv, USER_MSG)
        self.assertTrue(body["ok"])
        reply = body["data"]["reply"]
        self.assertNotEqual(reply, FALLBACK_TEXT)
        self.assertEqual(reply, RECOVERED)
        assert_clean(self, reply)

    def test_api_chat_recovery_failure_gives_execution_summary(self):
        h, srv = self._serve(rpc_calls() + [TRUNCATED_TOOL, NAMELESS_TOOL])
        body = self._post(srv, USER_MSG)
        self.assertTrue(body["ok"])
        reply = body["data"]["reply"]
        self.assertNotEqual(reply, FALLBACK_TEXT)
        assert_clean(self, reply)
        self.assertIn("eth.drpc.org", reply)
        self.assertIn("525", reply)


if __name__ == "__main__":
    unittest.main()
