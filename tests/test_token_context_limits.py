"""Regression tests: Astra must not impose small fixed API token limits and
artificial 6000-char / 20-turn / 12000-char context caps.

These prove the *behaviour* the architecture promises:

  - provider/Gateway output length is left to the model unless an explicit
    budget is configured (provider-aware; see astra.ai.token_limits);
  - conversation history is NOT trimmed to a small fixed size — it is only
    reduced when the selected model's real context window truly requires it
    (astra.ai.context_budget), never dropping the system prompt, the Gateway
    execution decision, the current user request, the tool protocol or the
    live execution state;
  - long tool results and long terminal output survive to the model;
  - every provider request stays valid (required fields still supplied).

The real `ConversationContextBuilder`/`ChatLog`/`ToolRegistry`/`AgentToolLoop`
are exercised where possible; only model HTTP is faked.
"""
import inspect
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.ai import chat_pipeline as cp
from astra.ai.agent_tool_loop import (AgentToolLoop, DEFAULT_MAX_TOOL_RESULT_CHARS,
                                      ToolCaller)
from astra.ai.adapters.bedrock import BedrockAdapter
from astra.ai.adapters.groq import GroqAdapter
from astra.ai.context_budget import (default_reserve_tokens,
                                     estimate_message_tokens, fit_messages)
from astra.ai.conversation_context import (ConversationContextBuilder,
                                           DEFAULT_MAX_CHARS, DEFAULT_MAX_TURNS)
from astra.ai.execution_history import (AgentExecutionHistory,
                                        DEFAULT_CONTEXT_CHARS)
from astra.ai.gateway import (GW_CLASSIFY_MAX_TOKENS,
                              GW_UNDERSTANDING_MAX_TOKENS, AstraAIGateway)
from astra.ai.gateway_contract import ProviderExecutionPort
from astra.ai.models import Model, ModelRegistry, metadata_for
from astra.ai.provider import ClaudeProvider
from astra.ai.router import AstraRouter, RoutingRequest, _RouterExecutionPort
from astra.ai.token_limits import resolve_output_tokens
from astra.core.permissions import Policy
from astra.core.config import Config
from astra.store import Store
from astra.chat_log import ChatLog
from astra.terminal import (TerminalManager, TerminalSession,
                            register_terminal_tools)
from astra.tools.builtins import register_builtins
from astra.tools.registry import ToolRegistry

from tests.helpers import ScriptedBrain


def _tool(command):
    return json.dumps({"action": "tool", "tool": "terminal_exec",
                       "args": {"command": command}, "thought": "go"})


def _final(answer):
    return json.dumps({"action": "final", "answer": answer})


class _Pool:
    """Minimal credential pool double: always healthy, secret is fake."""

    def pick(self, model=None):
        return "cred-1"

    def report_success(self, cred):
        pass

    def report_failure(self, *a, **k):
        pass

    def get_secret_for(self, cred):
        return "not-a-real-secret"

    def summary(self):
        return {}


# ── 1. no small fixed output-token caps ────────────────────────────────────
class NoFixedOutputCapsTests(unittest.TestCase):
    def test_routing_request_default_has_no_token_cap(self):
        self.assertIsNone(RoutingRequest().max_tokens)

    def test_chat_pipeline_default_has_no_token_cap(self):
        sig = inspect.signature(cp.ChatPipeline.__init__)
        self.assertIsNone(sig.parameters["max_tokens"].default)
        self.assertIsNone(cp.ChatPipeline(None, None).max_tokens)

    def test_explicit_pipeline_budget_is_honoured(self):
        self.assertEqual(cp.ChatPipeline(None, None, max_tokens=2048).max_tokens,
                         2048)

    def test_tool_loop_defaults_have_no_token_cap(self):
        for fn in (AgentToolLoop.run, ToolCaller.chat,
                   AstraRouter.run_tool_loop, AstraAIGateway.run_tool_loop):
            self.assertIsNone(
                inspect.signature(fn).parameters["max_tokens"].default, fn)
        for fn in (ProviderExecutionPort.execute,
                   _RouterExecutionPort.execute):
            self.assertIsNone(
                inspect.signature(fn).parameters["max_tokens"].default, fn)

    def test_gateway_chat_defaults_have_no_token_cap(self):
        self.assertIsNone(
            inspect.signature(AstraAIGateway.chat).parameters["max_tokens"].default)
        self.assertIsNone(
            inspect.signature(AstraAIGateway.stream).parameters["max_tokens"].default)

    def test_gateway_understanding_and_classify_constants_are_unset(self):
        self.assertIsNone(GW_UNDERSTANDING_MAX_TOKENS)
        self.assertIsNone(GW_CLASSIFY_MAX_TOKENS)

    def test_chat_pipeline_has_no_hidden_understand_verify_caps(self):
        # The old 700/600/12000 module constants must be gone.
        for name in ("UNDERSTAND_MAX_TOKENS", "VERIFY_MAX_TOKENS",
                     "MAX_OUTPUT_CHARS_IN_VERIFY"):
            self.assertFalse(hasattr(cp, name), name)


# ── 2. provider-aware output-token resolution ──────────────────────────────
class TokenLimitResolutionTests(unittest.TestCase):
    def test_optional_providers_omit_unset_limit(self):
        for provider in ("groq", "openrouter", "gemini", "cloudflare",
                         "bedrock", "cohere", "mistral", "zai", "openai"):
            self.assertIsNone(resolve_output_tokens(None, provider=provider),
                              provider)

    def test_explicit_budget_always_wins(self):
        self.assertEqual(resolve_output_tokens(2048, provider="groq"), 2048)
        self.assertEqual(resolve_output_tokens(4096, provider="anthropic"), 4096)

    def test_required_provider_derives_from_model_capability(self):
        model = Model("bedrock", "claude-3-5-sonnet",
                      max_output_tokens=8192)
        self.assertEqual(resolve_output_tokens(None, provider="anthropic",
                                               model_meta=model), 8192)

    def test_required_provider_derives_from_context_window_when_no_cap(self):
        model = Model("anthropic", "mystery-model", context_window=40000)
        self.assertEqual(resolve_output_tokens(None, provider="anthropic",
                                               model_meta=model), 10000)

    def test_model_metadata_exposes_documented_output_capability(self):
        self.assertEqual(metadata_for("claude-3-5-sonnet", "bedrock")
                         .get("max_output_tokens"), 8192)
        m = ModelRegistry(Config(path="/nonexistent-config.json")).add(
            "groq", "claude-3-5-sonnet")
        self.assertEqual(m.max_output_tokens, 8192)

    def test_compatible_adapter_omits_unset_max_tokens(self):
        adapter = GroqAdapter(config=None)
        adapter.pool = _Pool()
        adapter.models = ["test-model"]
        captured = {}

        def fake_post(url, body, cred):
            captured["body"] = body
            return {"choices": [{"message": {"content": "ok"}}]}

        adapter._post = fake_post
        adapter.chat([{"role": "user", "content": "hi"}])
        self.assertNotIn("max_tokens", captured["body"])

    def test_compatible_adapter_forwards_explicit_max_tokens(self):
        adapter = GroqAdapter(config=None)
        adapter.pool = _Pool()
        adapter.models = ["test-model"]
        captured = {}
        adapter._post = lambda url, body, cred: (
            captured.update(body=body) or {"choices": [{"message": {"content": "ok"}}]})
        adapter.chat([{"role": "user", "content": "hi"}], max_tokens=1234)
        self.assertEqual(captured["body"]["max_tokens"], 1234)

    def test_bedrock_converse_omits_unset_inference_config(self):
        adapter = BedrockAdapter(config=None)
        body = adapter._converse_body(
            [{"role": "user", "content": "hi"}], "some-model", None)
        self.assertNotIn("inferenceConfig", body)
        body2 = adapter._converse_body(
            [{"role": "user", "content": "hi"}], "some-model", 900)
        self.assertEqual(body2["inferenceConfig"]["maxTokens"], 900)

    def test_anthropic_provider_never_truncates_input_to_12000_chars(self):
        prov = ClaudeProvider(api_key="x", model="claude-3-5-sonnet")
        long_text = "A" * 40000
        body = prov._build_body(
            [{"role": "user", "content": long_text}], None, None)
        flattened = body["messages"][0]["content"]
        self.assertIn(long_text, flattened)
        self.assertGreater(len(flattened), 12000)
        # required by the Messages API: a real derived value, never None.
        self.assertIsNotNone(body["max_tokens"])
        self.assertGreater(body["max_tokens"], 0)


# ── 3. provider-aware context fitting ──────────────────────────────────────
class ContextFittingTests(unittest.TestCase):
    def test_fit_keeps_everything_when_it_fits(self):
        msgs = [{"role": "system", "content": "core rules"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
                {"role": "user", "content": "current request"}]
        self.assertEqual(fit_messages(msgs, context_window=100000), msgs)

    def test_fit_never_drops_system_or_current_request(self):
        big = "x" * 40000
        msgs = [{"role": "system", "content": "CORE SYSTEM PROMPT " + big},
                {"role": "user", "content": "old turn " + big},
                {"role": "assistant", "content": "old reply " + big},
                {"role": "user", "content": "CURRENT REQUEST"}]
        fitted = fit_messages(msgs, context_window=500, reserve_tokens=100)
        self.assertEqual(fitted[0]["role"], "system")
        self.assertIn("CORE SYSTEM PROMPT", fitted[0]["content"])
        self.assertEqual(fitted[-1]["content"], "CURRENT REQUEST")
        # the old middle turns are the ones dropped, never the protected ends.
        self.assertLess(len(fitted), len(msgs))

    def test_execution_decision_and_capability_block_survive(self):
        decision = ("Gateway execution decision: real tool execution REQUIRED "
                    "(capability: terminal)")
        capability = "Runtime capability catalog: terminal/shell access"
        msgs = [{"role": "system", "content": "CORE\n\n" + capability + "\n\n" + decision},
                {"role": "user", "content": "y" * 30000},
                {"role": "user", "content": "clone the repo"}]
        fitted = fit_messages(msgs, context_window=400, reserve_tokens=50)
        self.assertIn(decision, fitted[0]["content"])
        self.assertIn(capability, fitted[0]["content"])
        self.assertEqual(fitted[-1]["content"], "clone the repo")

    def test_no_context_window_means_no_trim(self):
        msgs = [{"role": "system", "content": "s"},
                {"role": "user", "content": "u" * 100000},
                {"role": "user", "content": "last"}]
        self.assertEqual(fit_messages(msgs, context_window=0), msgs)

    def test_estimate_is_labelled_as_chars_estimate(self):
        # No tokenizer is bundled, so it is a documented estimate, not exact.
        msgs = [{"role": "user", "content": "a" * 400}]
        self.assertEqual(estimate_message_tokens(msgs), 100)
        self.assertGreater(default_reserve_tokens(32000), 0)


# ── 4. conversation history is not artificially limited ────────────────────
class ConversationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.log = ChatLog(Store(":memory:"))
        self.cid = self.log.current_id

    def test_defaults_are_unlimited(self):
        self.assertIsNone(DEFAULT_MAX_CHARS)
        self.assertIsNone(DEFAULT_MAX_TURNS)
        self.assertIsNone(ConversationContextBuilder(self.log).max_chars)
        self.assertIsNone(ConversationContextBuilder(self.log).max_turns)

    def test_forty_turns_are_not_capped_at_twenty(self):
        for i in range(40):
            self.log.add_user(f"user turn {i}", conversation_id=self.cid)
            self.log.add_reply({"reply": f"assistant turn {i}", "ok": True},
                               conversation_id=self.cid)
        ctx = ConversationContextBuilder(self.log).build(self.cid)
        self.assertEqual(len(ctx.messages), 80)
        self.assertEqual(ctx.messages[0]["content"], "user turn 0")
        self.assertEqual(ctx.messages[-1]["content"], "assistant turn 39")

    def test_long_history_is_not_capped_at_6000_chars(self):
        for i in range(6):
            self.log.add_user(f"turn {i} " + "u" * 2000, conversation_id=self.cid)
            self.log.add_reply({"reply": "r" * 3000, "ok": True},
                               conversation_id=self.cid)
        ctx = ConversationContextBuilder(self.log).build(self.cid)
        total = sum(len(m["content"]) for m in ctx.messages)
        self.assertGreater(total, 6000)
        self.assertEqual(len(ctx.messages), 12)

    def test_explicit_budget_is_still_honoured_when_asked_for(self):
        for i in range(5):
            self.log.add_user(f"t{i} " + "x" * 500, conversation_id=self.cid)
        ctx = ConversationContextBuilder(self.log, max_chars=300).build(self.cid)
        self.assertTrue(ctx.messages)
        self.assertLessEqual(sum(len(m["content"]) for m in ctx.messages), 2000)


# ── 5. tool results / terminal output are not artificially truncated ───────
class ToolResultAndTerminalTests(unittest.TestCase):
    def test_default_tool_result_cap_is_disabled(self):
        self.assertIsNone(DEFAULT_MAX_TOOL_RESULT_CHARS)

    @staticmethod
    def _stack():
        policy = Policy(granted=["read", "low_risk_write", "browser_action",
                                 "system_action"])
        reg = ToolRegistry(policy=policy)
        register_builtins(reg)
        manager = TerminalManager()
        register_terminal_tools(reg, manager)
        return reg, manager

    def test_long_tool_result_reaches_the_model(self):
        registry, terminal = self._stack()
        brain = ScriptedBrain([
            _tool("printf 'Z%.0s' $(seq 1 9000)"),
            _final("done"),
        ])
        loop = AgentToolLoop(registry, terminal=terminal,
                             execution_history=AgentExecutionHistory())
        result = loop.run("show output", brain, system_prompt="sys",
                          session_id="c1", scope="1")
        self.assertTrue(result.ok)
        # The tool result fed back into the conversation must contain the
        # full long payload, not a 6000-char cut.
        tool_msgs = [m["content"] for m in brain.calls[1]
                     if isinstance(m.get("content"), str)
                     and "Z" * 9000 in m["content"]]
        self.assertTrue(tool_msgs, "full tool output was truncated before the model")
        terminal.close_all()

    def test_terminal_context_text_has_no_char_cap(self):
        import tempfile
        s = TerminalSession("s1", cwd=tempfile.mkdtemp())
        s.exec("printf 'Q%.0s' $(seq 1 3000)")
        text = s.context_text()
        self.assertIsInstance(text, str)
        self.assertNotIn("…", text)
        s.close()

    def test_execution_history_keeps_full_summary_by_default(self):
        hist = AgentExecutionHistory()
        self.assertIsNone(DEFAULT_CONTEXT_CHARS)
        hist.record("s", "terminal_exec", ok=True, status="completed",
                    result={"stdout": "R" * 5000})
        text = hist.context_text("s")
        self.assertIn("R" * 5000, text)


# ── 6. multi-step loop with large context can continue ─────────────────────
class MultiStepLargeContextTests(unittest.TestCase):
    def test_loop_continues_across_large_results(self):
        registry = ToolRegistry(Policy())
        register_builtins(registry)
        terminal = TerminalManager(events=None)
        register_terminal_tools(registry, terminal)
        brain = ScriptedBrain([
            _tool("seq 1 200"),
            _tool("echo second-step"),
            _final("finished both steps"),
        ])
        loop = AgentToolLoop(registry, terminal=terminal, max_steps=8,
                             execution_history=AgentExecutionHistory())
        result = loop.run("run two steps", brain, system_prompt="sys")
        self.assertTrue(result.ok)
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(result.text, "finished both steps")

    def test_router_fits_oversized_prompt_before_calling_adapter(self):
        # A router-issued call with an oversized prompt must still be sent,
        # fitted to the model's real window — never dropped entirely.
        seen = {}

        class Adapter:
            name = "groq"
            models = ["m"]

            def health_check(self):
                return True

            def chat(self, messages, model=None, max_tokens=None,
                     response_format=None):
                seen["messages"] = messages
                seen["max_tokens"] = max_tokens
                return "ok"

        registry = ModelRegistry(Config(path="/nonexistent-config.json"))
        registry.add("groq", "m", context_window=400)
        router = AstraRouter([Adapter()], max_retries=0, registry=registry,
                             gateway=None)
        model = registry.get("groq", "m")
        req = RoutingRequest(
            task_type="simple_chat",
            messages=[{"role": "system", "content": "SYS " + "s" * 2000},
                      {"role": "user", "content": "x" * 4000},
                      {"role": "user", "content": "current"}],
            max_tokens=None)
        rr = router._attempt(router.providers[0], model, req)
        self.assertIsNotNone(rr)
        self.assertTrue(rr.ok)
        self.assertEqual(seen["messages"][0]["role"], "system")
        self.assertEqual(seen["messages"][-1]["content"], "current")
        self.assertIsNone(seen["max_tokens"])
        self.assertLess(len(seen["messages"]), 3)  # oversized middle turn dropped

    def test_failover_preserves_full_context_and_execution_requirement(self):
        # Gateway execution decision rides in the system prompt, so a second
        # provider sees the identical requirement and the full context.
        seen = []

        class _Base:
            models = ["m"]

            def health_check(self):
                return True

        class ProviderA(_Base):
            name = "groq-a"

            def chat(self, messages, model=None, max_tokens=None,
                     response_format=None):
                seen.append(messages)
                raise RuntimeError("provider A down")

        class ProviderB(_Base):
            name = "groq-b"

            def chat(self, messages, model=None, max_tokens=None,
                     response_format=None):
                seen.append(messages)
                return "provider B answer"

        registry = ModelRegistry(Config(path="/nonexistent-config.json"))
        registry.add("groq-a", "m", context_window=200000)
        registry.add("groq-b", "m", context_window=200000)
        router = AstraRouter([ProviderA(), ProviderB()], max_retries=0,
                             registry=registry, gateway=None)
        decision = "Gateway execution decision: REQUIRED (capability: terminal)"
        req = RoutingRequest(
            task_type="simple_chat",
            messages=[{"role": "system", "content": "SYS\n\n" + decision},
                      {"role": "user", "content": "y" * 3000},
                      {"role": "user", "content": "clone it"}])
        rr = router.route_request(req)
        self.assertTrue(rr.ok)
        self.assertGreaterEqual(len(seen), 1)
        for messages in seen:
            self.assertIn(decision, messages[0]["content"])
            self.assertEqual(messages[-1]["content"], "clone it")


if __name__ == "__main__":
    unittest.main()
