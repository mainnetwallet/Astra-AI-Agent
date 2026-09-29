"""Regression: verifier -> redo -> STILL-empty provider must never end in
"(no reply)" when real tool execution already produced the evidence.

Real failure (after the "recover real final answers" fix): "Check RPC status
for Ethereum." ran real RPC commands (one endpoint HTTP 525, one healthy), the
verifier correctly said `incomplete/redo`, but the provider kept answering
with the adapters' `(no reply)` placeholder. The pipeline shipped it, because

  * `(no reply)` is a non-empty string, so the loop / boundary treated it as a
    real answer (the earlier fix only covered empty / protocol-only replies),
  * corrections re-enter the tool loop and their steps were thrown away, so
    there was nothing to summarise from at the end,
  * `runtime_status` alone counted as "tool execution" evidence, and
  * the empty-provider + verifier loop had no turn-level bound (~262s).

Invariant under test:
    verifier/correction exhausted -> real execution evidence ->
    deterministic final answer -> response boundary -> user
"""
import json
import os
import sys
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.agent import Agent
from astra.ai.agent_tool_loop import LIFECYCLE_HINT, LoopStep, TOOL_PROTOCOL
from astra.ai.execution_answer import (NO_EVIDENCE_TEXT, evidence_snapshot,
                                       has_sufficient_evidence,
                                       summarize_execution)
from astra.ai.gateway_contract import ProviderExecutionDecision
from astra.ai.response_boundary import (FALLBACK_TEXT, is_unusable_answer,
                                        sanitize_final_response)
from astra.core.correction import MAX_CORRECTION_ATTEMPTS
from tests.helpers import LiveServer, LocalRuntimeStub
from tests.test_gateway_provider_tool_architecture import (final, tool_call,
                                                           verdict)
from tests.test_rpc_final_recovery import (EMPTY_FINAL, FAKE_TOKEN, USER_MSG,
                                           assert_clean, rpc_harness,
                                           web3_understand)

NO_REPLY = "(no reply)"
WEB3 = ProviderExecutionDecision(required=True, capability="web3")
LLAMA = "https://eth.llamarpc.com"
PUBLIC = "https://ethereum.publicnode.com"
INSTRUCTIONS = ("The final answer must use block number 0x18dfcef, network "
                "version 1 and syncing false from the successful RPC results.")


# ── helpers ──────────────────────────────────────────────────────────────────
def rpc_cmd(url, method):
    return tool_call(
        f"curl -s -X POST {url} -H 'Content-Type: application/json' "
        f"--data '{{\"jsonrpc\":\"2.0\",\"method\":\"{method}\","
        f"\"params\":[],\"id\":1}}'")


def lifecycle_call(tool):
    return json.dumps({"action": "tool", "tool": tool, "args": {},
                       "thought": "check the runtime"})


def trace_calls():
    """The tool calls of the real failing run, in order."""
    return [lifecycle_call("runtime_status"),
            rpc_cmd(LLAMA, "eth_blockNumber"), rpc_cmd(LLAMA, "net_version"),
            rpc_cmd(PUBLIC, "eth_blockNumber"), rpc_cmd(PUBLIC, "net_version"),
            rpc_cmd(PUBLIC, "eth_syncing")]


def incomplete():
    return verdict("incomplete", missing=["the real RPC results in the answer"],
                   action="redo", instructions=INSTRUCTIONS)


class TraceRuntimeStub(LocalRuntimeStub):
    """Canned network results for the exact failing trace (no network)."""

    BODIES = {
        ("publicnode", "eth_blockNumber"):
            '{"jsonrpc":"2.0","id":1,"result":"0x18dfcef"}',
        ("publicnode", "net_version"): '{"jsonrpc":"2.0","id":1,"result":"1"}',
        ("publicnode", "eth_syncing"):
            '{"jsonrpc":"2.0","id":1,"result":false}',
    }

    def exec_command(self, command, *, session_id="", timeout=None,
                     rows=24, cols=80):
        self.commands.append((str(session_id), str(command)))
        out = ""
        if "llamarpc" in command:
            out = "error code: 525"        # Cloudflare page, curl exits 0
        else:
            for (host, method), body in self.BODIES.items():
                if host in command and method in command:
                    out = body
        return {"ok": True, "status": "completed", "session_id": "s",
                "runtime": "test", "command": command, "cwd": "/workspace",
                "exit_code": 0, "stdout": out, "stderr": "",
                "duration_ms": 3, "truncated": False, "blob_id": ""}


def trace_harness(brain_replies, gateway=None, *, pad=12):
    """Pipeline harness on the exact-trace runtime. The brain is padded with
    `(no reply)` so an unexpected extra model call is still an empty one."""
    h = rpc_harness(list(brain_replies) + [NO_REPLY] * pad,
                    gateway=(gateway if gateway is not None else
                             [web3_understand()] + [incomplete()] * 6))
    h.runtime = TraceRuntimeStub(events=h.stack["events"])
    h.pipeline.runtime = h.runtime
    return h


def leaky_harness():
    """A run where one endpoint's error body carries a credential and its raw
    result carries a session id; the answer is still the deterministic
    summary (provider empty)."""
    body = json.dumps({"error": {"message": f"bad key {FAKE_TOKEN}"}})

    class Leaky(TraceRuntimeStub):
        def exec_command(self, command, **kw):
            res = super().exec_command(command, **kw)
            if "leak.example" in command:
                res["stdout"] = body
                res["session_id"] = "sess-secret-1"
            return res

    h = trace_harness(
        [rpc_cmd("https://leak.example", "eth_blockNumber"),
         rpc_cmd(PUBLIC, "eth_blockNumber"), NO_REPLY, NO_REPLY],
        gateway=[web3_understand(), verdict("complete")])
    h.runtime = Leaky(events=h.stack["events"])
    h.pipeline.runtime = h.runtime
    return h


def step(tool, command="", *, ok=True, code=0, out="", err="", **result):
    args = {"command": command} if command else {}
    res = {"ok": ok, "status": "completed" if ok else "failed",
           "exit_code": code, "stdout": out, "stderr": err}
    res.update(result)
    return LoopStep(0, "tool", tool, args, ok=ok, status=res["status"],
                    result=res)


def curl(url, method, out, *, ok=True, code=0, err=""):
    return step("runtime_command",
                f"curl -s {url} --data '{{\"method\":\"{method}\"}}'",
                ok=ok, code=code, out=out, err=err)


def trace_steps():
    return [step("runtime_status", out='{"state":"running"}'),
            curl(LLAMA, "eth_blockNumber", "error code: 525"),
            curl(LLAMA, "net_version", "error code: 525"),
            curl(PUBLIC, "eth_blockNumber", '{"result":"0x18dfcef"}'),
            curl(PUBLIC, "net_version", '{"result":"1"}'),
            curl(PUBLIC, "eth_syncing", '{"result":false}')]


def assert_no_verifier_leak(tc, reply):
    low = reply.lower()
    for leak in ('"verdict"', '"instructions"', '"missing"', "redo",
                 "incomplete", "final answer must use"):
        tc.assertNotIn(leak, low)


# ── 1. evidence gate ─────────────────────────────────────────────────────────
class EvidenceGateTests(unittest.TestCase):
    def test_runtime_status_only_is_not_sufficient(self):
        s = [step("runtime_status", out='{"state":"running"}')]
        self.assertFalse(has_sufficient_evidence(s, WEB3))
        self.assertFalse(has_sufficient_evidence(s))
        self.assertEqual(evidence_snapshot(s, WEB3), {})
        self.assertEqual(summarize_execution(s, WEB3), "")

    def test_runtime_start_only_is_not_sufficient(self):
        s = [step("runtime_start", out='{"started":true}')]
        self.assertFalse(has_sufficient_evidence(s, WEB3))
        self.assertEqual(evidence_snapshot(s, WEB3), {})

    def test_status_plus_start_together_are_still_not_sufficient(self):
        s = [step("runtime_status"), step("runtime_start"),
             step("runtime_status")]
        self.assertFalse(has_sufficient_evidence(s, WEB3))

    def test_lifecycle_is_evidence_only_when_the_runtime_is_the_subject(self):
        s = [step("runtime_status", out='{"state":"running"}')]
        for cap in ("runtime", "system"):
            ex = ProviderExecutionDecision(required=True, capability=cap)
            self.assertTrue(has_sufficient_evidence(s, ex), cap)

    def test_rpc_success_is_sufficient(self):
        s = [curl(PUBLIC, "eth_blockNumber", '{"result":"0x1"}')]
        self.assertTrue(has_sufficient_evidence(s, WEB3))
        self.assertIn("tool_execution", evidence_snapshot(s, WEB3))

    def test_rpc_failure_alone_is_a_real_answer(self):
        s = [curl(LLAMA, "eth_blockNumber", "error code: 525")]
        self.assertTrue(has_sufficient_evidence(s, WEB3))
        text = summarize_execution(s, WEB3)
        self.assertIn("eth.llamarpc.com returned HTTP 525", text)
        self.assertIn("no endpoint responded successfully", text)

    def test_status_plus_rpc_is_sufficient_and_ignores_status_in_answer(self):
        text = summarize_execution(trace_steps(), WEB3)
        self.assertNotIn("running", text)
        self.assertNotIn("runtime_status", text)

    def test_a_refused_tool_call_is_not_answer_evidence(self):
        s = [LoopStep(0, "tool", "terminal_exec", {"command": "x"}, ok=False,
                      status="error", error="unknown tool: terminal_exec",
                      result={"ok": False, "error": "unknown tool"})]
        self.assertFalse(has_sufficient_evidence(s, WEB3))
        self.assertFalse(has_sufficient_evidence(s))

    def test_a_non_network_command_is_not_rpc_evidence_for_web3(self):
        s = [step("runtime_command", "ls -la", out="total 0")]
        self.assertFalse(has_sufficient_evidence(s, WEB3))
        # ...but it is ordinary evidence for a non-web3 execution task.
        self.assertTrue(has_sufficient_evidence(
            s, ProviderExecutionDecision(required=True, capability="terminal")))

    def test_any_rpc_client_counts_not_just_curl(self):
        s = [step("runtime_command",
                  "cast block-number --rpc-url https://x.example/rpc",
                  out="21000000")]
        self.assertTrue(has_sufficient_evidence(s, WEB3))


# ── 2. deterministic summary ─────────────────────────────────────────────────
class DeterministicSummaryTests(unittest.TestCase):
    def test_exact_failing_trace(self):
        text = summarize_execution(trace_steps(), WEB3)
        self.assertEqual(text, (
            "Ethereum RPC check completed.\n\n"
            "- ethereum.publicnode.com: responsive\n"
            "- Network: Ethereum mainnet (chain ID 1)\n"
            "- Latest block: 0x18dfcef\n"
            "- Syncing: false\n\n"
            "eth.llamarpc.com returned HTTP 525 during the check."))

    def test_values_are_derived_from_results_not_hardcoded(self):
        s = [curl("https://rpc.other.example", "eth_chainId", '{"result":"0x2105"}'),
             curl("https://rpc.other.example", "eth_blockNumber", '{"result":"0xabc123"}'),
             curl("https://rpc.other.example", "eth_syncing", '{"result":true}')]
        text = summarize_execution(s, WEB3)
        self.assertIn("Base RPC check completed.", text)
        self.assertIn("rpc.other.example: responsive", text)
        self.assertIn("Base mainnet (chain ID 8453)", text)
        self.assertIn("Latest block: 0xabc123", text)
        self.assertIn("Syncing: true", text)
        for stale in ("0x18dfcef", "publicnode", "525", "Ethereum"):
            self.assertNotIn(stale, text)

    def test_unknown_chain_id_is_reported_as_is(self):
        s = [curl("https://n.example", "net_version", '{"result":"424242"}'),
             curl("https://n.example", "eth_blockNumber", '{"result":"0x9"}')]
        text = summarize_execution(s, WEB3)
        self.assertIn("chain ID 424242", text)
        self.assertNotIn("mainnet", text)

    def test_two_healthy_endpoints_and_a_failed_one_are_all_reported(self):
        s = [curl(PUBLIC, "eth_blockNumber", '{"result":"0x18dfcef"}'),
             curl("https://eth.drpc.org", "eth_blockNumber", '{"result":"0x18dfcf0"}'),
             curl(LLAMA, "eth_blockNumber", "", ok=False, code=22,
                  err="curl: (22) The requested URL returned error: 525")]
        text = summarize_execution(s, WEB3)
        self.assertIn("ethereum.publicnode.com: responsive", text)
        self.assertIn("eth.drpc.org: responsive", text)
        self.assertIn("0x18dfcef", text)
        self.assertIn("0x18dfcf0", text)
        self.assertIn("eth.llamarpc.com returned HTTP 525", text)

    def test_rpc_error_body_timeout_and_unreachable(self):
        s = [curl("https://a.example", "eth_blockNumber",
                  '{"error":{"code":-32000,"message":"rate limited"}}'),
             curl("https://b.example", "eth_blockNumber", "", ok=False, code=28,
                  err="curl: (28) Operation timed out"),
             curl("https://c.example", "eth_blockNumber", "", ok=False, code=7,
                  err="curl: (7) Failed to connect")]
        text = summarize_execution(s, WEB3)
        self.assertIn("a.example returned an RPC error (rate limited)", text)
        self.assertIn("b.example timed out", text)
        self.assertIn("c.example could not be reached", text)

    def test_dedicated_chain_tool_results_are_summarised(self):
        s = [LoopStep(0, "tool", "rpc_status", {"network": "ethereum"}, ok=True,
                      status="ok", result={"ok": True, "chain": "eth", "rpcs": [
                          {"url": "https://ethereum-rpc.publicnode.com",
                           "ok": True, "block": 21000000},
                          {"url": "https://rpc.ankr.com/eth", "ok": False,
                           "block": None}]})]
        text = summarize_execution(s, WEB3)
        self.assertIn("Ethereum RPC check completed.", text)
        self.assertIn("ethereum-rpc.publicnode.com: responsive", text)
        self.assertIn("Latest block: 21000000", text)
        self.assertIn("rpc.ankr.com did not respond", text)

    def test_non_rpc_execution_uses_the_generic_evidence_summary(self):
        s = [step("runtime_command", "ls -la", out="total 0")]
        text = summarize_execution(
            s, ProviderExecutionDecision(required=True, capability="terminal"))
        self.assertIn("ls -la", text)
        self.assertIn("total 0", text)

    def test_summary_never_exposes_plumbing_or_secrets(self):
        s = trace_steps() + [
            curl("https://leak.example", "eth_blockNumber",
                 json.dumps({"error": {"message": f"bad key {FAKE_TOKEN}"}}))]
        for st in s:
            st.result.update({"session_id": "sess-1234", "trace_id": "tr-9"})
        text = summarize_execution(s, WEB3)
        assert_clean(self, text)
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn("sess-1234", text)
        self.assertNotIn("runtime_command", text)


# ── 3. the exact pipeline path ───────────────────────────────────────────────
class VerifierExhaustionPipelineTests(unittest.TestCase):
    def _summary_facts(self, reply):
        self.assertIn("0x18dfcef", reply)
        self.assertIn("Ethereum mainnet (chain ID 1)", reply)
        self.assertIn("Syncing: false", reply)
        self.assertIn("ethereum.publicnode.com", reply)
        self.assertIn("eth.llamarpc.com returned HTTP 525", reply)

    def test_exact_failure_path_returns_a_deterministic_answer(self):
        # provider empty -> verifier incomplete/redo -> correction runs the
        # real tools but the provider stays empty -> verifier incomplete again
        # -> correction empty again -> exhausted.
        h = trace_harness([NO_REPLY] + trace_calls() + [NO_REPLY, NO_REPLY])
        out = h.run(USER_MSG)
        reply = out["reply"]
        self.assertTrue(reply.strip())
        self.assertNotEqual(reply, NO_REPLY)
        self.assertNotEqual(reply, FALLBACK_TEXT)
        self.assertNotEqual(reply, NO_EVIDENCE_TEXT)
        self._summary_facts(reply)
        assert_clean(self, reply)
        assert_no_verifier_leak(self, reply)
        # the run really was verifier-exhausted, not a lucky first pass
        self.assertNotEqual(out["data"]["verification"]["status"], "COMPLETE")
        self.assertGreaterEqual(out["data"]["verification"]["attempts"], 1)

    def test_correction_steps_are_kept_as_evidence(self):
        h = trace_harness([NO_REPLY] + trace_calls() + [NO_REPLY, NO_REPLY])
        h.run(USER_MSG)
        cmds = [c for _, c in h.runtime.commands]
        self.assertTrue(any("publicnode" in c for c in cmds))
        self.assertTrue(any("llamarpc" in c for c in cmds))

    def test_provider_empty_after_max_corrections_is_deterministic(self):
        # Every correction is empty; the tools ran in the first loop only.
        h = trace_harness(trace_calls() + [NO_REPLY] * 3,
                          gateway=[web3_understand()] + [incomplete()] * 8)
        out = h.run(USER_MSG)
        self._summary_facts(out["reply"])
        self.assertLessEqual(out["data"]["verification"]["attempts"],
                             MAX_CORRECTION_ATTEMPTS)

    def test_always_empty_provider_terminates_within_the_budget(self):
        calls = trace_calls()
        h = trace_harness(calls + [NO_REPLY] * 40, pad=0,
                          gateway=[web3_understand()] + [incomplete()] * 10)
        out = h.run(USER_MSG)
        self._summary_facts(out["reply"])
        executed = [c for _, c in h.runtime.commands]
        self.assertLessEqual(len(executed), h.pipeline.max_tool_steps * 2)
        # bounded model calls: first loop + corrections (+ one recovery each)
        bound = (h.pipeline.max_tool_steps * 2
                 + (1 + MAX_CORRECTION_ATTEMPTS) * 2)
        self.assertLessEqual(len(h.brain.calls), bound)

    def test_time_budget_stops_corrections_and_still_answers(self):
        h = trace_harness(trace_calls() + [NO_REPLY, NO_REPLY])
        h.pipeline.turn_deadline_s = 1e-9       # already spent at correction
        out = h.run(USER_MSG)
        self._summary_facts(out["reply"])
        # no correction loop was entered: only the padding is left untouched
        self.assertEqual(len(h.brain.replies), 12)

    def test_step_budget_stops_corrections_and_still_answers(self):
        h = trace_harness(trace_calls() + [NO_REPLY, NO_REPLY])
        h.pipeline.max_tool_steps = 3       # budget 6; first loop uses them
        out = h.run(USER_MSG)
        self.assertTrue(out["reply"].strip())
        self.assertNotEqual(out["reply"], NO_REPLY)
        self.assertLessEqual(len(h.runtime.commands), 6)

    def test_recovery_that_asks_for_a_tool_is_rejected_not_executed(self):
        evil = rpc_cmd("https://evil.example/steal", "eth_blockNumber")
        h = trace_harness(
            [rpc_cmd(PUBLIC, "eth_blockNumber"), rpc_cmd(PUBLIC, "net_version"),
             rpc_cmd(PUBLIC, "eth_syncing"), rpc_cmd(LLAMA, "eth_blockNumber"),
             NO_REPLY,      # final answer: unusable
             evil],         # bounded recovery: asks for another tool
            gateway=[web3_understand(), verdict("complete")])
        out = h.run(USER_MSG)
        self.assertFalse(any("evil.example" in c for _, c in h.runtime.commands))
        self.assertIn("0x18dfcef", out["reply"])
        self.assertNotIn("evil.example", out["reply"])

    def test_protocol_only_final_after_tools_gives_deterministic_summary(self):
        h = trace_harness(trace_calls()[1:] + [EMPTY_FINAL, EMPTY_FINAL],
                          gateway=[web3_understand(), verdict("complete")])
        out = h.run(USER_MSG)
        self._summary_facts(out["reply"])
        assert_clean(self, out["reply"])

    def test_a_valid_provider_answer_is_left_alone(self):
        good = "Ethereum RPC is healthy: block 0x18dfcef on chain 1."
        h = trace_harness(trace_calls()[1:] + [final(good)],
                          gateway=[web3_understand(), verdict("complete")])
        out = h.run(USER_MSG)
        self.assertEqual(out["reply"], good)
        self.assertEqual(out["data"]["final_answer_source"], "model")

    def test_runtime_status_only_never_completes_an_rpc_check(self):
        h = trace_harness([lifecycle_call("runtime_status"),
                           lifecycle_call("runtime_start"), NO_REPLY],
                          gateway=[web3_understand(), verdict("complete"),
                                   verdict("complete"), verdict("complete")])
        out = h.run(USER_MSG)
        self.assertNotEqual(out["data"]["verification"]["status"], "COMPLETE")
        self.assertEqual(out["reply"], NO_EVIDENCE_TEXT)
        self.assertNotIn("responsive", out["reply"])

    def test_generic_text_only_when_there_is_no_answer_and_no_evidence(self):
        h = trace_harness([NO_REPLY] * 6,
                          gateway=[web3_understand()] + [incomplete()] * 6)
        out = h.run(USER_MSG)
        self.assertEqual(out["reply"], NO_EVIDENCE_TEXT)
        self.assertNotEqual(out["reply"], NO_REPLY)
        self.assertEqual(out["data"]["final_answer_source"], "no_evidence")

    def test_deterministic_summary_goes_through_the_response_boundary(self):
        h = leaky_harness()
        out = h.run(USER_MSG)
        # The user-facing text is what the boundary protects. (The internal
        # trace in `data` legitimately holds raw results; the web layer
        # scrubs it -- asserted on the real HTTP wire in ApiChat* below.)
        self.assertNotIn(FAKE_TOKEN, out["reply"])
        self.assertNotIn("sess-secret-1", out["reply"])
        self.assertIn("redacted", out["reply"])
        self.assertIn("0x18dfcef", out["reply"])
        assert_clean(self, out["reply"])

    def test_boundary_never_ships_the_no_reply_placeholder_when_it_can_help(self):
        rec = "Ethereum RPC check completed."
        self.assertEqual(sanitize_final_response(NO_REPLY, fallback=rec), rec)
        self.assertEqual(sanitize_final_response("  (No reply) ", fallback=rec),
                         rec)
        self.assertTrue(is_unusable_answer(NO_REPLY))
        self.assertTrue(is_unusable_answer(EMPTY_FINAL))
        self.assertFalse(is_unusable_answer("The RPC is healthy."))

    def test_non_execution_chat_is_untouched(self):
        h = trace_harness(["RPC is a remote procedure call interface."],
                          gateway=[web3_understand("Explain how RPC works.",
                                                   required=False,
                                                   capability=""),
                                   verdict("complete")])
        out = h.run("Explain how RPC works.")
        self.assertEqual(out["reply"], "RPC is a remote procedure call interface.")


# ── 4. tool selection ────────────────────────────────────────────────────────
class ToolSelectionTests(unittest.TestCase):
    def test_lifecycle_result_tells_the_model_to_do_the_real_work(self):
        h = trace_harness([lifecycle_call("runtime_status"),
                           rpc_cmd(PUBLIC, "eth_blockNumber"),
                           final("Block 0x18dfcef.")],
                          gateway=[web3_understand(), verdict("complete")])
        h.run(USER_MSG)
        second = h.brain.calls[1]
        self.assertIn(LIFECYCLE_HINT.strip()[:40], second[-1]["content"])

    def test_the_hint_is_not_repeated_once_real_work_started(self):
        h = trace_harness([rpc_cmd(PUBLIC, "eth_blockNumber"),
                           lifecycle_call("runtime_status"),
                           final("Block 0x18dfcef.")],
                          gateway=[web3_understand(), verdict("complete")])
        h.run(USER_MSG)
        self.assertNotIn(LIFECYCLE_HINT.strip()[:40],
                         h.brain.calls[2][-1]["content"])

    def test_protocol_prefers_the_real_operation_over_lifecycle_tools(self):
        self.assertIn("NOT evidence", TOOL_PROTOCOL)
        self.assertIn("RPC", TOOL_PROTOCOL)
        # a general rule, not one hardcoded endpoint
        self.assertNotIn("llamarpc", TOOL_PROTOCOL)
        self.assertNotIn("publicnode", TOOL_PROTOCOL)


# ── 5. HTTP end to end ───────────────────────────────────────────────────────
class ApiChatVerifierExhaustionTests(unittest.TestCase):
    def _post(self, srv, message):
        req = urllib.request.Request(
            f"{srv.base}/api/chat",
            data=json.dumps({"message": message}).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())

    def test_api_chat_check_rpc_status_for_ethereum(self):
        h = trace_harness([NO_REPLY] + trace_calls() + [NO_REPLY, NO_REPLY])
        h.stack["agent"] = Agent(pipeline=h.pipeline)
        srv = LiveServer(stack=h.stack)
        self.addCleanup(srv.stop)

        status, body = self._post(srv, USER_MSG)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        reply = body["data"]["reply"]
        self.assertTrue(reply.strip())
        self.assertNotEqual(reply, NO_REPLY)
        self.assertNotEqual(reply, FALLBACK_TEXT)
        self.assertNotIn("internal formatting issue", reply.lower())
        # evidence derived from the successful RPC execution
        self.assertIn("ethereum.publicnode.com", reply)
        self.assertIn("0x18dfcef", reply)
        self.assertIn("Ethereum mainnet (chain ID 1)", reply)
        self.assertIn("Syncing: false", reply)
        self.assertIn("eth.llamarpc.com returned HTTP 525", reply)
        # no leaks anywhere in the HTTP payload the client receives
        assert_clean(self, reply)
        assert_no_verifier_leak(self, reply)
        wire = json.dumps(body)
        for leak in ('"action": "tool"', '"action":"tool"', "session_id\":\"",
                     "FINAL_RECOVERY", "recovery_summary"):
            self.assertNotIn(leak, reply)
        self.assertNotIn(FAKE_TOKEN, wire)


    def test_api_chat_wire_never_carries_secrets_or_session_ids(self):
        h = leaky_harness()
        h.stack["agent"] = Agent(pipeline=h.pipeline)
        srv = LiveServer(stack=h.stack)
        self.addCleanup(srv.stop)
        req = urllib.request.Request(
            f"{srv.base}/api/chat",
            data=json.dumps({"message": USER_MSG}).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
        self.assertNotIn(FAKE_TOKEN, raw)
        self.assertNotIn("sess-secret-1", raw)
        self.assertIn("0x18dfcef", json.loads(raw)["data"]["reply"])


if __name__ == "__main__":
    unittest.main()
