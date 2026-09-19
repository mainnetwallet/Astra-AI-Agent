"""Chat pipeline: User -> Gateway (understand+assign) -> Provider ->
Gateway (verify, fix/redo loop) -> User.

Uses scripted fakes for the Gateway's AI calls and the router, but the REAL
bounded verify/correct supervisor (GatewayTaskCompletionSupervisor), so the
loop semantics tested here are the ones that run in production.
"""
import json
import unittest


from astra.agent import Agent
from astra.ai.chat_pipeline import ChatPipeline
from astra.ai.gateway_task_completion import GatewayTaskCompletionSupervisor
from astra.ai.router import RoutingResult
from astra.core.correction import MAX_CORRECTION_ATTEMPTS

TARGETS = [
    {"provider": "groq", "model": "llama-fast", "capabilities": ["chat"],
     "quality": "fast", "context_window": 8000},
    {"provider": "gemini", "model": "gemini-pro", "capabilities": ["chat", "coding"],
     "quality": "high", "context_window": 100000},
]


def understand(final_request="", was_incomplete=False, provider="gemini",
               model="gemini-pro", criteria=("answers the question",)):
    return json.dumps({"final_request": final_request,
                       "was_incomplete": was_incomplete, "provider": provider,
                       "model": model, "criteria": list(criteria),
                       "reason": "best fit"})


def verdict(v="complete", missing=(), action="fix", instructions=""):
    return json.dumps({"verdict": v, "missing": list(missing),
                       "action": action, "instructions": instructions})


class FakeGateway:
    """Scripted Gateway: `chat()` pops replies (str, or Exception to raise)."""

    def __init__(self, replies, usable=True):
        self.replies = list(replies)
        self.calls = []            # every gateway.chat() messages list
        self.categories = []       # the explicit category of each call
        self.usable = usable

    def is_usable(self):
        return self.usable

    def chat(self, messages, model=None, max_tokens=500, category=None):
        self.calls.append(messages)
        self.categories.append(category)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def supervise_task(self, port, target, messages, result, contract, *,
                       evidence=None, semantic_verifier=None, max_tokens=500):
        return GatewayTaskCompletionSupervisor().supervise(
            port, target, messages, result, contract, evidence=evidence,
            semantic_verifier=semantic_verifier, max_tokens=max_tokens)


class FakeRouter:
    """Scripted router: each route_request() pops a text (or Exception/None
    for failure) and answers as whatever provider/model was preferred."""

    def __init__(self, outputs, targets=None):
        self.outputs = list(outputs)
        self.requests = []
        self._targets = TARGETS if targets is None else targets

    def available_targets(self):
        return list(self._targets)

    def route_request(self, req):
        self.requests.append(req)
        out = self.outputs.pop(0)
        if out is None:
            return RoutingResult(ok=False, error="all providers failed")
        return RoutingResult(ok=True, text=out,
                             provider=req.preferred_provider or "groq",
                             model=req.preferred_model or "llama-fast")


def make(gateway_replies, outputs, **kw):
    gw = FakeGateway(gateway_replies, usable=kw.pop("usable", True))
    rt = FakeRouter(outputs)
    return ChatPipeline(gw, rt, max_tokens=800), gw, rt


def user_text(req):
    return req.messages[-1]["content"]


class TestHappyPath(unittest.TestCase):
    def test_complete_message_passes_unchanged_and_verified_output_returned(self):
        pipe, gw, rt = make(
            [understand(was_incomplete=False), verdict("complete")],
            ["Paris is the capital of France."])
        out = pipe.run("What is the capital of France?")
        self.assertTrue(out["ok"])
        self.assertEqual(out["reply"], "Paris is the capital of France.")
        # complete message: provider got it exactly, not a rewrite
        self.assertIn("What is the capital of France?", user_text(rt.requests[0]))
        self.assertEqual(out["data"]["verification"]["status"], "COMPLETE")
        self.assertFalse(out["data"]["was_incomplete"])
        self.assertEqual(len(gw.calls), 2)   # exactly: understand + verify

    def test_incomplete_message_is_completed_before_reaching_provider(self):
        pipe, gw, rt = make(
            [understand("Explain what Bitcoin halving is.", was_incomplete=True),
             verdict("complete")],
            ["Halving cuts the block reward in half."])
        out = pipe.run("bitcoin halving?")
        self.assertIn("Explain what Bitcoin halving is.", user_text(rt.requests[0]))
        self.assertTrue(out["data"]["was_incomplete"])
        self.assertEqual(out["data"]["understood"], "Explain what Bitcoin halving is.")

    def test_gateway_assigns_provider_and_model(self):
        pipe, gw, rt = make([understand(provider="gemini", model="gemini-pro"),
                             verdict("complete")], ["ok answer"])
        out = pipe.run("write a python function")
        self.assertEqual(rt.requests[0].preferred_provider, "gemini")
        self.assertEqual(rt.requests[0].preferred_model, "gemini-pro")
        self.assertEqual(out["data"]["assigned"], "gemini/gemini-pro")
        # the Gateway was shown the real catalogue to choose from
        self.assertIn("provider=groq model=llama-fast", gw.calls[0][1]["content"])
        self.assertIn("provider=gemini model=gemini-pro", gw.calls[0][1]["content"])

    def test_invented_target_is_dropped_not_trusted(self):
        pipe, gw, rt = make([understand(provider="nope", model="ghost-9"),
                             verdict("complete")], ["answer"])
        out = pipe.run("hello there")
        self.assertIsNone(rt.requests[0].preferred_provider)
        self.assertIsNone(rt.requests[0].preferred_model)
        self.assertTrue(out["ok"])


class TestGatewayCategoryOverride(unittest.TestCase):
    def test_pipeline_states_its_own_category_for_both_gateway_calls(self):
        pipe, gw, rt = make([understand(), verdict("complete")], ["ok"])
        pipe.run("hello")
        self.assertEqual(gw.categories, ["general", "reasoning"])

    def test_real_gateway_honours_category_over_keyword_sniffing(self):
        """A prompt that merely mentions 'vision' must not hard-filter out a
        Gateway model that has no vision capability when a category is set."""
        from astra.ai.gateway import AstraAIGateway
        from astra.core.exceptions import ProviderError
        from astra.store import Store
        conn = _GwConnection(["ok-1", "ok-2"])
        gw = AstraAIGateway(connections=[conn], store=Store(":memory:"))
        msgs = [{"role": "user",
                 "content": "caps=chat,vision  screenshot of a table as json"}]
        with self.assertRaises(ProviderError):       # sniffed -> vision -> no model
            gw.chat(msgs, max_tokens=50)
        conn.replies = ["ok-2"]
        self.assertEqual(gw.chat(msgs, max_tokens=50, category="general"), "ok-2")


class TestVerificationKnowsCallOne(unittest.TestCase):
    def test_verifier_is_handed_request_criteria_assignment_and_output(self):
        pipe, gw, rt = make(
            [understand("Do X fully.", True, "gemini", "gemini-pro",
                        ["mentions X", "is in Bengali"]), verdict("complete")],
            ["the provider output"])
        pipe.run("x?")
        verify_prompt = gw.calls[1][1]["content"]
        self.assertIn("x?", verify_prompt)                  # original message
        self.assertIn("Do X fully.", verify_prompt)         # what call #1 assigned
        self.assertIn("mentions X", verify_prompt)          # criteria from call #1
        self.assertIn("is in Bengali", verify_prompt)
        self.assertIn("gemini/gemini-pro", verify_prompt)   # who got the work
        self.assertIn("the provider output", verify_prompt)  # what to judge


class TestFixAndRedoLoop(unittest.TestCase):
    def test_incomplete_output_is_fixed_and_only_fixed_output_reaches_user(self):
        pipe, gw, rt = make(
            [understand(), verdict("incomplete", ["no example"], "fix",
                                   "Add a concrete example."), verdict("complete")],
            ["draft without example", "answer WITH example"])
        out = pipe.run("explain recursion")
        self.assertEqual(out["reply"], "answer WITH example")
        self.assertNotIn("draft without example", out["reply"])
        # correction went to the SAME provider/model, pinned (no failover)
        fix_req = rt.requests[1]
        self.assertEqual((fix_req.preferred_provider, fix_req.preferred_model),
                         ("gemini", "gemini-pro"))
        self.assertTrue(fix_req.no_fallback)
        # and carried the Gateway's own instructions + a "continue/fix" ask
        correction = fix_req.messages[-1]["content"]
        self.assertIn("Add a concrete example.", correction)
        self.assertNotIn("from scratch", correction)
        self.assertEqual(out["data"]["verification"]["attempts"], 1)
        self.assertEqual(out["data"]["verification"]["status"], "COMPLETE")

    def test_redo_action_tells_provider_to_start_from_scratch(self):
        pipe, gw, rt = make(
            [understand(), verdict("incomplete", ["off topic"], "redo",
                                   "Answer the actual question about taxes."),
             verdict("complete")],
            ["totally off topic", "correct tax answer"])
        out = pipe.run("how are freelance taxes calculated?")
        correction = rt.requests[1].messages[-1]["content"]
        self.assertIn("Answer the actual question about taxes.", correction)
        self.assertIn("from scratch", correction)
        self.assertEqual(out["reply"], "correct tax answer")

    def test_loop_is_bounded_and_user_is_told_what_is_missing(self):
        never = [verdict("incomplete", ["the numbers"], "fix", "Add the numbers.")
                 for _ in range(MAX_CORRECTION_ATTEMPTS + 1)]
        pipe, gw, rt = make([understand()] + never,
                            ["a1"] + ["still bad"] * MAX_CORRECTION_ATTEMPTS)
        out = pipe.run("give me the numbers")
        # provider ran once + one per allowed correction, then it stops
        self.assertEqual(len(rt.requests), 1 + MAX_CORRECTION_ATTEMPTS)
        self.assertTrue(out["ok"])                       # user still gets the best answer
        self.assertIn("still bad", out["reply"])
        self.assertIn("100% confirm hoyni", out["reply"])  # ...honestly flagged
        self.assertIn("the numbers", out["reply"])
        self.assertNotEqual(out["data"]["verification"]["status"], "COMPLETE")


class TestFailOpen(unittest.TestCase):
    def test_gateway_unavailable_passes_straight_to_provider_unverified(self):
        pipe, gw, rt = make([], ["plain answer"], usable=False)
        out = pipe.run("hi")
        self.assertTrue(out["ok"])
        self.assertEqual(out["reply"], "plain answer")
        self.assertEqual(gw.calls, [])
        self.assertEqual(out["data"]["gateway"], "unavailable")
        self.assertEqual(out["data"]["verification"]["status"], "skipped")

    def test_understand_failure_uses_raw_message_and_still_verifies(self):
        pipe, gw, rt = make([RuntimeError("gw down"), verdict("complete")],
                            ["answer"])
        out = pipe.run("tell me a joke")
        self.assertIn("tell me a joke", user_text(rt.requests[0]))
        self.assertIsNone(rt.requests[0].preferred_provider)
        self.assertEqual(out["data"]["verification"]["status"], "COMPLETE")

    def test_verifier_unavailable_returns_answer_flagged_and_does_not_retry(self):
        pipe, gw, rt = make([understand(), RuntimeError("gw down")], ["answer"])
        out = pipe.run("hello")
        self.assertTrue(out["ok"])
        self.assertIn("answer", out["reply"])
        self.assertIn("verify korte parenni", out["reply"])
        self.assertEqual(len(rt.requests), 1)   # no wasted correction round-trips

    def test_unparsable_verifier_reply_is_treated_as_unavailable(self):
        pipe, gw, rt = make([understand(), "I think it looks fine!"], ["answer"])
        out = pipe.run("hello")
        self.assertIn("verify korte parenni", out["reply"])
        self.assertEqual(len(rt.requests), 1)

    def test_provider_failure_is_reported_honestly(self):
        pipe, gw, rt = make([understand()], [None])
        out = pipe.run("hello")
        self.assertFalse(out["ok"])
        self.assertIn("all providers failed", out["reply"])
        self.assertEqual(len(gw.calls), 1)      # nothing to verify

    def test_empty_message_is_rejected_without_ai_calls(self):
        pipe, gw, rt = make([], [])
        out = pipe.run("   ")
        self.assertFalse(out["ok"])
        self.assertEqual((gw.calls, rt.requests), ([], []))


class TestAgentHandle(unittest.TestCase):
    def test_handle_delegates_to_pipeline_and_forwards_context(self):
        pipe, gw, rt = make([understand(), verdict("complete")], ["hi there"])
        agent = Agent(pipeline=pipe)
        out = agent.handle("hello", context="earlier: we talked about cats")
        self.assertEqual(out["reply"], "hi there")
        self.assertIn("we talked about cats", gw.calls[0][1]["content"])

    def test_handle_without_pipeline_is_an_honest_error(self):
        out = Agent().handle("hello")
        self.assertFalse(out["ok"])
        self.assertEqual(out["action"], "none")

    def test_handle_never_raises(self):
        class Boom:
            def run(self, *a, **k):
                raise RuntimeError("kaboom")
        out = Agent(pipeline=Boom()).handle("hello")
        self.assertFalse(out["ok"])
        self.assertIn("kaboom", out["reply"])


class _Pool:
    def __bool__(self):
        return True


class _GwConnection:
    """A Gateway AI connection (its OWN model, used for understand/verify)."""
    name = "astra-gw-groq"
    models = ["gw-model"]

    def __init__(self, replies):
        self.pool = _Pool()
        self.replies = list(replies)
        self.prompts = []

    def health_check(self):
        return True

    def chat(self, messages, model=None, max_tokens=500):
        self.prompts.append(messages)
        return self.replies.pop(0)


class _ProviderAdapter:
    """A Provider (the model that does the work) behind the REAL router."""

    def __init__(self, name, models, replies):
        self.name, self.models, self.pool = name, models, _Pool()
        self.replies = list(replies)
        self.calls = []

    def health_check(self):
        return True

    def chat(self, messages, model=None, max_tokens=500, response_format=None):
        self.calls.append((model, messages, response_format))
        return self.replies.pop(0)


class TestRealRouterAndGatewayWiring(unittest.TestCase):
    """Same flow, but with the REAL AstraAIGateway + AstraRouter — only the
    outermost AI adapters are fake."""

    def _build(self, gw_replies, provider_replies):
        from astra.ai.gateway import AstraAIGateway
        from astra.ai.router import AstraRouter
        from astra.store import Store
        conn = _GwConnection(gw_replies)
        gw = AstraAIGateway(connections=[conn], store=Store(":memory:"))
        prov_a = _ProviderAdapter("groq", ["llama-fast"], [])
        prov_b = _ProviderAdapter("gemini", ["gemini-pro"], provider_replies)
        router = AstraRouter(providers=[prov_a, prov_b], gateway=gw)
        return ChatPipeline(gw, router, max_tokens=800), conn, prov_a, prov_b

    def test_available_targets_lists_real_provider_models_without_secrets(self):
        _, _, a, b = self._build([], [])
        pipe = self._build([], [])[0]
        targets = pipe.router.available_targets()
        self.assertEqual({(t["provider"], t["model"]) for t in targets},
                         {("groq", "llama-fast"), ("gemini", "gemini-pro")})
        for t in targets:
            self.assertEqual(set(t), {"provider", "model", "capabilities",
                                      "quality", "context_window"})

    def test_full_flow_with_fix_uses_assigned_provider_only(self):
        pipe, conn, groq, gemini = self._build(
            [understand("Explain DNS.", True, "gemini", "gemini-pro"),
             verdict("incomplete", ["no records"], "fix", "Explain DNS records."),
             verdict("complete")],
            ["DNS maps names.", "DNS maps names via A/MX records."])
        out = pipe.run("dns?")
        self.assertEqual(out["reply"], "DNS maps names via A/MX records.")
        self.assertEqual(len(gemini.calls), 2)   # first try + one fix
        self.assertEqual(groq.calls, [])         # the unassigned provider never ran
        self.assertEqual(len(conn.prompts), 3)   # understand + verify + verify
        # provider chat is plain chat: no forced JSON mode on the answer
        self.assertTrue(all(c[2] is None for c in gemini.calls))
        self.assertEqual(out["data"]["served_by"], "gemini/gemini-pro")


class TestChatEndpoint(unittest.TestCase):
    """POST /api/chat -> Agent.handle -> pipeline, over real HTTP."""

    def test_api_chat_returns_verified_reply_shape(self):
        import urllib.request
        from tests.helpers import LiveServer, make_stack

        stack = make_stack()
        pipe, gw, rt = make([understand(), verdict("complete")], ["verified hello"])
        stack["agent"] = Agent(pipeline=pipe)
        srv = LiveServer(stack=stack)
        self.addCleanup(srv.stop)
        req = urllib.request.Request(
            f"{srv.base}/api/chat",
            data=json.dumps({"message": "hello"}).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read())
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["reply"], "verified hello")
        self.assertEqual(body["data"]["action"], "none")
        self.assertEqual(body["data"]["data"]["verification"]["status"], "COMPLETE")


if __name__ == "__main__":
    unittest.main()
