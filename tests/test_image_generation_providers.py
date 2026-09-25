"""Real image-generation providers, end to end.

The Gateway's routing decision can only be honoured if a provider that
*genuinely* generates images is configured: a model whose adapter really
implements an image API must be in the catalog, carry the `image` capability,
and be selected for `image_generation` — and a text/vision model must never
be, because `vision` is image *understanding*, not generation.

    image_generation
      -> the router's image gate (capability + adapter)
      -> CloudflareAdapter.generate_image (real Workers AI /ai/run API)
      -> data URI -> stored artifact -> Chat image card

The HTTP stub stands in for Cloudflare's API only; the adapter, metadata,
capability gate, router, artifact pipeline and ChatPipeline are real.
"""
import base64
import json
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from astra.ai.adapters.cloudflare import CloudflareAdapter, _image_payload
from astra.ai.capabilities import (adapter_can_generate, adapter_supports_output,
                                   filter_candidates_by_output_modalities)
from astra.ai.models import ModelRegistry, metadata_for
from astra.ai.router import AstraRouter, RoutingRequest

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
SD_MODEL = "@cf/stabilityai/stable-diffusion-xl-base-1.0"
FLUX_MODEL = "@cf/black-forest-labs/flux-1-schnell"
LUCID_MODEL = "@cf/leonardo/lucid-origin"
# Every id in this tuple has a REAL image API behind it; the router
# may pick any of them for image_generation (scoring decides which).
IMAGE_MODELS = (SD_MODEL, FLUX_MODEL, LUCID_MODEL)
CHAT_MODEL = "@cf/qwen/qwen3.8-27b"
VISION_MODEL = "@cf/meta/llama-4-scout-17b-16e-instruct"
BEDROCK_IMAGE_MODEL = "stability.stable-diffusion-xl-v1"


class _Cfg:
    """Minimal Config stand-in (get / getlist) for adapter + registry wiring."""

    def __init__(self, **kw):
        self._d = dict(kw)

    def get(self, key, default=None):
        return self._d.get(key, default)

    def getlist(self, key, default=None):
        v = self._d.get(key)
        if v is None:
            return list(default or [])
        if isinstance(v, (list, tuple)):
            return list(v)
        return [x.strip() for x in str(v).split(",") if x.strip()]


class _CfStub(BaseHTTPRequestHandler):
    """Stands in for the Cloudflare API (chat completions + /ai/run)."""

    requests = []
    image_mode = "binary"          # "binary" | "json"
    fail_code = 0

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            body = {}
        type(self).requests.append({
            "path": self.path,
            "auth": self.headers.get("authorization") or "",
            "body": body,
        })
        if type(self).fail_code:
            self.send_response(type(self).fail_code)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"success":false,"errors":[{"code":1}]}')
            return
        if self.path.endswith("/chat/completions"):
            payload = json.dumps({
                "choices": [{"message": {"content": "hello from cloudflare"}}],
            }).encode()
            ctype = "application/json"
        elif type(self).image_mode == "json":
            payload = json.dumps({
                "success": True,
                "result": {"image": base64.b64encode(PNG_BYTES).decode("ascii")},
            }).encode()
            ctype = "application/json"
        else:
            payload, ctype = PNG_BYTES, "image/png"
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):        # keep the test output clean
        pass


class _StubCf:
    """A live Cloudflare stub server on an ephemeral port."""

    def __init__(self):
        _CfStub.requests = []
        _CfStub.image_mode = "binary"
        _CfStub.fail_code = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _CfStub)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def adapter(self, models=(CHAT_MODEL,), **kw):
        return CloudflareAdapter(config=_Cfg(
            CLOUDFLARE_API_KEYS="test-token",
            CLOUDFLARE_ACCOUNT_IDS="acct-1",
            CLOUDFLARE_MODELS=",".join(models),
            CLOUDFLARE_BASE_URL=self.base_url, **kw))


class TestImageModelMetadata(unittest.TestCase):
    """Only genuinely image-GENERATING models get the image capability."""

    def test_cloudflare_image_models_declare_image_output(self):
        for mid in (SD_MODEL, FLUX_MODEL, "@cf/leonardo/lucid-origin"):
            meta = metadata_for(mid, "cloudflare")
            self.assertIn("image", meta["capabilities"], mid)
            self.assertIn("image", meta["output_modalities"], mid)
            # ... and image generation is NOT inferred from vision.
            self.assertNotIn("vision", meta["capabilities"], mid)
            self.assertFalse(meta["supports_vision"], mid)

    def test_bedrock_image_models_declare_image_output(self):
        for mid in (BEDROCK_IMAGE_MODEL, "amazon.titan-image-generator-v2:0"):
            meta = metadata_for(mid, "bedrock")
            self.assertIn("image", meta["capabilities"], mid)
            self.assertIn("image", meta["output_modalities"], mid)
            self.assertNotIn("vision", meta["capabilities"], mid)

    def test_vision_models_are_not_image_generators(self):
        for mid in (VISION_MODEL, "gemini-2.5-flash", "pixtral-large-latest"):
            meta = metadata_for(mid, "cloudflare")
            self.assertNotIn("image", meta["capabilities"], mid)
            self.assertNotIn("image", meta["output_modalities"], mid)

    def test_text_models_are_not_image_generators(self):
        meta = metadata_for("llama-3.1-70b", "groq")
        self.assertNotIn("image", meta["capabilities"])
        self.assertNotIn("image", meta["output_modalities"])


class TestAdapterImageCapability(unittest.TestCase):
    def test_cloudflare_declares_a_real_image_api(self):
        self.assertTrue(adapter_supports_output("cloudflare", "image"))
        self.assertTrue(adapter_supports_output("bedrock", "image"))
        self.assertFalse(adapter_supports_output("groq", "image"))
        self.assertFalse(adapter_supports_output("gemini", "image"))

    def test_adapter_gate_requires_declaration_and_a_real_method(self):
        stub = _StubCf()
        self.addCleanup(stub.stop)
        self.assertTrue(adapter_can_generate(stub.adapter(), "image"))
        # A provider whose adapter has no image API is never selected.
        self.assertFalse(adapter_can_generate(_NoImageAdapter(), "image"))

    def test_candidate_filter_keeps_only_real_image_providers(self):
        stub = _StubCf()
        self.addCleanup(stub.stop)
        pairs = [(stub.adapter(), object()), (_NoImageAdapter(), object())]
        kept = filter_candidates_by_output_modalities(pairs, ["image"])
        self.assertEqual([a.name for a, _ in kept], ["cloudflare"])


class _NoImageAdapter:
    """A provider adapter with no image API at all."""

    name = "groq"

    def health_check(self):
        return True


class TestImageModelsAreExposedWhenConfigured(unittest.TestCase):
    def test_configured_provider_exposes_its_image_models(self):
        reg = ModelRegistry(_Cfg(CLOUDFLARE_MODELS=f"{CHAT_MODEL},{VISION_MODEL}"))
        ids = {m.model_id for m in reg.for_provider("cloudflare")}
        self.assertIn(CHAT_MODEL, ids)
        self.assertIn(SD_MODEL, ids, "real Workers AI image model must be listed")
        self.assertIn(FLUX_MODEL, ids)
        sd = reg.get("cloudflare", SD_MODEL)
        self.assertIn("image", sd.capabilities)
        self.assertIn("image", sd.output_modalities)

    def test_unconfigured_provider_gains_no_phantom_image_models(self):
        reg = ModelRegistry(_Cfg())
        self.assertEqual(reg.for_provider("cloudflare"), [])
        self.assertEqual(reg.all_models(), [])


class TestCloudflareImageApi(unittest.TestCase):
    """The adapter really speaks Workers AI's image API."""

    def setUp(self):
        self.stub = _StubCf()
        self.addCleanup(self.stub.stop)

    def test_generate_image_posts_to_ai_run_and_returns_a_data_uri(self):
        adapter = self.stub.adapter()
        out = adapter.generate_image("a cyberpunk city", model=SD_MODEL)
        self.assertTrue(out.startswith("data:image/png;base64,"), out[:40])
        payload = base64.b64decode(out.split(",", 1)[1])
        self.assertEqual(payload, PNG_BYTES)

        req = _CfStub.requests[-1]
        self.assertEqual(req["path"], f"/accounts/acct-1/ai/run/{SD_MODEL}")
        self.assertEqual(req["body"], {"prompt": "a cyberpunk city"})
        self.assertEqual(req["auth"], "Bearer test-token")

    def test_json_envelope_response_is_normalised(self):
        _CfStub.image_mode = "json"
        adapter = self.stub.adapter()
        out = adapter.generate_image("a cat", model=FLUX_MODEL)
        self.assertTrue(out.startswith("data:image/png;base64,"), out[:40])
        self.assertEqual(base64.b64decode(out.split(",", 1)[1]), PNG_BYTES)

    def test_provider_error_has_no_hidden_image(self):
        _CfStub.fail_code = 500
        adapter = self.stub.adapter()
        from astra.core.exceptions import ProviderError
        with self.assertRaises(ProviderError):
            adapter.generate_image("x", model=SD_MODEL)

    def test_payload_helper_rejects_an_empty_response(self):
        self.assertEqual(_image_payload(b"", "image/png"), ("", ""))
        self.assertEqual(_image_payload(b'{"success": false}', "application/json"),
                         ("", ""))
        self.assertEqual(_image_payload(b'{"result": {}}', "application/json"),
                         ("", ""))


class TestRouterSelectsRealImageModel(unittest.TestCase):
    """image_generation lands on a real image model — never a text/vision one."""

    def setUp(self):
        self.stub = _StubCf()
        self.addCleanup(self.stub.stop)
        self.adapter = self.stub.adapter(
            models=(CHAT_MODEL, VISION_MODEL, SD_MODEL))

    def _image_request(self, message="akta chobi banao"):
        return RoutingRequest(task_type="image_generation",
                              messages=[{"role": "user", "content": message}],
                              required_output_modalities=["image"])

    def test_image_request_selects_the_image_model(self):
        router = AstraRouter(providers=[self.adapter])
        rr = router.route_request(self._image_request())
        self.assertTrue(rr.ok, rr.error)
        self.assertEqual(rr.provider, "cloudflare")
        self.assertIn(rr.model, IMAGE_MODELS)
        self.assertTrue(rr.text.startswith("data:image/png;base64,"))

    def test_text_and_vision_models_are_never_chosen(self):
        router = AstraRouter(providers=[self.adapter])
        rr = router.route_request(self._image_request())
        self.assertNotIn(rr.model, (CHAT_MODEL, VISION_MODEL))
        # The prompt reaches the image API verbatim.
        self.assertEqual(_CfStub.requests[-1]["body"]["prompt"],
                         "akta chobi banao")

    def test_task_type_alone_is_enough_to_require_an_image_model(self):
        # No required_output_modalities set by the caller: the router must
        # still enforce the image requirement from the task type.
        router = AstraRouter(providers=[self.adapter])
        req = RoutingRequest(task_type="image_generation",
                             messages=[{"role": "user", "content": "draw a cat"}],
                             preferred_provider="cloudflare",
                             preferred_model=CHAT_MODEL)
        rr = router.route_request(req)
        self.assertTrue(rr.ok, rr.error)
        self.assertIn(rr.model, IMAGE_MODELS)

    def test_explicit_image_model_preference_is_honoured(self):
        # An explicit preferred image model must win among the image models.
        router = AstraRouter(providers=[self.adapter])
        req = RoutingRequest(task_type="image_generation",
                             messages=[{"role": "user", "content": "draw a cat"}],
                             preferred_provider="cloudflare",
                             preferred_model=SD_MODEL)
        rr = router.route_request(req)
        self.assertTrue(rr.ok, rr.error)
        self.assertEqual(rr.model, SD_MODEL)

    def test_chat_request_does_not_land_on_a_diffusion_model(self):
        router = AstraRouter(providers=[self.adapter])
        rr = router.route_request(RoutingRequest(
            task_type="simple_chat",
            messages=[{"role": "user", "content": "hello"}]))
        self.assertTrue(rr.ok, rr.error)
        self.assertEqual(rr.model, CHAT_MODEL)

    def test_no_image_provider_fails_precisely(self):
        from astra.ai.adapters.gemini import GeminiAdapter
        vision_only = GeminiAdapter(config=_Cfg(
            GEMINI_API_KEYS="k", GEMINI_MODELS="gemini-2.5-flash"))
        router = AstraRouter(providers=[vision_only])
        rr = router.route_request(self._image_request())
        self.assertFalse(rr.ok)
        self.assertIn("no eligible", rr.error)


class TestProductionShapedCatalog(unittest.TestCase):
    """Mirrors bootstrap: the adapter and the ModelRegistry are built from the
    SAME config. This is the exact shape of the reported failure — a catalog
    that used to hold only the configured text/vision models must now also
    hold (and route to) a real image model."""

    def test_registry_and_adapter_together_route_to_a_real_image_model(self):
        stub = _StubCf()
        self.addCleanup(stub.stop)
        cfg = _Cfg(CLOUDFLARE_API_KEYS="test-token",
                   CLOUDFLARE_ACCOUNT_IDS="acct-1",
                   CLOUDFLARE_BASE_URL=stub.base_url,
                   CLOUDFLARE_MODELS=f"{CHAT_MODEL},{VISION_MODEL}")
        adapter = CloudflareAdapter(config=cfg)
        registry = ModelRegistry(cfg)

        # The catalog itself now carries the real image models...
        catalog = {m.model_id for m in registry.for_provider("cloudflare")}
        self.assertTrue(set(IMAGE_MODELS).issubset(catalog), catalog)

        router = AstraRouter(providers=[adapter], registry=registry)
        prompt = "ekta futuristic city er chobi banao"
        rr = router.route_request(RoutingRequest(
            task_type="image_generation",
            messages=[{"role": "user", "content": prompt}],
            required_output_modalities=["image"]))
        self.assertTrue(rr.ok, rr.error)
        self.assertEqual(rr.provider, "cloudflare")
        self.assertIn(rr.model, IMAGE_MODELS)
        self.assertTrue(rr.text.startswith("data:image/png;base64,"))
        # The original user prompt reaches the real image API verbatim.
        self.assertEqual(_CfStub.requests[-1]["body"]["prompt"], prompt)

    def test_catalog_never_makes_a_vision_model_an_image_generator(self):
        stub = _StubCf()
        self.addCleanup(stub.stop)
        cfg = _Cfg(CLOUDFLARE_API_KEYS="test-token",
                   CLOUDFLARE_ACCOUNT_IDS="acct-1",
                   CLOUDFLARE_BASE_URL=stub.base_url,
                   CLOUDFLARE_MODELS=VISION_MODEL)
        registry = ModelRegistry(cfg)
        vision = registry.get("cloudflare", VISION_MODEL)
        self.assertNotIn("image", vision.capabilities)
        self.assertNotIn("image", vision.output_modalities)


class TestChatPipelineImageCard(unittest.TestCase):
    """Gateway decision -> real image API -> artifact -> chat reply/UI."""

    def setUp(self):
        self.stub = _StubCf()
        self.addCleanup(self.stub.stop)

    def test_gateway_image_decision_produces_a_stored_image_artifact(self):
        import os
        import tempfile

        from astra.ai.chat_pipeline import ChatPipeline
        from astra.core.artifacts import Artifact, validate_artifact

        adapter = self.stub.adapter(models=(CHAT_MODEL, VISION_MODEL, SD_MODEL))
        gateway = _ScriptedGateway(json.dumps({
            "final_request": "একটা ছবি বানাও", "was_incomplete": False,
            "routing": {"task_type": "image_generation",
                        "output_modalities": ["image"]},
            "provider": "cloudflare", "model": CHAT_MODEL,
            "criteria": ["returns an image"], "reason": "best fit",
        }))
        pipe = ChatPipeline(gateway, AstraRouter(providers=[adapter]))
        out = pipe.run("একটা ছবি বানাও")

        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["task_type"], "image_generation")
        served_by = out["data"]["served_by"]
        self.assertTrue(served_by.startswith("cloudflare/"), served_by)
        self.assertIn(served_by.split("/", 1)[1], IMAGE_MODELS)
        # The Gateway assigned the TEXT model on purpose: capability
        # validation must still land it on the real image model.
        self.assertEqual(_CfStub.requests[-1]["body"]["prompt"], "একটা ছবি বানাও")

        arts = out.get("artifacts") or []
        self.assertEqual(len(arts), 1)
        art = arts[0]
        self.assertEqual(art["artifact_type"], "image")
        self.assertEqual(art["mime_type"], "image/png")
        self.assertTrue(art["validated"])
        path = os.path.join(tempfile.gettempdir(), "astra", "artifacts",
                            f'{art["id"]}_{art["filename"]}')
        self.assertTrue(os.path.isfile(path))
        stored = Artifact(id=art["id"], filename=art["filename"],
                          mime_type=art["mime_type"], artifact_type="image",
                          storage_path=path)
        self.assertEqual(validate_artifact(stored), (True, ""))
        # The reply never carries the base64 payload.
        self.assertNotIn("base64", out["reply"])
        self.assertNotIn("data:image", out["reply"])

    def test_no_image_provider_returns_the_precise_message(self):
        from astra.ai.chat_pipeline import ChatPipeline
        from astra.ai.adapters.gemini import GeminiAdapter

        vision_only = GeminiAdapter(config=_Cfg(
            GEMINI_API_KEYS="k", GEMINI_MODELS="gemini-2.5-flash"))
        gateway = _ScriptedGateway(json.dumps({
            "final_request": "akta chobi banao", "was_incomplete": False,
            "routing": {"task_type": "image_generation",
                        "output_modalities": ["image"]},
            "provider": "", "model": "", "criteria": [], "reason": "fit",
        }))
        pipe = ChatPipeline(gateway, AstraRouter(providers=[vision_only]))
        out = pipe.run("akta chobi banao")
        self.assertFalse(out["ok"], out)
        self.assertNotIn("artifacts", out)
        self.assertIn("No image-generation provider configured", out["reply"])


class _ScriptedGateway:
    def __init__(self, understand_reply):
        self.reply = understand_reply

    def is_usable(self):
        return True

    def chat(self, messages, model=None, max_tokens=500, category=None,
             trace=""):
        return self.reply


class TestImageArtifactOverHttp(unittest.TestCase):
    """The generated image is served on the route renderArtifact() uses."""

    def test_generated_image_is_served_from_the_artifact_route(self):
        from astra.ai.chat_pipeline import ChatPipeline
        from astra.agent import Agent
        from tests.helpers import LiveServer, make_stack

        stub = _StubCf()
        self.addCleanup(stub.stop)
        adapter = stub.adapter(models=(CHAT_MODEL, SD_MODEL))
        gateway = _ScriptedGateway(json.dumps({
            "final_request": "akta chobi banao", "was_incomplete": False,
            "routing": {"task_type": "image_generation",
                        "output_modalities": ["image"]},
            "provider": "", "model": "", "criteria": [], "reason": "fit",
        }))
        stack = make_stack()
        stack["agent"] = Agent(pipeline=ChatPipeline(
            gateway, AstraRouter(providers=[adapter])))
        srv = LiveServer(stack=stack)
        self.addCleanup(srv.stop)

        req = urllib.request.Request(
            f"{srv.base}/api/chat",
            data=json.dumps({"message": "akta chobi banao"}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read())
        self.assertTrue(body["ok"], body)
        arts = body["data"]["artifacts"]
        self.assertEqual(len(arts), 1)
        art = arts[0]
        self.assertEqual(art["artifact_type"], "image")
        self.assertEqual(art["mime_type"], "image/png")

        url = f'{srv.base}/api/v1/artifacts/{art["id"]}/{art["filename"]}'
        with urllib.request.urlopen(url, timeout=20) as r:
            self.assertEqual(r.headers.get("content-type"), "image/png")
            self.assertEqual(r.read(), PNG_BYTES)


if __name__ == "__main__":
    unittest.main()
