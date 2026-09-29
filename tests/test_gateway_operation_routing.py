"""Operation-scoped fallback + global last-successful.

Two scopes:
  * GLOBAL     (GatewayRoutingState)        -- last_successful, health,
                                               latency, cooldowns; decides where
                                               a NEW operation starts.
  * OPERATION  (OperationRoutingContext)    -- failed / attempted provider+model
                                               targets of ONE user operation;
                                               makes fallback monotonic.

Target names used below follow the design scenario:
    Gemini-A = gemini/gem-a   Groq-A = groq/groq-a   Cloudflare-A = cloudflare/cf-a
"""
from __future__ import annotations

import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from astra.ai.gateway import AstraAIGateway, AstraGatewayGroq
from astra.ai.gateway_routing import (OperationRoutingContext,
                                      OperationRoutingRegistry)
from astra.core.exceptions import ProviderError
from tests.test_gateway_providers import (_capture, _cfg, _http_error,
                                          _openai_body, _resp)
from tests.test_logs_api_call_events import _Conn

MSG = [{"role": "user", "content": "hello there, how are you doing today my friend?"}]

GEMINI = ("gemini", "gem-a")
GROQ = ("groq", "groq-a")
CLOUDFLARE = ("cloudflare", "cf-a")


class _Rec(_Conn):
    """`_Conn` that records the order in which connections are called."""

    def __init__(self, name, models, calls, fail=False):
        super().__init__(name, models, fail=fail)
        self.calls = calls

    def chat(self, messages, model=None, max_tokens=500, response_format=None):
        self.calls.append(self.name)
        return super().chat(messages, model=model, max_tokens=max_tokens)

    def stream(self, messages, model=None, max_tokens=500):
        self.calls.append(self.name)
        yield from super().stream(messages, model=model, max_tokens=max_tokens)


def _world():
    calls: list[str] = []
    gem = _Rec("astra-gw-gemini", ["gem-a"], calls)
    groq = _Rec("astra-gw-groq", ["groq-a"], calls)
    cf = _Rec("astra-gw-cloudflare", ["cf-a"], calls)
    gw = AstraAIGateway(connections=[gem, groq, cf], events=None)
    return gw, gem, groq, cf, calls


def _recover(gw, *targets):
    """Model cooldown expiry / another operation succeeding on the target:
    it is eligible again globally, though it failed earlier in OUR operation."""
    for provider, model in targets:
        gw.routing_state.get_health(provider, model).cooldown_until = 0.0


def _ranked(gw, op_ctx=None):
    _cat, ranked = gw._select_order(MSG, None, op_ctx=op_ctx)
    return [(m.provider, m.model_id) for _c, m, _h in ranked]


def _last(gw):
    last = gw.routing_state.last_successful()
    return (last["provider"], last["model"]) if last else None


class TestOperationFallbackIsMonotonic(unittest.TestCase):
    # TEST 1 ---------------------------------------------------------------
    def test_same_operation_does_not_reuse_failed_target_via_chat(self):
        gw, gem, groq, cf, calls = _world()

        # OP-1 / Gateway #1: Gemini-A fails, Groq-A succeeds
        gem.fail = True
        gw.chat(MSG, operation_id="OP-1")
        self.assertEqual(gw.last_connection, "astra-gw-groq")
        self.assertEqual(_last(gw), GROQ)
        ctx = gw.operation_routing.get("OP-1")
        self.assertEqual(ctx.failed_targets, {GEMINI})

        # Gemini is healthy again (cooldown over / a concurrent op succeeded),
        # and would win if the original bug (restart from Gemini) were present.
        gem.fail = False
        _recover(gw, GEMINI)
        del calls[:]

        # OP-1 / Gateway #2: sticky Groq-A first, fails -> must NOT go to Gemini
        groq.fail = True
        gw.chat(MSG, operation_id="OP-1")
        self.assertEqual(calls, ["astra-gw-groq", "astra-gw-cloudflare"])
        self.assertNotIn("astra-gw-gemini", calls)
        self.assertEqual(gw.last_connection, "astra-gw-cloudflare")
        self.assertEqual(ctx.failed_targets, {GEMINI, GROQ})
        self.assertEqual(_last(gw), CLOUDFLARE)

    def test_same_operation_selection_excludes_failed_and_keeps_ranking(self):
        gw, *_ = _world()
        ctx = OperationRoutingContext("OP-1")
        ctx.mark_failed(*GEMINI)
        ctx.mark_failed(*GROQ)
        self.assertEqual(_ranked(gw, ctx), [CLOUDFLARE])

        # Only Gemini failed: the rest keep the normal (health, then list) order.
        ctx2 = OperationRoutingContext("OP-x")
        ctx2.mark_failed(*GEMINI)
        self.assertEqual(_ranked(gw, ctx2), [GROQ, CLOUDFLARE])

    def test_no_operation_context_keeps_legacy_ordering(self):
        gw, *_ = _world()
        self.assertEqual(_ranked(gw)[0], GEMINI)     # Gemini primary, unchanged

    def test_trace_is_used_as_operation_id_when_none_given(self):
        gw, gem, groq, cf, calls = _world()
        gem.fail = True
        gw.chat(MSG, trace="req-1")                  # pipeline style: trace=req
        gem.fail = False
        _recover(gw, GEMINI)
        groq.fail = True
        del calls[:]
        gw.chat(MSG, trace="req-1")
        self.assertNotIn("astra-gw-gemini", calls)
        self.assertEqual(gw.last_connection, "astra-gw-cloudflare")

    def test_calls_without_operation_or_trace_share_nothing(self):
        gw, gem, groq, cf, calls = _world()
        gem.fail = True
        gw.chat(MSG)
        gem.fail = False
        _recover(gw, GEMINI)
        groq.fail = True
        del calls[:]
        gw.chat(MSG)                                 # fresh op: Gemini allowed
        self.assertIn("astra-gw-gemini", calls)

    def test_stream_path_is_operation_scoped_too(self):
        gw, gem, groq, cf, calls = _world()
        gem.fail = True
        list(gw.stream(MSG, operation_id="OP-S"))
        self.assertEqual(gw.operation_routing.get("OP-S").failed_targets,
                         {GEMINI})
        gem.fail = False
        _recover(gw, GEMINI)
        groq.fail = True
        del calls[:]
        list(gw.stream(MSG, operation_id="OP-S"))
        self.assertNotIn("astra-gw-gemini", calls)
        self.assertEqual(gw.last_connection, "astra-gw-cloudflare")

    def test_exhausted_operation_reports_no_suitable_target(self):
        gw, gem, groq, cf, _calls = _world()
        gem.fail = groq.fail = cf.fail = True
        with self.assertRaises(ProviderError):
            gw.chat(MSG, operation_id="OP-dead")
        for c in (gem, groq, cf):
            c.fail = False
        _recover(gw, GEMINI, GROQ, CLOUDFLARE)
        with self.assertRaises(ProviderError) as cm:
            gw.chat(MSG, operation_id="OP-dead")     # everything failed in OP
        self.assertIn("no suitable provider+model", str(cm.exception))
        gw.chat(MSG, operation_id="OP-fresh")        # a new operation is fine


class TestGlobalStateAcrossOperations(unittest.TestCase):
    # TESTS 2 + 4 ----------------------------------------------------------
    def test_new_operation_starts_from_global_last_successful(self):
        gw, gem, groq, cf, calls = _world()
        gem.fail = groq.fail = True
        gw.chat(MSG, operation_id="OP-1")            # ends on Cloudflare-A
        self.assertEqual(_last(gw), CLOUDFLARE)

        gem.fail = groq.fail = False
        _recover(gw, GEMINI, GROQ)
        ctx2 = gw.operation_routing.get("OP-2")
        self.assertEqual(ctx2.failed_targets, frozenset())
        self.assertEqual(ctx2.attempted_targets, ())
        ranked = _ranked(gw, ctx2)
        self.assertIn(CLOUDFLARE, ranked)
        self.assertEqual(ranked[0], CLOUDFLARE)      # eligible AND first

    def test_global_last_success_survives_operation_boundary_in_chat(self):
        gw, gem, groq, cf, calls = _world()
        gem.fail = groq.fail = True
        gw.chat(MSG, operation_id="OP-1")
        gem.fail = groq.fail = False
        _recover(gw, GEMINI, GROQ)
        del calls[:]
        gw.chat(MSG, operation_id="OP-2")
        self.assertEqual(calls, ["astra-gw-cloudflare"])
        self.assertEqual(_last(gw), CLOUDFLARE)

    def test_scenario_b_op2_fallback_may_use_gemini(self):
        gw, gem, groq, cf, calls = _world()
        gem.fail = groq.fail = True
        gw.chat(MSG, operation_id="OP-1")            # OP-1 ends on Cloudflare-A
        gem.fail = groq.fail = False
        _recover(gw, GEMINI, GROQ)
        del calls[:]
        cf.fail = True                                # OP-2: Cloudflare-A fails
        gw.chat(MSG, operation_id="OP-2")
        self.assertEqual(calls, ["astra-gw-cloudflare", "astra-gw-gemini"])
        self.assertEqual(gw.operation_routing.get("OP-2").failed_targets,
                         {CLOUDFLARE})

    # TEST 3 ---------------------------------------------------------------
    def test_operation_failure_history_does_not_leak(self):
        gw, gem, groq, cf, _calls = _world()
        gem.fail = True
        gw.chat(MSG, operation_id="OP-1")
        self.assertEqual(gw.operation_routing.get("OP-1").failed_targets,
                         {GEMINI})
        gem.fail = False
        _recover(gw, GEMINI)

        ctx2 = gw.operation_routing.get("OP-2")
        self.assertIsNot(ctx2, gw.operation_routing.get("OP-1"))
        self.assertEqual(ctx2.failed_targets, frozenset())
        self.assertIn(GEMINI, _ranked(gw, ctx2))     # eligible again in OP-2
        self.assertNotIn(GEMINI, _ranked(gw, gw.operation_routing.get("OP-1")))

    # TEST 5 ---------------------------------------------------------------
    def test_global_failure_clear_does_not_reset_operation_history(self):
        gw, gem, groq, cf, _calls = _world()
        ctx = gw.operation_routing.get("OP-1")

        gw.routing_state.record_failure(*GEMINI)
        ctx.mark_failed(*GEMINI)                      # Gemini-A fails in OP-1
        gw.routing_state.record_success(*GROQ, 100.0)  # Groq-A succeeds
        ctx.mark_attempted(*GROQ)
        self.assertEqual(_last(gw), GROQ)

        gw.routing_state.record_failure(*GROQ)         # Groq-A later fails ...
        ctx.mark_failed(*GROQ)
        self.assertIsNone(gw.routing_state.last_successful())   # ... global cleared
        self.assertEqual(ctx.failed_targets, {GEMINI, GROQ})    # op history intact

        _recover(gw, GEMINI, GROQ)
        # The old behaviour (clear + rank_by_health) re-promotes Gemini ...
        self.assertEqual(_ranked(gw)[0], GEMINI)
        # ... the operation-scoped selection must not.
        self.assertEqual(_ranked(gw, ctx), [CLOUDFLARE])


class TestOperationIsolation(unittest.TestCase):
    # TEST 6 ---------------------------------------------------------------
    def test_concurrent_operations_are_isolated_through_chat(self):
        for _ in range(5):                            # stability
            calls: list[str] = []
            both_in_gemini = threading.Barrier(2, timeout=10)

            class _Gemini(_Rec):
                def chat(self, messages, model=None, max_tokens=500,
                         response_format=None):
                    self.calls.append(self.name)
                    both_in_gemini.wait()             # both ops already selected it
                    if "OP-1" in messages[-1]["content"]:
                        raise ProviderError("gemini: boom")
                    return "ok-gemini"

            gem = _Gemini("astra-gw-gemini", ["gem-a"], calls)
            groq = _Rec("astra-gw-groq", ["groq-a"], calls)
            gw = AstraAIGateway(connections=[gem, groq], events=None)

            def run(op):
                msgs = [{"role": "user",
                         "content": f"{op} hello there, how are you today?"}]
                return gw.chat(msgs, operation_id=op)

            with ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(run, "OP-1")
                f2 = pool.submit(run, "OP-2")
                self.assertEqual(f2.result(timeout=15), "ok-gemini")
                self.assertEqual(f1.result(timeout=15), "ok-astra-gw-groq-groq-a")

            ctx1 = gw.operation_routing.get("OP-1")
            ctx2 = gw.operation_routing.get("OP-2")
            self.assertEqual(ctx1.failed_targets, {GEMINI})
            self.assertEqual(ctx2.failed_targets, frozenset())
            # Global health of Gemini is back to healthy (OP-2's success);
            # OP-2 may still select it, OP-1 may not.
            _recover(gw, GEMINI)
            self.assertIn(GEMINI, _ranked(gw, ctx2))
            self.assertNotIn(GEMINI, _ranked(gw, ctx1))

    def test_many_concurrent_contexts_never_share_state(self):
        gw, *_ = _world()
        n = 24
        start = threading.Barrier(n, timeout=10)
        targets = [GEMINI, GROQ, CLOUDFLARE]

        def worker(i):
            ctx = gw.operation_routing.get(f"OP-{i}")
            mine = targets[i % 3]
            start.wait()
            for _ in range(50):
                ctx.mark_attempted(*mine)
                ctx.mark_failed(*mine)
                _ranked(gw, ctx)
            return ctx, mine

        with ThreadPoolExecutor(max_workers=n) as pool:
            results = list(pool.map(worker, range(n)))
        for ctx, mine in results:
            self.assertEqual(ctx.failed_targets, {mine})
            self.assertEqual(ctx.attempted_targets, (mine,))
        self.assertEqual(len({id(c) for c, _ in results}), n)


class TestRegistry(unittest.TestCase):
    def test_same_id_same_context_empty_id_always_fresh(self):
        reg = OperationRoutingRegistry()
        self.assertIs(reg.get("a"), reg.get("a"))
        self.assertIsNot(reg.get("a"), reg.get("b"))
        self.assertIsNot(reg.get(""), reg.get(""))
        self.assertEqual(len(reg), 2)                 # "" is never stored

    def test_bounded_by_count_and_age(self):
        reg = OperationRoutingRegistry()
        reg.MAX_CONTEXTS = 8
        for i in range(40):
            reg.get(f"op-{i}")
        self.assertLessEqual(len(reg), 8)
        reg = OperationRoutingRegistry()
        old = reg.get("old")
        old.last_used -= reg.TTL_S + 1
        reg.get("new")
        self.assertIsNot(reg.get("old"), old)         # expired -> clean state


class TestCredentialRetryUntouched(unittest.TestCase):
    # TEST 7 ---------------------------------------------------------------
    def _gateway(self, keys):
        conn = AstraGatewayGroq(config=_cfg(GW_GROQ_API_KEYS=keys,
                                            GW_GROQ_MODELS="m1",
                                            GW_RETRY_BACKOFF="0"))
        return AstraAIGateway(connections=[conn], events=None), conn

    def test_key_failure_does_not_mark_target_failed_when_sibling_key_works(self):
        gw, conn = self._gateway("key-1,key-2")
        side, seen = _capture([_http_error(401), _resp(_openai_body("pong"))])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            out = gw.chat(MSG, operation_id="OP-K")
        self.assertEqual(out, "pong")
        self.assertEqual(len(seen), 2)                # key-1 failed, key-2 served
        auth = [r["headers"].get("Authorization") for r in seen]
        self.assertEqual(len(set(auth)), 2, auth)
        ctx = gw.operation_routing.get("OP-K")
        self.assertEqual(ctx.failed_targets, frozenset())
        self.assertEqual(ctx.attempted_targets, (("groq", "m1"),))
        self.assertEqual(_last(gw), ("groq", "m1"))

    def test_target_is_marked_failed_only_after_every_key_failed(self):
        gw, conn = self._gateway("key-1,key-2")
        side, seen = _capture([_http_error(401), _http_error(401)])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            with self.assertRaises(ProviderError):
                gw.chat(MSG, operation_id="OP-K2")
        self.assertEqual(len(seen), 2)
        self.assertEqual(gw.operation_routing.get("OP-K2").failed_targets,
                         {("groq", "m1")})


if __name__ == "__main__":
    unittest.main()
