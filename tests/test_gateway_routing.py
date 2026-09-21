"""Astra AI Gateway — multi-provider / multi-model intelligent routing.

Covers the FINAL ARCHITECTURE spec: multiple models per Gateway provider,
capability-aware selection, fastest-suitable-model preference, persistent
last-successful-target across restarts, model-level (not provider-level)
failure/cooldown, provider-level failure fallback, capability-aware
fallback, and isolation from the existing Provider system.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest

from astra.core.exceptions import ProviderError
from astra.store import Store


# ── fakes ────────────────────────────────────────────────────────────────────
class _ShimPool:
    def __init__(self, ok=True):
        self.ok = ok
        self.count = 1

    def __bool__(self):
        return self.ok


class _FakeMultiModelConn:
    """Gateway connection stand-in that serves several models and can be
    told, per model id, to fail (always or a fixed number of times) or add
    artificial latency — so fastest-suitable and failure-recovery routing
    can be exercised deterministically."""

    base_url = ""

    def __init__(self, name, models, *, fail_models=None, fail_times=None,
                healthy=True, latency_by_model=None):
        self.name = name
        self.models = list(models)
        self.pool = _ShimPool(healthy)
        self.fail_models = set(fail_models or [])       # always fail
        self.fail_times = dict(fail_times or {})         # model -> N times
        self._fail_counts = {}
        self.latency_by_model = dict(latency_by_model or {})
        self.calls = []                                   # list of model ids

    def chat(self, messages, model=None, max_tokens=500, response_format=None):
        self.calls.append(model)
        if model in self.fail_models:
            raise ProviderError(f"{self.name}/{model}: simulated hard failure")
        budget = self.fail_times.get(model)
        if budget:
            done = self._fail_counts.get(model, 0)
            if done < budget:
                self._fail_counts[model] = done + 1
                raise ProviderError(f"{self.name}/{model}: simulated failure")
        delay = self.latency_by_model.get(model, 0)
        if delay:
            time.sleep(delay)
        return f"reply-from-{self.name}-{model}"

    def health_check(self):
        return bool(self.pool)


def _msg(text):
    return [{"role": "user", "content": text}]


# ── multi-model selection ────────────────────────────────────────────────────
class TestMultiModelSelection(unittest.TestCase):
    def test_multiple_models_per_provider_are_all_catalogued(self):
        from astra.ai.gateway import AstraAIGateway
        conn = _FakeMultiModelConn("astra-gw-gemini",
                                   ["gemini-2.0-flash", "gemini-2.0-pro"])
        gw = AstraAIGateway(connections=[conn])
        model_ids = {m.model_id for _, m in gw._catalog}
        self.assertEqual(model_ids, {"gemini-2.0-flash", "gemini-2.0-pro"})

    def test_fastest_suitable_model_is_preferred_for_a_simple_request(self):
        from astra.ai.gateway import AstraAIGateway
        # "pro" -> quality_class high; "flash" -> quality_class fast. Both
        # are otherwise identical/healthy, so the fast one should win a
        # plain simple/general request.
        conn = _FakeMultiModelConn("astra-gw-gemini",
                                   ["gemini-2.0-pro", "gemini-2.0-flash"])
        gw = AstraAIGateway(connections=[conn])
        gw.chat(_msg("hi"))
        self.assertEqual(gw.last_model, "gemini-2.0-flash")

    def test_capability_filtering_excludes_non_vision_model(self):
        from astra.ai.gateway import AstraAIGateway
        conn = _FakeMultiModelConn(
            "astra-gw-groq", ["llama-3.1-8b", "llama-3.1-8b-vision"])
        gw = AstraAIGateway(connections=[conn])
        gw.chat([{"role": "user", "content": "describe this screenshot"}])
        self.assertEqual(gw.last_model, "llama-3.1-8b-vision")

    def test_unavailable_model_in_last_successful_is_ignored_gracefully(self):
        """A stale last-successful pointer to a model no longer configured
        must never crash routing — it's simply not found among the
        eligible targets, and normal ranking proceeds."""
        from astra.ai.gateway import AstraAIGateway
        conn = _FakeMultiModelConn("astra-gw-groq", ["llama-3.1-8b"])
        gw = AstraAIGateway(connections=[conn])
        gw.routing_state.record_success("groq", "some-retired-model", 100.0)
        result = gw.chat(_msg("hi"))
        self.assertEqual(result, "reply-from-astra-gw-groq-llama-3.1-8b")

    def test_unhealthy_model_in_cooldown_is_excluded(self):
        from astra.ai.gateway import AstraAIGateway
        conn = _FakeMultiModelConn("astra-gw-groq", ["model-a", "model-b"])
        gw = AstraAIGateway(connections=[conn])
        gw.routing_state.record_failure("groq", "model-a", cooldown_s=999)
        gw.chat(_msg("hi"))
        self.assertEqual(gw.last_model, "model-b")


# ── failure handling: model-level, not provider-level (§6) ─────────────────
class TestFailureRecovery(unittest.TestCase):
    def test_one_model_failing_moves_to_sibling_model_same_provider(self):
        from astra.ai.gateway import AstraAIGateway
        conn = _FakeMultiModelConn("astra-gw-groq", ["model-a", "model-b"],
                                   fail_models={"model-a"})
        gw = AstraAIGateway(connections=[conn])
        result = gw.chat(_msg("hi"))
        self.assertEqual(result, "reply-from-astra-gw-groq-model-b")
        self.assertEqual(gw.last_model, "model-b")
        # Model-A is temporarily deprioritized (in cooldown), not the whole
        # connection/provider.
        health_a = gw.routing_state.get_health("groq", "model-a")
        self.assertFalse(health_a.healthy)
        self.assertEqual(health_a.failure_count, 1)

    def test_provider_level_failure_falls_back_to_another_provider(self):
        from astra.ai.gateway import AstraAIGateway
        dead = _FakeMultiModelConn("astra-gw-gemini", ["model-x"], healthy=False)
        alive = _FakeMultiModelConn("astra-gw-groq", ["model-y"])
        gw = AstraAIGateway(connections=[dead, alive])
        result = gw.chat(_msg("hi"))
        self.assertEqual(gw.last_connection, "astra-gw-groq")
        self.assertEqual(result, "reply-from-astra-gw-groq-model-y")
        self.assertEqual(dead.calls, [])   # never even attempted

    def test_capability_aware_fallback_never_prefers_incapable_fast_model(self):
        from astra.ai.gateway import AstraAIGateway
        # "qwen2.5-coder-32b" -> family qwen: has "coding"; mid/high quality.
        # "llama-3.1-8b-flash-lite" -> family llama: NO "coding"; fast.
        conn = _FakeMultiModelConn(
            "astra-gw-groq", ["llama-3.1-8b-flash-lite", "qwen2.5-coder-32b"])
        gw = AstraAIGateway(connections=[conn])
        result = gw.chat([{"role": "user", "content": "fix this python bug"}])
        self.assertEqual(gw.last_model, "qwen2.5-coder-32b")
        self.assertEqual(result, "reply-from-astra-gw-groq-qwen2.5-coder-32b")


# ── persistent last-successful target (§4/§5/§14) ───────────────────────────
class TestPersistence(unittest.TestCase):
    def test_last_successful_target_survives_restart(self):
        from astra.ai.gateway import AstraAIGateway
        d = tempfile.mkdtemp()
        db_path = os.path.join(d, "gw.db")

        store1 = Store(db_path)
        conn1 = _FakeMultiModelConn("astra-gw-groq", ["model-a", "model-b"])
        gw1 = AstraAIGateway(connections=[conn1], store=store1)
        gw1.chat(_msg("hi"))
        first_model = gw1.last_model
        store1.close()

        # "restart": brand-new process-equivalent objects, same DB file.
        store2 = Store(db_path)
        conn2 = _FakeMultiModelConn("astra-gw-groq", ["model-a", "model-b"])
        gw2 = AstraAIGateway(connections=[conn2], store=store2)
        last = gw2.routing_state.last_successful()
        self.assertIsNotNone(last)
        self.assertEqual(last["provider"], "groq")
        self.assertEqual(last["model"], first_model)

        # And it's actually preferred on the next request too.
        gw2.chat(_msg("hi"))
        self.assertEqual(gw2.last_model, first_model)
        store2.close()

    def test_last_successful_is_not_a_permanent_lock(self):
        """§5: prefer last-successful only while it's still healthy — once
        it starts failing, routing must move on to another suitable
        target and then remember THAT one instead."""
        from astra.ai.gateway import AstraAIGateway
        conn = _FakeMultiModelConn("astra-gw-groq", ["model-a", "model-b"])
        gw = AstraAIGateway(connections=[conn])
        gw.chat(_msg("hi"))
        first = gw.last_model

        # make the previously-successful model start failing
        other = "model-b" if first == "model-a" else "model-a"
        conn.fail_models.add(first)
        gw.chat(_msg("hi"))
        self.assertEqual(gw.last_model, other)
        last = gw.routing_state.last_successful()
        self.assertEqual(last["model"], other)

    def test_no_credentials_never_written_to_store(self):
        """The persisted routing tables only ever hold provider/model names,
        counters and timestamps — never secrets."""
        from astra.ai.gateway import AstraAIGateway
        d = tempfile.mkdtemp()
        store = Store(os.path.join(d, "gw.db"))
        conn = _FakeMultiModelConn("astra-gw-groq", ["model-a"])
        gw = AstraAIGateway(connections=[conn], store=store)
        gw.chat(_msg("hi"))
        cols = {r["name"] for r in store.fetch(
            "PRAGMA table_info(gateway_model_health)")}
        self.assertNotIn("api_key", cols)
        self.assertNotIn("secret", cols)
        self.assertNotIn("credentials", cols)
        store.close()


# ── isolation: this module never touches the existing Provider system ──────
class TestGatewayRoutingIsolation(unittest.TestCase):
    def test_gateway_routing_module_has_no_provider_system_imports(self):
        """Checks actual imports and code usage via the AST — not raw text —
        so the module's own docstrings/comments explaining the isolation
        (which necessarily *name* ProviderRegistry etc. in prose) can't
        trip a plain substring search."""
        import ast
        import astra.ai.gateway_routing as gr
        with open(gr.__file__, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)

        forbidden_modules = ("astra.ai.registry", "astra.ai.provider",
                             "astra.ai.adapters")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for forbidden in forbidden_modules:
                        self.assertFalse(alias.name.startswith(forbidden))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for forbidden in forbidden_modules:
                    self.assertFalse(module.startswith(forbidden))
            elif isinstance(node, ast.Name):
                self.assertNotEqual(node.id, "ProviderRegistry")
            elif isinstance(node, ast.Attribute):
                self.assertNotEqual(node.attr, "ProviderRegistry")

    def test_gateway_never_falls_back_into_provider_registry(self):
        from astra.ai.gateway import AstraAIGateway
        conn = _FakeMultiModelConn("astra-gw-groq", ["model-a"])
        gw = AstraAIGateway(connections=[conn])
        self.assertFalse(hasattr(gw, "providers"))
        self.assertFalse(hasattr(gw, "registry"))


# ── explicit user-requested model still respected (§17) ─────────────────────
class TestExplicitModelRequest(unittest.TestCase):
    def test_explicit_model_bypasses_intelligent_selection(self):
        from astra.ai.gateway import AstraAIGateway
        conn = _FakeMultiModelConn("astra-gw-groq", ["model-a", "model-b"])
        gw = AstraAIGateway(connections=[conn])
        result = gw.chat(_msg("hi"), model="model-b")
        self.assertEqual(gw.last_model, "model-b")
        self.assertEqual(result, "reply-from-astra-gw-groq-model-b")


# ── pure decision-logic unit tests (no HTTP, mirrors routing_policy tests) ───
class TestGatewayRoutingPolicyUnits(unittest.TestCase):
    def test_classify_gateway_request_categories(self):
        from astra.ai.gateway_routing import classify_gateway_request as c
        self.assertEqual(c("hi"), "simple")
        self.assertEqual(c("please fix this bug in my script"), "coding")
        self.assertEqual(c("why does this algorithm work? explain the logic"),
                        "reasoning")
        self.assertEqual(c("return this as json"), "structured_output")
        self.assertEqual(c("describe this screenshot"), "vision")
        self.assertEqual(c("", context_tokens=40000), "long_context")
        self.assertEqual(
            c("Tell me a fairly long, meandering story about your day"),
            "general")

    def test_system_prompt_text_never_leaks_into_classification(self):
        """A system/instruction message that happens to mention 'json' or
        'code' must never force a hard-capability filter based on the
        USER's actual (unrelated) request."""
        from astra.ai.gateway import AstraAIGateway
        conn = _FakeMultiModelConn("astra-gw-groq", ["model-a"])
        gw = AstraAIGateway(connections=[conn])
        messages = [
            {"role": "system", "content": "Respond using structured json code."},
            {"role": "user", "content": "hi"},
        ]
        result = gw.chat(messages)
        self.assertEqual(result, "reply-from-astra-gw-groq-model-a")

    def test_meets_gateway_requirements_context_window(self):
        from astra.ai.gateway_routing import meets_gateway_requirements
        from astra.ai.models import Model
        small = Model("groq", "small-model", context_window=8000)
        self.assertFalse(meets_gateway_requirements(
            small, category="general", context_tokens=50000))
        self.assertTrue(meets_gateway_requirements(
            small, category="general", context_tokens=1000))


if __name__ == "__main__":
    unittest.main()
