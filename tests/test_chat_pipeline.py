"""Chat pipeline: User -> Gateway (understand+assign) -> Provider ->
Gateway (verify, fix/redo loop) -> User.

Uses scripted fakes for the Gateway's AI calls and the router, but the REAL
bounded verify/correct supervisor (GatewayTaskCompletionSupervisor), so the
loop semantics tested here are the ones that run in production.
"""
import json
import os
import tempfile
import unittest


from astra.agent import Agent
from astra.ai.chat_pipeline import ChatPipeline
from astra.ai.chat_pipeline import _NO_GATEWAY_CONFIGURED_MESSAGE
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
               model="gemini-pro", criteria=("answers the question",),
               task_type=None):
    data = {"final_request": final_request,
            "was_incomplete": was_incomplete, "provider": provider,
            "model": model, "criteria": list(criteria),
            "reason": "best fit"}
    if task_type is not None:
        data["task_type"] = task_type
    return json.dumps(data)


def verdict(v="complete", missing=(), action="fix", instructions=""):
    return json.dumps({"verdict": v, "missing": list(missing),
                       "action": action, "instructions": instructions})


class FakeGateway:
    """Scripted Gateway: `chat()` pops replies (str, or Exception to raise)."""

    def __init__(self, replies, usable=True):
        self.replies = list(replies)
        self.calls = []            # every gateway.chat() messages list
        self.categories = []       # the explicit category of each call
        self.traces = []           # correlation id forwarded for each call
        self.usable = usable

    def is_usable(self):
        return self.usable

    def chat(self, messages, model=None, max_tokens=500, category=None,
             trace=""):
        self.calls.append(messages)
        self.categories.append(category)
        self.traces.append(trace)
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


class FakeImageRouter:
    """Records `ImageRouter.generate()` calls and returns a scripted result.

    This is the ONLY image execution path ChatPipeline may use: it must never
    fall back to the Provider router's own image dispatch
    (`FakeRouter.route_request` with task_type == "image_generation")."""

    def __init__(self, result=None):
        self.result = result if result is not None else _png_data_uri()
        self.calls = []                # every generate() call's arguments

    def generate(self, prompt, model=None, size="1024x1024", n=1, *,
                 editing=False, source_images=None, trace="", discover=True):
        self.calls.append({"prompt": prompt, "model": model, "size": size,
                           "n": n, "editing": editing,
                           "source_images": source_images or [],
                           "trace": trace, "discover": discover})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _png_data_uri():
    import base64
    return "data:image/png;base64," + base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 200).decode()


def make(gateway_replies, outputs, **kw):
    gw = FakeGateway(gateway_replies, usable=kw.pop("usable", True))
    rt = FakeRouter(outputs)
    # ChatPipeline's image path goes through gateway.image_router (the real
    # ImageRouter in production); the fake records the calls so the wiring can
    # be asserted directly.
    gw.image_router = FakeImageRouter(kw.pop("image_router_result", None))
    # Optional deterministic artifact directory: tests that generate images
    # pass their own temp dir instead of relying on the shared system temp
    # path ({tempdir}/astra/artifacts). None keeps production behaviour.
    artifact_dir = kw.pop("artifact_dir", None)
    return ChatPipeline(gw, rt, max_tokens=800,
                        artifact_dir=artifact_dir), gw, rt


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
        self.assertEqual(gw.categories, ["control", "control"])

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
        self.assertEqual(out["reply"], "still bad")       # exactly the answer, no caveat text
        self.assertNotIn("100% confirm hoyni", out["reply"])   # not leaked into chat...
        self.assertNotIn("Missing:", out["reply"])
        # ...but still honestly flagged for the Activity Log / server logs
        self.assertIn("100% confirm hoyni", out["data"]["internal_note"])
        self.assertIn("the numbers", out["data"]["internal_note"])
        self.assertNotEqual(out["data"]["verification"]["status"], "COMPLETE")
        self.assertIn("the numbers", out["data"]["verification"]["missing"])


class TestFailOpen(unittest.TestCase):
    def test_gateway_unavailable_passes_straight_to_provider_unverified(self):
        pipe, gw, rt = make([], ["plain answer"], usable=False)
        out = pipe.run("hi")
        self.assertTrue(out["ok"])
        # Pass-through as before, but the reply now carries the plain-text
        # notice that Gateway verification was skipped because no GW_* API
        # key is configured (deliberate, see chat_pipeline
        # ._NO_GATEWAY_CONFIGURED_MESSAGE: the user is told instead of the
        # skip being silent forever).
        self.assertEqual(out["reply"],
                         "plain answer\n\n" + _NO_GATEWAY_CONFIGURED_MESSAGE)
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
        self.assertEqual(out["reply"], "answer")   # no internal caveat appended
        self.assertNotIn("verify korte parenni", out["reply"])
        self.assertIn("verify korte parenni", out["data"]["internal_note"])
        self.assertEqual(len(rt.requests), 1)   # no wasted correction round-trips

    def test_unparsable_verifier_reply_is_treated_as_unavailable(self):
        pipe, gw, rt = make([understand(), "I think it looks fine!"], ["answer"])
        out = pipe.run("hello")
        self.assertEqual(out["reply"], "answer")
        self.assertNotIn("verify korte parenni", out["reply"])
        self.assertIn("verify korte parenni", out["data"]["internal_note"])
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


class TestNoInternalDebugLeak(unittest.TestCase):
    """Regression coverage for the chat-output bug where Gateway/verification
    debug text ("Missing: ...", "...100% confirm hoyni", "verify korte
    parenni", routing/assignment detail) was concatenated onto the
    user-facing `reply` string. `reply` is exactly what ChatLog stores and
    the chat UI renders (see astra/web.py `add_reply` + static/js/astra.js
    `chatBubble`), so any of this text landing there is a leak straight into
    the chat window. Every scenario below must produce a `reply` containing
    ONLY the assistant's actual answer; the diagnostic detail must still be
    retrievable from `data` for the Activity Log / server logs.
    """

    FORBIDDEN = ("Missing:", "confirm hoyni", "verify korte parenni",
                 "verify kora hoyni", "Gateway", "⚠️", "assign_reason",
                 "criteria")

    def _assert_clean(self, out):
        for phrase in self.FORBIDDEN:
            self.assertNotIn(phrase, out["reply"],
                             f"leaked internal text {phrase!r} into reply: "
                             f"{out['reply']!r}")

    def test_bounded_correction_loop_reply_is_clean(self):
        never = [verdict("incomplete", ["the numbers"], "fix", "Add the numbers.")
                 for _ in range(MAX_CORRECTION_ATTEMPTS + 1)]
        pipe, gw, rt = make([understand()] + never,
                            ["a1"] + ["still bad"] * MAX_CORRECTION_ATTEMPTS)
        out = pipe.run("give me the numbers")
        self._assert_clean(out)
        self.assertEqual(out["reply"], "still bad")
        # the same detail must still reach the logs, just not the chat
        self.assertIn("Missing", out["data"]["internal_note"])
        self.assertEqual(out["data"]["verification"]["missing"], ["the numbers"])

    def test_verifier_crash_reply_is_clean(self):
        pipe, gw, rt = make([understand(), RuntimeError("gw down")], ["answer"])
        out = pipe.run("hello")
        self._assert_clean(out)
        self.assertEqual(out["reply"], "answer")
        self.assertIn("verify korte parenni", out["data"]["internal_note"])

    def test_unparsable_verdict_reply_is_clean(self):
        pipe, gw, rt = make([understand(), "I think it looks fine!"], ["answer"])
        out = pipe.run("hello")
        self._assert_clean(out)
        self.assertEqual(out["reply"], "answer")

    def test_gateway_verify_supervision_error_reply_is_clean(self):
        """`supervise_task` itself raising is a separate branch from a bad
        verifier reply (astra/ai/chat_pipeline.py's `except Exception as e`
        around `self.gateway.supervise_task`) and carries its own note."""
        class BoomingGateway:
            def is_usable(self):
                return True

            def chat(self, *a, **k):
                return understand()

            def supervise_task(self, *a, **k):
                raise RuntimeError("supervisor exploded")

        rt = FakeRouter(["answer"])
        pipe = ChatPipeline(BoomingGateway(), rt, max_tokens=800)
        out = pipe.run("hello")
        self._assert_clean(out)
        self.assertEqual(out["reply"], "answer")
        self.assertIn("supervisor exploded", out["data"]["verification"]["reason"])

    def test_complete_happy_path_reply_is_clean(self):
        pipe, gw, rt = make(
            [understand(was_incomplete=False), verdict("complete")],
            ["Paris is the capital of France."])
        out = pipe.run("What is the capital of France?")
        self._assert_clean(out)
        self.assertEqual(out["reply"], "Paris is the capital of France.")

    def test_reply_field_is_exactly_what_chat_log_and_ui_would_store(self):
        """Guards the wiring itself: `_reply`'s `text` argument must be the
        only thing that ends up as `out["reply"]` — `note` may only affect
        `data`. If a future change reintroduces string concatenation this
        catches it directly, independent of any particular wording.

        Runs on the Gateway-available path: the ONE deliberate exception to
        the no-concatenation rule is the missing-Gateway-key notice asserted
        in TestFailOpen above (a user-facing message, not internal debug)."""
        pipe, gw, rt = make([understand()], ["clean answer"], usable=True)
        out = pipe.run("hi")
        self.assertEqual(out["reply"], "clean answer")
        self.assertNotIn("\n\n⚠️", out["reply"])


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
            # `health` is part of the documented catalogue the Gateway's
            # "assign" call sees (router.available_targets; commit 976b2ea) —
            # the Gateway must not be blind to real per-model health.
            self.assertEqual(set(t), {"provider", "model", "capabilities",
                                      "quality", "context_window", "health"})

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


class TestImageGenerationPipelineWiring(unittest.TestCase):
    """ChatPipeline._route() must translate an image-generation task_type
    into RoutingRequest.required_output_modalities=["image"], for English
    and Bangla/Banglish phrasing alike, and the generated image must reach
    the user as a real artifact — not a wall of base64 text pretending to be
    the chat reply. Uses the same FakeGateway/FakeRouter harness as the rest
    of this file. Image execution is owned EXCLUSIVELY by the Gateway's
    ImageRouter, so the wiring is asserted against `gateway.image_router`
    calls -- and the Provider router (FakeRouter) must receive NO
    image_generation request at all."""

    _PNG_DATA_URI = None

    def setUp(self):
        # Deterministic, writable artifact storage that is cleaned up
        # automatically after each test. ChatPipeline must never fall back to
        # the shared system temp path ({tempdir}/astra/artifacts) here.
        self._artifacts_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._artifacts_tmp.cleanup)
        self.artifact_dir = self._artifacts_tmp.name

    def _make(self, gateway_replies, outputs, **kw):
        kw.setdefault("artifact_dir", self.artifact_dir)
        return make(gateway_replies, outputs, **kw)

    @classmethod
    def setUpClass(cls):
        import base64
        img = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
        cls._PNG_DATA_URI = f"data:image/png;base64,{base64.b64encode(img).decode()}"

    def test_english_image_request_sets_output_modality(self):
        # was_incomplete=False (the understand() default) means the Gateway's
        # final_request is NOT what reaches _task_type() — the ORIGINAL
        # message is (see ChatPipeline._understand: `"final_request":
        # ... if rewrote else message`) — so it's the literal text below
        # that classify() must recognize, not anything scripted into
        # understand().
        pipe, gw, rt = self._make([understand(), verdict("complete")],
                                  [self._PNG_DATA_URI])
        pipe.run("generate an image of a sunset over the mountains")
        self.assertEqual(len(gw.image_router.calls), 1)
        self.assertEqual(gw.image_router.calls[0]["prompt"],
                         "generate an image of a sunset over the mountains")
        self.assertEqual(rt.requests, [])          # old path never used

    def test_bangla_image_request_sets_output_modality(self):
        pipe, gw, rt = self._make([understand(), verdict("complete")],
                                  [self._PNG_DATA_URI])
        pipe.run("akta chobi banao")
        self.assertEqual([c["prompt"] for c in gw.image_router.calls],
                         ["akta chobi banao"])
        self.assertEqual(rt.requests, [])

    def test_bangla_script_image_request_sets_output_modality(self):
        """Real Bengali script, not just Latin-script Banglish."""
        pipe, gw, rt = self._make([understand(), verdict("complete")],
                                  [self._PNG_DATA_URI])
        pipe.run("\u098f\u0995\u099f\u09be \u099b\u09ac\u09bf \u09ac\u09be\u09a8\u09be\u0993")
        self.assertEqual(len(gw.image_router.calls), 1)
        self.assertEqual(rt.requests, [])

    def test_banglish_photo_create_koro_sets_output_modality(self):
        pipe, gw, rt = self._make([understand(), verdict("complete")],
                                  [self._PNG_DATA_URI])
        pipe.run("photo create koro")
        self.assertEqual(len(gw.image_router.calls), 1)
        self.assertEqual(rt.requests, [])

    def test_gateway_task_type_is_authoritative_for_execution_path(self):
        """The Gateway's UNDERSTAND API call owns task classification.
        An ambiguous user message must follow the task_type returned by the
        Gateway rather than being re-classified locally by ChatPipeline."""
        pipe, gw, rt = self._make(
            [understand(task_type="image_generation"), verdict("complete")],
            [self._PNG_DATA_URI])
        pipe.run("make something for me")
        self.assertEqual(len(gw.image_router.calls), 1)
        self.assertEqual(rt.requests, [])

    def test_gateway_task_type_coding_is_authoritative(self):
        pipe, gw, rt = self._make(
            [understand(task_type="coding"), verdict("complete")],
            ["implemented"])
        pipe.run("do something with my project")
        self.assertEqual(len(rt.requests), 1)
        self.assertEqual(rt.requests[0].task_type, "coding")
        self.assertEqual(gw.image_router.calls, [])

    def test_normal_chat_request_gets_no_output_modality(self):
        """Regression guard: 'python code likhe dao' and other ordinary
        requests must not be redirected through the image-generation path."""
        pipe, gw, rt = self._make([understand(), verdict("complete")],
                                  ["def reverse(s): return s[::-1]"])
        pipe.run("python code likhe dao — ekta reverse string function")
        self.assertNotEqual(rt.requests[0].task_type, "image_generation")
        self.assertEqual(rt.requests[0].required_output_modalities, [])
        self.assertEqual(gw.image_router.calls, [])   # not an image turn

    def test_provider_router_is_never_used_for_an_image_request(self):
        """Hard architectural guard: even when ImageRouter fails, ChatPipeline
        must NOT fall back to the Provider router's image dispatch -- the
        duplicate execution path this architecture forbids."""
        pipe, gw, rt = self._make([understand(), verdict("complete")], [],
                                  image_router_result=RuntimeError("boom"))
        out = pipe.run("generate an image of a sunset")
        self.assertFalse(out["ok"])
        self.assertEqual(len(gw.image_router.calls), 1)
        self.assertEqual(rt.requests, [])

    def test_generated_image_reaches_the_user_as_a_real_artifact(self):
        pipe, gw, rt = self._make([understand(), verdict("complete")],
                                  [self._PNG_DATA_URI])
        out = pipe.run("generate an image of a sunset over the mountains")
        self.assertTrue(out["ok"])
        arts = out.get("artifacts") or []
        self.assertEqual(len(arts), 1)                 # exactly one artifact
        self.assertEqual(arts[0]["artifact_type"], "image")
        self.assertTrue(arts[0]["validated"])
        # A REAL, readable image file in the deterministic temp dir -- not
        # just metadata. store_artifact names files {id}_{filename}.
        path = os.path.join(self.artifact_dir,
                            "%s_%s" % (arts[0]["id"], arts[0]["filename"]))
        self.assertTrue(os.path.isfile(path), path)
        self.assertEqual(arts[0]["mime_type"], "image/png")
        with open(path, "rb") as f:
            raw = f.read()
        self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_image_task_emits_a_gateway_handoff_event_distinct_from_the_turn(self):
        """The Gateway's OWN classification/handoff step
        (chat.pipeline.image_dispatch) must be visible in the Activity Log
        as its own row, separate from the real provider API call ImageRouter
        reports (astra_gateway.* with category=image_generation) and from
        the turn's own op:chat:<req> started/finished row -- see
        static/js/log_model.js::titleOf and the "Gateway image handoff"
        tests in tests/js/log_model.test.js."""
        from astra.core.events import EventBus
        from astra.store import Store

        bus = EventBus(Store(":memory:"))
        gw = FakeGateway([understand(), verdict("complete")])
        rt = FakeRouter([])
        gw.image_router = FakeImageRouter(self._PNG_DATA_URI)
        pipe = ChatPipeline(gw, rt, events=bus, max_tokens=800,
                            artifact_dir=self.artifact_dir)
        pipe.run("generate an image of a sunset over the mountains")

        rows = [e for e in bus.history(limit=200)
               if e["kind"] == "chat.pipeline.image_dispatch"]
        self.assertEqual(len(rows), 1)
        d = rows[0]["data"]
        self.assertEqual(d["task"], "image_generation")
        self.assertEqual(d["route"], "ImageRouter")
        # Deliberately NOT sharing the turn's own `op:chat:<req>` key (see
        # chat_pipeline.py comment at the emit site): merging into that
        # lifecycle row would let this event's title overwrite "Request
        # received"/"Response generated" instead of appending its own row.
        self.assertNotIn("op", d)

    def test_non_image_task_never_emits_the_image_handoff_event(self):
        from astra.core.events import EventBus
        from astra.store import Store

        bus = EventBus(Store(":memory:"))
        gw = FakeGateway([understand(), verdict("complete")])
        rt = FakeRouter(["def reverse(s): return s[::-1]"])
        gw.image_router = FakeImageRouter(self._PNG_DATA_URI)
        pipe = ChatPipeline(gw, rt, events=bus, max_tokens=800,
                            artifact_dir=self.artifact_dir)
        pipe.run("python code likhe dao")

        kinds = {e["kind"] for e in bus.history(limit=200)}
        self.assertNotIn("chat.pipeline.image_dispatch", kinds)

    def test_raw_base64_never_leaks_into_the_visible_reply(self):
        """The image is delivered via `artifacts`; the visible `reply` text
        must never contain the raw data URI (see
        astra.ai.response_boundary.sanitize_final_response)."""
        pipe, gw, rt = self._make([understand(), verdict("complete")],
                                  [self._PNG_DATA_URI])
        out = pipe.run("generate an image of a sunset over the mountains")
        self.assertNotIn("base64,", out["reply"])
        self.assertTrue((out["reply"] or "").strip())  # some human-readable line remains


if __name__ == "__main__":
    unittest.main()
