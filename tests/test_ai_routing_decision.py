"""AI-driven routing: the Gateway's structured decision — not hard-coded
regex — chooses the task type and output modality.

    user request (English / Banglish / Bengali)
      -> Gateway UNDERSTAND reads the ACTUAL request + the live system context
      -> returns a structured `routing` decision (task_type + output_modalities)
      -> ChatPipeline routes on THAT decision
      -> image_generation -> real generate_image() -> stored artifact
      -> coding / chat    -> the normal text provider path

The regex `classify()` remains only as a lightweight FALLBACK for when the
Gateway is unavailable or returns no usable decision; it may never override
an AI-understood intent. Only the outermost provider adapters and the Gateway
are fakes here — ChatPipeline, ProviderRoutingDecision, AstraRouter, the
capability gate and the artifact pipeline are the real production code.
"""
import base64
import json
import unittest

from astra.ai.chat_pipeline import ChatPipeline, UNDERSTAND_SYSTEM_PROMPT
from astra.ai.gateway_contract import ProviderRoutingDecision
from astra.ai.gateway_task_completion import GatewayTaskCompletionSupervisor
from astra.ai.router import AstraRouter, RoutingResult, classify

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
IMAGE_DATA_URI = ("data:image/png;base64,"
                  + base64.b64encode(PNG_BYTES).decode("ascii"))
IMAGE_MODEL = "stability.stable-diffusion-xl-v1"
TEXT_MODEL = "llama-3.1-70b"

# The old prompt-side hack this refactor removed: an artificial task hint
# appended to the user's words. It must never come back.
ARTIFICIAL_HINT = "This request asks for a GENERATED IMAGE"


class _Pool:
    def __bool__(self):
        return True

    def pick(self, model=None):
        return object()


class _FakeAdapter:
    """A text-only provider adapter."""

    def __init__(self, name, models, replies=None):
        self.name = name
        self.models = list(models)
        self.pool = _Pool()
        self.replies = list(replies or [])
        self.chat_calls = []

    def health_check(self):
        return True

    def chat(self, messages, model=None, max_tokens=500, response_format=None):
        self.chat_calls.append((model, messages, response_format))
        return self.replies.pop(0) if self.replies else "ok"


class _FakeImageAdapter(_FakeAdapter):
    """A provider adapter with the REAL generate_image() contract."""

    def __init__(self, name, models, replies=None):
        super().__init__(name, models, replies)
        self.image_prompts = []
        self.image_models = []

    def generate_image(self, prompt, model=None, **kw):
        self.image_prompts.append(prompt)
        self.image_models.append(model)
        return IMAGE_DATA_URI


class _FakeGateway:
    def __init__(self, replies, usable=True):
        self.replies = list(replies)
        self.usable = usable
        self.prompts = []

    def is_usable(self):
        return self.usable

    def chat(self, messages, model=None, max_tokens=500, category=None,
             trace=""):
        self.prompts.append(messages)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def supervise_task(self, port, target, messages, result, contract, *,
                       evidence=None, semantic_verifier=None, max_tokens=500):
        return GatewayTaskCompletionSupervisor().supervise(
            port, target, messages, result, contract, evidence=evidence,
            semantic_verifier=semantic_verifier, max_tokens=max_tokens)


class _FilesRegistry:
    """Exposes a real 'files' tool but no runtime/terminal tool, so the tool
    loop is only entered when the Gateway's execution decision requires it."""

    def list(self, category=None):
        tools = [{"name": "file_read", "category": "files"}]
        if category is None:
            return list(tools)
        return [t for t in tools if t["category"] == category]


def understand(message, *, task_type=None, output_modalities=None,
               provider="", model="", criteria=("answers the request",),
               execution=None, reason="best fit", routing=True):
    """A Gateway UNDERSTAND reply. `routing=False` omits the routing object
    entirely (an old-style reply), which must fall back to the classifier."""
    d = {"final_request": message, "was_incomplete": False,
         "provider": provider, "model": model, "criteria": list(criteria),
         "reason": reason}
    if routing:
        d["routing"] = {"task_type": task_type if task_type is not None else "",
                        "output_modalities": list(output_modalities or []),
                        "intent": "what the user asked for"}
    if execution is not None:
        d["execution"] = execution
    return json.dumps(d)


def verdict(v="complete"):
    return json.dumps({"verdict": v, "missing": [], "action": "fix",
                       "instructions": ""})


class TestRoutingDecisionContract(unittest.TestCase):
    """The structured decision tolerates any shape and never invents a task."""

    def test_absent_or_malformed_decision_is_no_decision(self):
        for raw in (None, {}, [], "image", 7, {"task_type": 9}):
            d = ProviderRoutingDecision.from_dict(raw).normalized()
            self.assertEqual(d.task_type, "", raw)
            self.assertEqual(d.required_output_modalities, (), raw)

    def test_unknown_task_type_is_dropped(self):
        d = ProviderRoutingDecision.from_dict(
            {"task_type": "banana", "output_modalities": ["image"]})
        # The image modality still decides — the two halves are reconciled.
        self.assertEqual(d.normalized().task_type, "image_generation")

    def test_image_task_always_carries_the_image_modality(self):
        d = ProviderRoutingDecision.from_dict(
            {"task_type": "image_generation"}).normalized()
        self.assertEqual(d.task_type, "image_generation")
        self.assertEqual(d.required_output_modalities, ("image",))

    def test_image_modality_forces_the_image_task(self):
        d = ProviderRoutingDecision.from_dict(
            {"task_type": "simple_chat",
             "output_modalities": ["image"]}).normalized()
        self.assertEqual(d.task_type, "image_generation")
        self.assertEqual(d.required_output_modalities, ("image",))

    def test_non_executable_modalities_never_become_a_requirement(self):
        d = ProviderRoutingDecision.from_dict(
            {"task_type": "simple_chat",
             "output_modalities": ["audio", "video"]}).normalized()
        self.assertEqual(d.task_type, "simple_chat")
        self.assertEqual(d.output_modalities, ("audio", "video"))
        self.assertEqual(d.required_output_modalities, ())

    def test_plain_text_decision_requests_no_modality(self):
        d = ProviderRoutingDecision.from_dict(
            {"task_type": "coding", "output_modalities": ["text"]}).normalized()
        self.assertEqual(d.task_type, "coding")
        self.assertEqual(d.required_output_modalities, ())


class TestSystemPromptDefinesRouting(unittest.TestCase):
    """The Gateway's system prompt is what makes AI routing possible."""

    def test_prompt_defines_capabilities_paths_modalities_and_task_types(self):
        p = UNDERSTAND_SYSTEM_PROMPT
        for phrase in ("WHAT ASTRA CAN DO, AND THE EXECUTION PATHS",
                       "GENERATED IMAGE",
                       "DECIDE THE TASK TYPE + OUTPUT MODALITY",
                       "output_modalities",
                       "image_generation", "simple_chat", "coding",
                       "research", "planning", "translation",
                       "summarization"):
            self.assertIn(phrase, p, phrase)
        # The visual/terminal/browser paths are named so the model can choose
        # the right one.
        for path in ("terminal", "browser", "Agent Runtime",
                     "approval-gated host terminal"):
            self.assertIn(path, p, path)
        # ... and the JSON contract the pipeline parses.
        self.assertIn(chr(34) + "routing" + chr(34) + ":", p)

    def test_prompt_never_injects_an_artificial_task_hint(self):
        self.assertNotIn(ARTIFICIAL_HINT, UNDERSTAND_SYSTEM_PROMPT)


class TestAiDecisionDrivesRouting(unittest.TestCase):
    """English / Banglish / Bengali, across chat / coding / image."""

    CASES = (
        ("english-chat", "What is the capital of France?", "simple_chat", []),
        ("banglish-chat", "Ami ki bhabe valo hobo?", "simple_chat", []),
        ("bengali-chat", "তুমি কেমন আছো?", "simple_chat", []),
        ("english-coding", "Write a python function that adds two numbers",
         "coding", []),
        ("banglish-coding", "Akta python function likhe dao", "coding", []),
        ("bengali-coding", "একটা পাইথন ফাংশন লিখে দাও", "coding", []),
        ("english-image", "Create an image of a cyberpunk city",
         "image_generation", ["image"]),
        ("banglish-image", "akta chobi banao", "image_generation", ["image"]),
        ("bengali-image", "একটা ছবি বানাও", "image_generation", ["image"]),
    )

    def _pipeline(self, gw_replies, adapters):
        gw = _FakeGateway(gw_replies)
        router = AstraRouter(providers=list(adapters))
        return ChatPipeline(gw, router), gw

    def test_each_language_and_task_routes_on_the_ai_decision(self):
        for label, msg, task_type, mods in self.CASES:
            with self.subTest(case=label):
                text = _FakeAdapter("groq", [TEXT_MODEL],
                                    replies=["Here is your answer."])
                image = _FakeImageAdapter("bedrock", [IMAGE_MODEL])
                is_image = task_type == "image_generation"
                # The Gateway assigns a TEXT model for every case on purpose:
                # for an image request capability validation must still land
                # it on the image-capable provider.
                replies = [understand(msg, task_type=task_type,
                                      output_modalities=mods,
                                      provider="groq", model=TEXT_MODEL)]
                if not is_image:
                    replies.append(verdict("complete"))
                pipe, gw = self._pipeline(replies, [text, image])
                out = pipe.run(msg)

                self.assertTrue(out["ok"], (label, out))
                # The AI decision — not the regex — picked the task type.
                self.assertEqual(out["data"]["task_type"], task_type, label)
                self.assertEqual(out["data"]["routing"]["task_type"],
                                 task_type, label)
                self.assertEqual(out["data"]["routing"]["output_modalities"],
                                 mods, label)
                if is_image:
                    self.assertEqual(out["data"]["served_by"],
                                     f"bedrock/{IMAGE_MODEL}", label)
                    self.assertEqual(image.image_prompts, [msg], label)
                    self.assertEqual(len(out.get("artifacts") or []), 1, label)
                    self.assertEqual(text.chat_calls, [], label)
                else:
                    self.assertEqual(out["data"]["served_by"],
                                     f"groq/{TEXT_MODEL}", label)
                    self.assertEqual(image.image_prompts, [], label)
                    self.assertNotIn("artifacts", out, label)
                    self.assertEqual(len(text.chat_calls), 1, label)

    def test_image_decision_uses_the_real_generate_image_api(self):
        image = _FakeImageAdapter("bedrock", [IMAGE_MODEL])
        text = _FakeAdapter("groq", [TEXT_MODEL])
        pipe, _ = self._pipeline(
            [understand("একটা ছবি বানাও", task_type="image_generation",
                        output_modalities=["image"],
                        provider="groq", model=TEXT_MODEL)],
            [text, image])
        out = pipe.run("একটা ছবি বানাও")
        self.assertTrue(out["ok"], out)
        # The provider's real image API ran, with the ORIGINAL request.
        self.assertEqual(image.image_prompts, ["একটা ছবি বানাও"])
        arts = out.get("artifacts") or []
        self.assertEqual(arts[0]["artifact_type"], "image")
        self.assertNotIn("base64", out["reply"])


class TestAiDecisionOverridesRegex(unittest.TestCase):
    """The regex is a hint only: the AI's understanding always wins."""

    def test_ai_says_chat_though_the_regex_would_say_image(self):
        msg = "I do not want you to create an image, just explain recursion"
        self.assertEqual(classify(msg), "image_generation")  # regex misfires
        text = _FakeAdapter("groq", [TEXT_MODEL], replies=["Recursion is ..."])
        image = _FakeImageAdapter("bedrock", [IMAGE_MODEL])
        pipe = ChatPipeline(_FakeGateway(
            [understand(msg, task_type="simple_chat", output_modalities=[],
                        provider="groq", model=TEXT_MODEL),
             verdict("complete")]), AstraRouter(providers=[text, image]))
        out = pipe.run(msg)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["task_type"], "simple_chat")
        self.assertEqual(out["data"]["served_by"], f"groq/{TEXT_MODEL}")
        self.assertEqual(image.image_prompts, [])
        self.assertNotIn("artifacts", out)

    def test_ai_says_image_though_the_regex_would_not(self):
        msg = "amar ekta cyberpunk city er visual chai"
        self.assertEqual(classify(msg), "simple_chat")  # regex misses it
        text = _FakeAdapter("groq", [TEXT_MODEL])
        image = _FakeImageAdapter("bedrock", [IMAGE_MODEL])
        pipe = ChatPipeline(_FakeGateway(
            [understand(msg, task_type="image_generation",
                        output_modalities=["image"],
                        provider="groq", model=TEXT_MODEL)]),
            AstraRouter(providers=[text, image]))
        out = pipe.run(msg)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["task_type"], "image_generation")
        self.assertEqual(out["data"]["served_by"], f"bedrock/{IMAGE_MODEL}")
        self.assertEqual(image.image_prompts, [msg])
        self.assertEqual(len(out.get("artifacts") or []), 1)


class TestRegexFallback(unittest.TestCase):
    """`classify()` still works when there is no usable AI decision."""

    def test_gateway_unavailable_falls_back_to_the_classifier(self):
        image = _FakeImageAdapter("bedrock", [IMAGE_MODEL])
        text = _FakeAdapter("groq", [TEXT_MODEL])
        pipe = ChatPipeline(_FakeGateway([], usable=False),
                            AstraRouter(providers=[text, image]))
        out = pipe.run("akta chobi banao")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["served_by"], f"bedrock/{IMAGE_MODEL}")
        self.assertEqual(len(out.get("artifacts") or []), 1)

    def test_unknown_ai_task_falls_back_to_the_classifier(self):
        image = _FakeImageAdapter("bedrock", [IMAGE_MODEL])
        text = _FakeAdapter("groq", [TEXT_MODEL])
        pipe = ChatPipeline(_FakeGateway(
            [understand("akta chobi banao", task_type="banana",
                        output_modalities=[])]),
            AstraRouter(providers=[text, image]))
        out = pipe.run("akta chobi banao")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["task_type"], "image_generation")
        self.assertEqual(out["data"]["served_by"], f"bedrock/{IMAGE_MODEL}")

    def test_old_style_reply_without_routing_still_classifies(self):
        text = _FakeAdapter("groq", [TEXT_MODEL], replies=["def add(): ..."])
        pipe = ChatPipeline(_FakeGateway(
            [understand("Python code likhe dao", routing=False,
                        provider="groq", model=TEXT_MODEL),
             verdict("complete")]), AstraRouter(providers=[text]))
        out = pipe.run("Python code likhe dao")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["task_type"], "coding")


class TestOriginalRequestAndContext(unittest.TestCase):
    """The provider gets the ORIGINAL request + canonical history, and no
    artificial task text is injected anywhere."""

    def test_provider_receives_original_request_and_history_unchanged(self):
        text = _FakeAdapter("groq", [TEXT_MODEL], replies=["Paris."])
        hist = [{"role": "user", "content": "earlier question"},
                {"role": "assistant", "content": "earlier answer"}]
        gw = _FakeGateway(
            [understand("What is the capital of France?",
                        task_type="simple_chat", output_modalities=[],
                        provider="groq", model=TEXT_MODEL),
             verdict("complete")])
        pipe = ChatPipeline(gw, AstraRouter(providers=[text]))
        out = pipe.run("What is the capital of France?", history=hist)
        self.assertTrue(out["ok"], out)

        messages = text.chat_calls[0][1]
        self.assertIn("You are Astra", messages[0]["content"])
        self.assertEqual(messages[1:3], hist, "history passed through unchanged")
        self.assertEqual(messages[-1], {"role": "user",
                                        "content": "What is the capital of France?"})

        # The Gateway read the same original request + history, and no
        # artificial task hint was appended to the user's words.
        gw_prompt = gw.prompts[0][1]["content"]
        self.assertIn("User message:\nWhat is the capital of France?", gw_prompt)
        self.assertIn("earlier question", gw_prompt)
        self.assertNotIn(ARTIFICIAL_HINT, gw_prompt)

    def test_understanding_prompt_uses_the_raw_bangla_words(self):
        image = _FakeImageAdapter("bedrock", [IMAGE_MODEL])
        gw = _FakeGateway(
            [understand("একটা ছবি বানাও", task_type="image_generation",
                        output_modalities=["image"],
                        provider="groq", model=TEXT_MODEL)])
        pipe = ChatPipeline(gw, AstraRouter(
            providers=[_FakeAdapter("groq", [TEXT_MODEL]), image]))
        out = pipe.run("একটা ছবি বানাও")
        self.assertTrue(out["ok"], out)
        gw_prompt = gw.prompts[0][1]["content"]
        self.assertIn("User message:\nএকটা ছবি বানাও", gw_prompt)
        self.assertNotIn(ARTIFICIAL_HINT, gw_prompt)
        # ... and the image API received exactly the user's own words.
        self.assertEqual(image.image_prompts, ["একটা ছবি বানাও"])


class _BranchSpy(ChatPipeline):
    """Records which execution branch the pipeline chose, without running the
    real tool loop (the test is about branch selection, not loop internals)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.branch = ""
        self.loop_messages = []

    def _run_tool_loop(self, brief, hist_turns, ctx_text, task_type, vision,
                       scope, session_id, terminal_context, exec_context, req,
                       trace, messages, content):
        self.branch = "tool_loop"
        self.loop_messages = messages
        return RoutingResult(provider="groq", model=TEXT_MODEL,
                             text="ran the tools", ok=True)

    def _route(self, *a, **kw):
        self.branch = "single_call"
        return super()._route(*a, **kw)


class TestExecutionDecisionBranch(unittest.TestCase):
    """A coding request the Gateway marks execution-required goes through the
    real tool-execution path; a plain one does not."""

    def _run(self, required):
        gw = _FakeGateway(
            [understand("Fix the failing tests", task_type="coding",
                        output_modalities=[], provider="groq",
                        model=TEXT_MODEL,
                        execution={"required": required,
                                   "capability": "files",
                                   "intent": "inspect and fix the tests"}),
             verdict("complete")])
        pipe = _BranchSpy(gw, AstraRouter(
            providers=[_FakeAdapter("groq", [TEXT_MODEL])]),
            registry=_FilesRegistry())
        out = pipe.run("Fix the failing tests")
        return pipe, out

    def test_execution_required_uses_the_tool_loop(self):
        pipe, out = self._run(True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["task_type"], "coding")
        self.assertEqual(pipe.branch, "tool_loop")
        self.assertIn("REQUIRES real tool execution",
                      pipe.loop_messages[0]["content"])

    def test_plain_coding_request_stays_a_single_call(self):
        pipe, out = self._run(False)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["task_type"], "coding")
        self.assertEqual(pipe.branch, "single_call")


class TestCapabilityValidationPreserved(unittest.TestCase):
    """AI-driven routing never lets a text model serve an image request."""

    def test_ai_assigned_text_model_is_rejected_for_an_image_request(self):
        text = _FakeAdapter("groq", [TEXT_MODEL], replies=["Here is an image!"])
        image = _FakeImageAdapter("bedrock", [IMAGE_MODEL])
        pipe = ChatPipeline(_FakeGateway(
            [understand("akta chobi banao", task_type="image_generation",
                        output_modalities=["image"],
                        provider="groq", model=TEXT_MODEL)]),
            AstraRouter(providers=[text, image]))
        out = pipe.run("akta chobi banao")
        self.assertTrue(out["ok"], out)
        self.assertEqual(text.chat_calls, [], "text model must not be used")
        self.assertEqual(out["data"]["served_by"], f"bedrock/{IMAGE_MODEL}")

    def test_image_request_with_no_capable_provider_fails_honestly(self):
        text = _FakeAdapter("groq", [TEXT_MODEL], replies=["Sure, here you go"])
        pipe = ChatPipeline(_FakeGateway(
            [understand("akta chobi banao", task_type="image_generation",
                        output_modalities=["image"],
                        provider="groq", model=TEXT_MODEL)]),
            AstraRouter(providers=[text]))
        out = pipe.run("akta chobi banao")
        self.assertFalse(out["ok"], out)
        self.assertNotIn("artifacts", out)
        self.assertEqual(text.chat_calls, [])
        self.assertIn("image", out["reply"].lower())

    def test_explicit_image_model_preference_is_honoured(self):
        other = "some-other-image-model"
        image = _FakeImageAdapter("bedrock", [other, IMAGE_MODEL])
        pipe = ChatPipeline(_FakeGateway(
            [understand("akta chobi banao", task_type="image_generation",
                        output_modalities=["image"],
                        provider="bedrock", model=IMAGE_MODEL)]),
            AstraRouter(providers=[image]))
        out = pipe.run("akta chobi banao")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["served_by"], f"bedrock/{IMAGE_MODEL}")
        self.assertEqual(image.image_models, [IMAGE_MODEL])


if __name__ == "__main__":
    unittest.main()
