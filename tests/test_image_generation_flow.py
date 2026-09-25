"""Image-generation flow, end to end.

Pins the whole chain that the Astra Chat image path is supposed to be:

    user request (English / Banglish / Bengali)
      -> ChatPipeline classifies it as image_generation
      -> RoutingRequest carries required_output_modalities=["image"]
      -> AstraRouter selects ONLY an image-capable (adapter, model) pair
      -> adapter.generate_image()
      -> the returned data URI becomes a stored artifact
      -> the chat reply carries that artifact (never the raw base64).

Only the outermost provider adapters and the Gateway here are fakes; the
classifier, the routing policy/capability gate, the artifact pipeline and the
chat pipeline itself are the real production code.
"""
import base64
import json
import os
import tempfile
import unittest

from astra.ai.chat_pipeline import ChatPipeline
from astra.ai.gateway_task_completion import GatewayTaskCompletionSupervisor
from astra.ai.router import AstraRouter, RoutingRequest, classify
from astra.core.artifacts import Artifact

# A syntactically valid PNG (real 8-byte signature, then padding): passes both
# the artifact pipeline's ">100 bytes" check and its PNG header validation.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
IMAGE_DATA_URI = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")

IMAGE_MODEL = "stability.stable-diffusion-xl-v1"
TEXT_MODEL = "llama-3.1-70b"


class _Pool:
    def __bool__(self):
        return True

    def pick(self, model=None):
        return object()


class _FakeAdapter:
    """A provider adapter with no image support at all."""

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
    """Scripted Gateway: understand/verify replies plus a supervise_task
    recorder, so a test can assert whether the text verifier ran at all."""

    def __init__(self, replies, usable=True):
        self.replies = list(replies)
        self.usable = usable
        self.prompts = []
        self.supervised = []

    def is_usable(self):
        return self.usable

    def chat(self, messages, model=None, max_tokens=500, category=None, trace=""):
        self.prompts.append(messages)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def supervise_task(self, port, target, messages, result, contract, *,
                       evidence=None, semantic_verifier=None, max_tokens=500):
        self.supervised.append((target, result))
        return GatewayTaskCompletionSupervisor().supervise(
            port, target, messages, result, contract, evidence=evidence,
            semantic_verifier=semantic_verifier, max_tokens=max_tokens)


class _FakeRegistry:
    """A ToolRegistry that reports a runtime tool, so `_tool_loop_usable()`
    is True — the image path must still bypass the tool loop."""

    def list(self, category=None):
        return [{"name": "terminal_exec", "category": "runtime"}]


def understand(final_request, provider="", model="", criteria=("answers the request",)):
    return json.dumps({"final_request": final_request, "was_incomplete": False,
                       "provider": provider, "model": model,
                       "criteria": list(criteria), "reason": "best fit"})


def verdict(v="complete", missing=(), action="fix", instructions=""):
    return json.dumps({"verdict": v, "missing": list(missing),
                       "action": action, "instructions": instructions})


def image_request(message, **kw):
    return RoutingRequest(task_type="image_generation", messages=[
        {"role": "user", "content": message}],
        required_output_modalities=["image"], **kw)


class TestImageIntentDetection(unittest.TestCase):
    """The classifier must recognise English, Banglish and Bengali requests."""

    def test_image_requests_are_detected(self):
        for msg in ("Create an image of a cyberpunk city",
                    "generate an image of a cat",
                    "akta chobi banao",
                    "ekta chhobi banao",
                    "photo create koro",
                    "chobi create koro",
                    "একটা ছবি বানাও",
                    "ছবি তৈরি করো",
                    "একটা futuristic city photo তৈরি করো"):
            self.assertEqual(classify(msg), "image_generation", msg)

    def test_plain_chat_is_not_image_generation(self):
        for msg in ("Python code likhe dao",
                    "hello there",
                    "start the billing service then make a report",
                    "I took a photo of the sunset"):
            self.assertNotEqual(classify(msg), "image_generation", msg)


class TestRouterImageRouting(unittest.TestCase):
    """The router must select only models/providers that can really generate."""

    def test_image_request_reaches_generate_image_on_the_image_provider(self):
        text = _FakeAdapter("groq", [TEXT_MODEL])
        image = _FakeImageAdapter("bedrock", [IMAGE_MODEL])
        router = AstraRouter(providers=[text, image])
        rr = router.route_request(image_request("akta chobi banao"))
        self.assertTrue(rr.ok, rr.error)
        self.assertEqual(rr.provider, "bedrock")
        self.assertEqual(rr.model, IMAGE_MODEL)
        self.assertEqual(image.image_prompts, ["akta chobi banao"])
        self.assertTrue(rr.text.startswith("data:image/png;base64,"))
        self.assertEqual(text.chat_calls, [], "text model must never be asked")

    def test_no_image_capable_provider_fails_instead_of_using_text(self):
        text = _FakeAdapter("groq", [TEXT_MODEL])
        router = AstraRouter(providers=[text])
        rr = router.route_request(image_request("photo create koro"))
        self.assertFalse(rr.ok)
        self.assertIn("no eligible", rr.error)
        self.assertEqual(text.chat_calls, [])

    def test_adapter_without_generate_image_is_rejected(self):
        # Allow-listed provider name, image-capable model metadata, but NO
        # generate_image() implementation: the adapter half of the gate must
        # reject it rather than dispatch and fail mid-call.
        no_method = _FakeAdapter("bedrock", [IMAGE_MODEL])
        router = AstraRouter(providers=[no_method])
        rr = router.route_request(image_request("akta chobi banao"))
        self.assertFalse(rr.ok)
        self.assertIn("no eligible", rr.error)

    def test_provider_not_declared_for_images_is_rejected(self):
        # A model whose metadata claims image output, on a provider whose
        # adapter is not declared image-capable (Groq has no image endpoint).
        wrong = _FakeImageAdapter("groq", [IMAGE_MODEL])
        router = AstraRouter(providers=[wrong])
        rr = router.route_request(image_request("akta chobi banao"))
        self.assertFalse(rr.ok)
        self.assertIn("no eligible", rr.error)
        self.assertEqual(wrong.image_prompts, [])

    def test_preferred_text_model_is_not_used_for_an_image_request(self):
        text = _FakeAdapter("groq", [TEXT_MODEL])
        image = _FakeImageAdapter("bedrock", [IMAGE_MODEL])
        router = AstraRouter(providers=[text, image])
        rr = router.route_request(image_request(
            "akta chobi banao", preferred_provider="groq",
            preferred_model=TEXT_MODEL))
        self.assertTrue(rr.ok, rr.error)
        self.assertEqual(rr.provider, "bedrock")

    def test_explicit_image_model_preference_is_honoured(self):
        other = "titan-image-v1"
        image = _FakeImageAdapter("bedrock", [IMAGE_MODEL, other])
        router = AstraRouter(providers=[image])
        rr = router.route_request(image_request(
            "akta chobi banao", preferred_provider="bedrock",
            preferred_model=other))
        self.assertTrue(rr.ok, rr.error)
        self.assertEqual(rr.model, other)
        self.assertEqual(image.image_models, [other])

    def test_text_chat_still_routes_to_the_text_model(self):
        text = _FakeAdapter("groq", [TEXT_MODEL], replies=["hi there"])
        router = AstraRouter(providers=[text])
        rr = router.route_request(RoutingRequest(
            task_type="simple_chat",
            messages=[{"role": "user", "content": "hello"}]))
        self.assertTrue(rr.ok, rr.error)
        self.assertEqual(rr.provider, "groq")
        self.assertEqual(rr.text, "hi there")
        self.assertEqual(len(text.chat_calls), 1)

    def test_capability_helpers_gate_on_adapter_and_method(self):
        from astra.ai.capabilities import (adapter_can_generate,
                                           adapter_supports_output,
                                           filter_candidates_by_output_modalities)
        from astra.ai.models import Model
        self.assertTrue(adapter_supports_output("bedrock", "image"))
        self.assertFalse(adapter_supports_output("groq", "image"))
        self.assertTrue(adapter_can_generate(_FakeImageAdapter("bedrock", []), "image"))
        self.assertFalse(adapter_can_generate(_FakeAdapter("bedrock", []), "image"))
        self.assertFalse(adapter_can_generate(_FakeImageAdapter("groq", []), "image"))
        self.assertTrue(adapter_can_generate(_FakeAdapter("groq", []), "text"))
        model = Model("groq", TEXT_MODEL)
        pairs = [(a, model) for a in (_FakeImageAdapter("bedrock", []),
                                      _FakeImageAdapter("groq", []))]
        kept = filter_candidates_by_output_modalities(pairs, ["image"])
        self.assertEqual([a.name for a, _ in kept], ["bedrock"])
        self.assertEqual(filter_candidates_by_output_modalities(pairs, []), pairs)


class TestChatPipelineImageFlow(unittest.TestCase):
    """request -> route -> generate_image -> artifact -> chat reply."""

    def _pipeline(self, gw_replies, adapters, **kw):
        gw = _FakeGateway(gw_replies)
        router = AstraRouter(providers=list(adapters))
        return ChatPipeline(gw, router, **kw), gw, router

    def test_image_request_returns_an_artifact_not_base64_text(self):
        image = _FakeImageAdapter("bedrock", [IMAGE_MODEL])
        text = _FakeAdapter("groq", [TEXT_MODEL])
        pipe, gw, router = self._pipeline(
            # The Gateway assigns a TEXT model — routing must still land on
            # the image-capable provider.
            [understand("Generate an image of a cyberpunk city",
                        provider="groq", model=TEXT_MODEL)],
            [text, image])
        out = pipe.run("akta chobi banao")

        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["served_by"], f"bedrock/{IMAGE_MODEL}")
        self.assertEqual(len(image.image_prompts), 1)

        arts = out.get("artifacts") or []
        self.assertEqual(len(arts), 1)
        art = arts[0]
        self.assertEqual(art["artifact_type"], "image")
        self.assertEqual(art["mime_type"], "image/png")
        self.assertTrue(art["validated"])
        # It is a real stored artifact under the existing artifact dir …
        path = os.path.join(tempfile.gettempdir(), "astra", "artifacts",
                            f'{art["id"]}_{art["filename"]}')
        self.assertTrue(os.path.isfile(path))
        # … and it validates as an image through the existing validator.
        a = Artifact(id=art["id"], filename=art["filename"],
                     mime_type=art["mime_type"], artifact_type="image",
                     storage_path=path)
        from astra.core.artifacts import validate_artifact
        self.assertEqual(validate_artifact(a), (True, ""))

        # The reply bubble carries a short line, never the base64 payload.
        self.assertNotIn("base64", out["reply"])
        self.assertNotIn("data:image", out["reply"])
        self.assertLess(len(out["reply"]), 200)
        # No text verification of an image payload.
        self.assertEqual(gw.supervised, [])
        self.assertEqual(out["data"]["verification"]["status"], "not_applicable")
        self.assertEqual(text.chat_calls, [])

    def test_bangla_and_banglish_requests_carry_the_image_requirement(self):
        for msg in ("akta chobi banao", "photo create koro",
                    "একটা ছবি বানাও"):
            image = _FakeImageAdapter("bedrock", [IMAGE_MODEL])
            pipe, gw, router = self._pipeline(
                [understand("Generate an image", provider="groq",
                            model=TEXT_MODEL)],
                [_FakeAdapter("groq", [TEXT_MODEL]), image])
            out = pipe.run(msg)
            self.assertTrue(out["ok"], (msg, out))
            self.assertEqual(out["data"]["served_by"], f"bedrock/{IMAGE_MODEL}")
            self.assertEqual(len(out.get("artifacts") or []), 1, msg)

    def test_no_image_model_reports_honestly_and_never_answers_with_text(self):
        text = _FakeAdapter("groq", [TEXT_MODEL], replies=["Sure! Here is how..."])
        pipe, gw, router = self._pipeline(
            [understand("Generate an image", provider="groq", model=TEXT_MODEL)],
            [text])
        out = pipe.run("akta chobi banao")
        self.assertFalse(out["ok"])
        self.assertNotIn("artifacts", out)
        self.assertIn("image", out["reply"].lower())
        self.assertEqual(text.chat_calls, [])

    def test_tool_loop_is_bypassed_for_image_generation(self):
        image = _FakeImageAdapter("bedrock", [IMAGE_MODEL])
        pipe, gw, router = self._pipeline(
            [understand("Generate an image", provider="groq", model=TEXT_MODEL)],
            [_FakeAdapter("groq", [TEXT_MODEL]), image],
            registry=_FakeRegistry())
        self.assertTrue(pipe._tool_loop_usable())
        out = pipe.run("akta chobi banao")
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(image.image_prompts), 1,
                         "the image went through generate_image, not the loop")
        self.assertEqual(len(out.get("artifacts") or []), 1)

    def test_image_works_without_a_gateway(self):
        # No Gateway key configured: the raw message is used as-is and the
        # image must still be generated, artifact-wrapped and served — and
        # the reply must NOT carry the text-path "no gateway" caveat.
        image = _FakeImageAdapter("bedrock", [IMAGE_MODEL])
        text = _FakeAdapter("groq", [TEXT_MODEL])
        gw = _FakeGateway([], usable=False)
        pipe = ChatPipeline(gw, AstraRouter(providers=[text, image]))
        out = pipe.run("akta chobi banao")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["served_by"], f"bedrock/{IMAGE_MODEL}")
        self.assertEqual(len(out.get("artifacts") or []), 1)
        self.assertNotIn("gateway", out["reply"].lower())
        self.assertEqual(gw.prompts, [], "no Gateway call was possible")

    def test_normal_text_chat_is_unaffected(self):
        text = _FakeAdapter("groq", [TEXT_MODEL], replies=["def add(a, b): ..."])
        pipe, gw, router = self._pipeline(
            [understand("Write a Python function", provider="groq",
                        model=TEXT_MODEL),
             verdict("complete")],
            [text])
        out = pipe.run("Python code likhe dao")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["reply"], "def add(a, b): ...")
        self.assertNotIn("artifacts", out)
        self.assertEqual(out["data"]["served_by"], f"groq/{TEXT_MODEL}")


class TestImageArtifactOverHttp(unittest.TestCase):
    """The full wire path: POST /api/chat -> ... -> GET the served artifact.

    This is the chain the Assistant Chat UI actually uses: the reply carries
    the artifact dict, and `renderArtifact()` points at the
    `/api/v1/artifacts/{id}/{filename}` route below.
    """

    def test_generated_image_is_served_from_the_artifact_route(self):
        import urllib.request

        from astra.agent import Agent
        from tests.helpers import LiveServer, make_stack

        stack = make_stack()
        gw = _FakeGateway([understand("Generate an image of a cyberpunk city",
                                      provider="groq", model=TEXT_MODEL)])
        router = AstraRouter(providers=[_FakeAdapter("groq", [TEXT_MODEL]),
                                        _FakeImageAdapter("bedrock", [IMAGE_MODEL])])
        stack["agent"] = Agent(pipeline=ChatPipeline(gw, router))
        srv = LiveServer(stack=stack)
        self.addCleanup(srv.stop)

        req = urllib.request.Request(
            f"{srv.base}/api/chat",
            data=json.dumps({"message": "akta chobi banao"}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read())
        self.assertTrue(body["ok"], body)
        reply = body["data"]
        self.assertNotIn("base64", reply["reply"])
        arts = reply["artifacts"]
        self.assertEqual(len(arts), 1)
        art = arts[0]
        self.assertEqual(art["artifact_type"], "image")

        url = f'{srv.base}/api/v1/artifacts/{art["id"]}/{art["filename"]}'
        with urllib.request.urlopen(url, timeout=20) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type", "")
        self.assertEqual(raw, PNG_BYTES)
        self.assertTrue(raw.startswith(b"\x89PNG"))
        self.assertIn("image/png", ctype)


if __name__ == "__main__":
    unittest.main()
