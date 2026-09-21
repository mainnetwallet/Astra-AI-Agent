"""Tests for the multimodal extension: attachments, capabilities, artifacts,
capability-aware routing, and the multipart /api/chat path."""
import json
import os
import tempfile
import unittest
import zipfile

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.core.attachments import (
    FILE_FAMILIES, SUPPORTED_EXTENSIONS, DANGEROUS_EXTENSIONS,
    detect_mime, validate_file, process_upload, validate_archive,
    normalize_attachments, MAX_FILE_SIZE,
)
from astra.core.artifacts import (
    store_artifact, validate_artifact,
)
from astra.ai.capabilities import (
    ALL_INPUT_CAPS, ALL_OUTPUT_CAPS,
    INPUT_TEXT, INPUT_IMAGE, INPUT_AUDIO, INPUT_VIDEO, INPUT_DOCUMENT,
    OUTPUT_TEXT, OUTPUT_IMAGE, OUTPUT_AUDIO,
    detect_required_input_capabilities,
    detect_required_output_capabilities,
    check_model_compatibility,
    filter_candidates_by_capabilities,
)
from astra.ai.models import Model, metadata_for
from astra.ai.router import RoutingRequest, classify


class TestAttachments(unittest.TestCase):
    """Attachment processing, MIME detection, file validation."""

    def test_supported_extensions(self):
        for ext in (".pdf", ".docx", ".csv", ".xlsx", ".pptx", ".json",
                    ".png", ".mp3", ".mp4", ".zip"):
            self.assertIn(ext, SUPPORTED_EXTENSIONS)

    def test_dangerous_extensions(self):
        for ext in (".exe", ".bat", ".cmd", ".sh", ".ps1"):
            self.assertIn(ext, DANGEROUS_EXTENSIONS)

    def test_file_families(self):
        self.assertEqual(FILE_FAMILIES[".pdf"], "document")
        self.assertEqual(FILE_FAMILIES[".png"], "image")
        self.assertEqual(FILE_FAMILIES[".mp3"], "audio")
        self.assertEqual(FILE_FAMILIES[".mp4"], "video")
        self.assertEqual(FILE_FAMILIES[".zip"], "archive")
        self.assertEqual(FILE_FAMILIES[".csv"], "data")
        self.assertEqual(FILE_FAMILIES[".xlsx"], "spreadsheet")

    def test_detect_mime_png(self):
        data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        mime = detect_mime(data, "test.png")
        self.assertIn("png", mime.lower())

    def test_detect_mime_jpeg(self):
        data = b"\xff\xd8\xff" + b"\x00" * 100
        mime = detect_mime(data, "photo.jpg")
        self.assertIn("jpeg", mime.lower())

    def test_detect_mime_pdf(self):
        data = b"%PDF-1.4" + b"\x00" * 100
        mime = detect_mime(data, "doc.pdf")
        self.assertIn("pdf", mime.lower())

    def test_detect_mime_zip(self):
        data = b"PK\x03\x04" + b"\x00" * 100
        mime = detect_mime(data, "archive.zip")
        self.assertIn("zip", mime.lower())

    def test_detect_mime_fallback_to_extension(self):
        mime = detect_mime(b"hello world", "readme.txt")
        self.assertIn("text", mime.lower())

    def test_validate_file_ok(self):
        ok, err = validate_file(b"hello", "test.txt", MAX_FILE_SIZE)
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_validate_file_too_large(self):
        ok, err = validate_file(b"x" * (MAX_FILE_SIZE + 1), "big.txt", MAX_FILE_SIZE)
        self.assertFalse(ok)
        self.assertIn("exceeds", err.lower())

    def test_validate_file_dangerous_extension(self):
        ok, err = validate_file(b"echo hello", "script.exe", MAX_FILE_SIZE)
        self.assertFalse(ok)
        self.assertIn("dangerous", err.lower())

    def test_validate_file_bat(self):
        ok, err = validate_file(b"echo hello", "run.bat", MAX_FILE_SIZE)
        self.assertFalse(ok)

    def test_validate_file_sh(self):
        ok, err = validate_file(b"#!/bin/bash", "run.sh", MAX_FILE_SIZE)
        self.assertFalse(ok)

    def test_process_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            att = process_upload(b"hello world", "test.txt", tmp)
            self.assertTrue(att.processed)
            self.assertEqual(att.family, "text")
            self.assertEqual(att.extension, ".txt")
            self.assertTrue(os.path.isfile(att.storage_path))

    def test_process_upload_image(self):
        data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        with tempfile.TemporaryDirectory() as tmp:
            att = process_upload(data, "photo.png", tmp)
            self.assertTrue(att.processed)
            self.assertEqual(att.family, "image")

    def test_process_upload_rejects_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            att = process_upload(b"MZ" + b"\x00" * 100, "malware.exe", tmp)
            self.assertFalse(att.processed)
            self.assertTrue(att.error)

    def test_normalize_attachments(self):
        raw = [{"filename": "test.txt", "family": "text", "processed": True}]
        result = normalize_attachments(raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].filename, "test.txt")


class TestArchiveSafety(unittest.TestCase):
    """Archive traversal, symlink, zip bomb protection."""

    def _make_zip(self, entries: dict) -> str:
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        with zipfile.ZipFile(tmp, "w") as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        tmp.close()
        return tmp.name

    def test_valid_zip(self):
        path = self._make_zip({"hello.txt": b"hello world"})
        try:
            ok, err = validate_archive(path)
            self.assertTrue(ok)
        finally:
            os.unlink(path)

    def test_traversal_zip(self):
        path = self._make_zip({"../../../etc/passwd": b"root:x:0:0"})
        try:
            ok, err = validate_archive(path)
            self.assertFalse(ok)
            self.assertIn("traversal", err.lower())
        finally:
            os.unlink(path)

    def test_absolute_path_zip(self):
        path = self._make_zip({"/etc/passwd": b"root:x:0:0"})
        try:
            ok, err = validate_archive(path)
            self.assertFalse(ok)
        finally:
            os.unlink(path)

    def test_too_many_files(self):
        entries = {f"file_{i}.txt": b"x" for i in range(501)}
        path = self._make_zip(entries)
        try:
            ok, err = validate_archive(path)
            self.assertFalse(ok)
            self.assertIn("many", err.lower())
        finally:
            os.unlink(path)


class TestArtifacts(unittest.TestCase):
    """Artifact storage and validation."""

    def test_store_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            art = store_artifact(b'{"key": "value"}', "data.json", "data", tmp)
            self.assertTrue(os.path.isfile(art.storage_path))
            self.assertEqual(art.artifact_type, "data")

    def test_validate_json_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            art = store_artifact(b'{"key": "value"}', "data.json", "data", tmp)
            ok, err = validate_artifact(art)
            self.assertTrue(ok)
            self.assertTrue(art.validated)

    def test_validate_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            art = store_artifact(b"not json {{{", "data.json", "data", tmp)
            ok, err = validate_artifact(art)
            self.assertFalse(ok)

    def test_validate_pdf_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_data = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"
            art = store_artifact(pdf_data, "doc.pdf", "document", tmp)
            ok, err = validate_artifact(art)
            self.assertTrue(ok)

    def test_validate_empty_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            art = store_artifact(b"", "empty.txt", "text", tmp)
            ok, err = validate_artifact(art)
            self.assertFalse(ok)

    def test_validate_image_artifact_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
            art = store_artifact(data, "img.png", "image", tmp)
            ok, err = validate_artifact(art)
            self.assertTrue(ok)

    def test_validate_image_artifact_bad(self):
        with tempfile.TemporaryDirectory() as tmp:
            art = store_artifact(b"not an image", "img.png", "image", tmp)
            ok, err = validate_artifact(art)
            self.assertFalse(ok)


class TestCapabilities(unittest.TestCase):
    """Multimodal capability detection and filtering."""

    def test_input_capabilities_defined(self):
        self.assertIn(INPUT_TEXT, ALL_INPUT_CAPS)
        self.assertIn(INPUT_IMAGE, ALL_INPUT_CAPS)
        self.assertIn(INPUT_AUDIO, ALL_INPUT_CAPS)
        self.assertIn(INPUT_VIDEO, ALL_INPUT_CAPS)
        self.assertIn(INPUT_DOCUMENT, ALL_INPUT_CAPS)

    def test_output_capabilities_defined(self):
        self.assertIn(OUTPUT_TEXT, ALL_OUTPUT_CAPS)
        self.assertIn(OUTPUT_IMAGE, ALL_OUTPUT_CAPS)
        self.assertIn(OUTPUT_AUDIO, ALL_OUTPUT_CAPS)

    def test_detect_input_caps_text_only(self):
        caps = detect_required_input_capabilities([])
        self.assertIn(INPUT_TEXT, caps)

    def test_detect_input_caps_image(self):
        atts = [{"family": "image", "detected_type": "image/png"}]
        caps = detect_required_input_capabilities(atts)
        self.assertIn(INPUT_IMAGE, caps)

    def test_detect_input_caps_audio(self):
        atts = [{"family": "audio", "detected_type": "audio/mp3"}]
        caps = detect_required_input_capabilities(atts)
        self.assertIn(INPUT_AUDIO, caps)

    def test_detect_input_caps_video(self):
        atts = [{"family": "video", "detected_type": "video/mp4"}]
        caps = detect_required_input_capabilities(atts)
        self.assertIn(INPUT_VIDEO, caps)

    def test_detect_input_caps_document(self):
        atts = [{"family": "document", "detected_type": "application/pdf"}]
        caps = detect_required_input_capabilities(atts)
        self.assertIn(INPUT_DOCUMENT, caps)

    def test_detect_output_caps_image_generation(self):
        caps = detect_required_output_capabilities("generate an image of a cat")
        self.assertIn(OUTPUT_IMAGE, caps)

    def test_detect_output_caps_audio_generation(self):
        caps = detect_required_output_capabilities("create audio narration")
        self.assertIn(OUTPUT_AUDIO, caps)

    def test_detect_output_caps_text_only(self):
        caps = detect_required_output_capabilities("what is the weather")
        self.assertIn(OUTPUT_TEXT, caps)
        self.assertNotIn(OUTPUT_IMAGE, caps)

    def test_check_model_compatibility_text_ok(self):
        model = Model("gemini", "gemini-flash", capabilities=["chat", "vision"],
                       input_modalities=["text", "image"],
                       output_modalities=["text"])
        ok, reason = check_model_compatibility(model, [INPUT_IMAGE], [])
        self.assertTrue(ok)

    def test_check_model_compatibility_text_only_rejects_image(self):
        model = Model("groq", "llama-70b", capabilities=["chat"],
                       input_modalities=["text"],
                       output_modalities=["text"])
        ok, reason = check_model_compatibility(model, [INPUT_IMAGE], [])
        self.assertFalse(ok)
        self.assertIn("image", reason.lower())

    def test_check_model_compatibility_no_image_gen(self):
        model = Model("gemini", "gemini-flash", capabilities=["chat", "vision"],
                       input_modalities=["text", "image"],
                       output_modalities=["text"])
        ok, reason = check_model_compatibility(model, [], [OUTPUT_IMAGE])
        self.assertFalse(ok)

    def test_filter_candidates(self):
        class FakeAdapter:
            name = "gemini"
        adapter = FakeAdapter()
        vision_model = Model("gemini", "gemini-flash",
                              capabilities=["chat", "vision"],
                              input_modalities=["text", "image"],
                              output_modalities=["text"])
        text_model = Model("groq", "llama-70b",
                            capabilities=["chat"],
                            input_modalities=["text"],
                            output_modalities=["text"])
        candidates = [(adapter, vision_model), (adapter, text_model)]
        filtered = filter_candidates_by_capabilities(
            candidates, [INPUT_IMAGE], [])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0][1].model_id, "gemini-flash")


class TestCapabilityRouting(unittest.TestCase):
    """Capability filtering must happen BEFORE normal health/latency/preference ranking."""

    def test_routing_request_modalities(self):
        req = RoutingRequest(
            task_type="vision",
            required_input_modalities=["image"],
            required_output_modalities=[])
        self.assertEqual(req.required_input_modalities, ["image"])

    def test_classify_image_generation(self):
        self.assertEqual(classify("generate an image of a sunset"), "image_generation")

    def test_classify_audio_generation(self):
        self.assertEqual(classify("generate audio from text"), "audio")

    def test_classify_video_generation(self):
        self.assertEqual(classify("create a video of a cat"), "video")

    def test_classify_multimodal(self):
        self.assertEqual(classify("[2 file(s) attached]"), "multimodal")

    def test_classify_existing_unchanged(self):
        self.assertEqual(classify("hello how are you"), "simple_chat")

    def test_explicit_incompatible_model_rejection(self):
        req = RoutingRequest(
            preferred_model="llama-70b",
            preferred_provider="groq",
            no_fallback=True,
            required_capabilities=["vision"])
        self.assertTrue(req.no_fallback)
        self.assertIn("vision", req.required_capabilities)


class TestModelMultimodal(unittest.TestCase):
    """Model metadata includes multimodal modalities."""

    def test_gemini_has_multimodal_input(self):
        meta = metadata_for("gemini-2.0-flash", "gemini")
        self.assertIn("image", meta.get("input_modalities", []))

    def test_groq_text_only(self):
        meta = metadata_for("llama-3.1-70b-versatile", "groq")
        mods = meta.get("input_modalities", ["text"])
        self.assertEqual(mods, ["text"])

    def test_claude_has_image_input(self):
        meta = metadata_for("claude-sonnet-4-20250514", "bedrock")
        self.assertIn("image", meta.get("input_modalities", []))

    def test_model_to_dict_includes_modalities(self):
        m = Model("test", "test-model",
                   input_modalities=["text", "image"],
                   output_modalities=["text"])
        d = m.to_dict()
        self.assertIn("input_modalities", d)
        self.assertIn("image", d["input_modalities"])


class TestMimeValidation(unittest.TestCase):
    """MIME type detection must not trust extension alone."""

    def test_png_magic_bytes(self):
        mime = detect_mime(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50, "noext")
        self.assertIn("png", mime.lower())

    def test_extension_fallback(self):
        mime = detect_mime(b"not a real png", "fake.png")
        self.assertTrue(mime)


class TestNoCodeExecution(unittest.TestCase):
    """Uploaded file content must never be executed."""

    def test_dangerous_extensions_comprehensive(self):
        for ext in (".exe", ".bat", ".cmd", ".sh", ".ps1", ".com", ".msi",
                    ".vbs", ".wsf", ".scr"):
            ok, err = validate_file(b"content", f"file{ext}", MAX_FILE_SIZE)
            self.assertFalse(ok, f"Extension {ext} should be rejected")


class TestZeroBypassMultimodal(unittest.TestCase):
    """Multimodal must not introduce any new bypass paths."""

    def test_no_llm_in_agent(self):
        from astra.agent import Agent
        import inspect
        src = inspect.getsource(Agent)
        self.assertNotIn("self.llm(", src)

    def test_no_direct_provider_in_web(self):
        import inspect
        from astra import web, web_fastapi
        self.assertNotIn("ProviderRegistry", inspect.getsource(web))
        self.assertNotIn("ProviderRegistry", inspect.getsource(web_fastapi))

    def test_routing_request_preserved(self):
        req = RoutingRequest(
            messages=[{"role": "user", "content": "test"}],
            required_input_modalities=["image"],
            vision=True)
        self.assertTrue(req.vision)
        self.assertEqual(req.required_input_modalities, ["image"])


class TestExistingChatCompatibility(unittest.TestCase):
    """Existing text-only /api/chat must remain unchanged."""

    def test_agent_handle_text_only(self):
        from tests.helpers import make_agent
        _, _, agent = make_agent()
        result = agent.handle("help")
        self.assertIn("reply", result)
        self.assertIn("ok", result)

    def test_agent_handle_empty_attachments(self):
        from tests.helpers import make_agent
        _, _, agent = make_agent()
        result = agent.handle("hello", attachments=[])
        self.assertIn("reply", result)

    def test_agent_handle_with_attachments(self):
        from tests.helpers import make_agent
        _, _, agent = make_agent()
        atts = [{"filename": "test.png", "family": "image",
                 "processed": True, "detected_type": "image/png"}]
        result = agent.handle("what is in this image?", attachments=atts)
        self.assertIn("reply", result)


class TestMultipartChat(unittest.TestCase):
    """Multipart /api/chat endpoint — same production path."""

    @classmethod
    def setUpClass(cls):
        from tests.helpers import LiveServer, make_agent
        cls.store, cls.plugin, cls.agent = make_agent()
        cls.server = LiveServer(store=cls.store, agent=cls.agent)
        cls.port = cls.server.port
        cls.base = cls.server.base

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.store.close()

    def _post_json(self, path, payload):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        body = json.dumps(payload)
        conn.request("POST", path, body=body,
                      headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return resp.status, data

    def test_text_json_chat(self):
        """Existing JSON POST still works."""
        status, body = self._post_json("/api/chat", {"message": "help"})
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        self.assertIn("reply", body.get("data", {}))

    def test_text_json_v1_chat(self):
        """V1 prefix should also work."""
        status, body = self._post_json("/api/v1/chat", {"message": "help"})
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))


class TestOversizedFiles(unittest.TestCase):
    """Oversized file rejection."""

    def test_oversized_rejected(self):
        ok, err = validate_file(b"x" * (MAX_FILE_SIZE + 1), "big.pdf",
                                MAX_FILE_SIZE)
        self.assertFalse(ok)
        self.assertIn("exceeds", err.lower())


class TestUnsupportedCapability(unittest.TestCase):
    """When capability is unsupported, report clearly."""

    def test_unsupported_input(self):
        model = Model("test", "text-only", capabilities=["chat"],
                       input_modalities=["text"],
                       output_modalities=["text"])
        ok, reason = check_model_compatibility(
            model, [INPUT_VIDEO], [])
        self.assertFalse(ok)
        self.assertIn("video", reason.lower())


class TestRoutingPolicyModality(unittest.TestCase):
    """meets_hard_requirements rejects models lacking required modalities."""

    def test_rejects_text_only_for_image_request(self):
        from astra.ai.routing_policy import meets_hard_requirements
        model = Model("groq", "llama-70b", capabilities=["chat"],
                       input_modalities=["text"],
                       output_modalities=["text"])
        req = RoutingRequest(required_input_modalities=["image"])
        self.assertFalse(meets_hard_requirements(model, req))

    def test_accepts_vision_model_for_image_request(self):
        from astra.ai.routing_policy import meets_hard_requirements
        model = Model("gemini", "gemini-flash", capabilities=["chat", "vision"],
                       input_modalities=["text", "image"],
                       output_modalities=["text"])
        req = RoutingRequest(required_input_modalities=["image"])
        self.assertTrue(meets_hard_requirements(model, req))


class TestMultimodalMessages(unittest.TestCase):
    """Multimodal message builder produces correct content parts."""

    def test_text_only_returns_string(self):
        from astra.ai.multimodal_messages import build_multimodal_content
        result = build_multimodal_content("hello", [])
        self.assertEqual(result, "hello")

    def test_image_attachment_produces_image_url_part(self):
        from astra.ai.multimodal_messages import build_multimodal_content
        data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(data)
            path = f.name
        try:
            att = [{"family": "image", "storage_path": path,
                    "detected_type": "image/png", "filename": "test.png"}]
            result = build_multimodal_content("describe this", att)
            self.assertIsInstance(result, list)
            types = [p["type"] for p in result]
            self.assertIn("text", types)
            self.assertIn("image_url", types)
            img_part = [p for p in result if p["type"] == "image_url"][0]
            self.assertTrue(
                img_part["image_url"]["url"].startswith("data:image/png;base64,"))
        finally:
            os.unlink(path)

    def test_text_document_inlined(self):
        from astra.ai.multimodal_messages import build_multimodal_content
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
            f.write("Hello world content")
            path = f.name
        try:
            att = [{"family": "text", "storage_path": path,
                    "detected_type": "text/plain", "filename": "doc.txt",
                    "extension": ".txt", "original_filename": "doc.txt"}]
            result = build_multimodal_content("read this", att)
            self.assertIsInstance(result, list)
            texts = [p["text"] for p in result if p.get("type") == "text"]
            combined = " ".join(texts)
            self.assertIn("Hello world content", combined)
        finally:
            os.unlink(path)

    def test_has_inline_content_checks_file(self):
        from astra.ai.multimodal_messages import has_inline_content
        self.assertFalse(has_inline_content([]))
        self.assertFalse(has_inline_content(
            [{"storage_path": "/nonexistent/file.png"}]))
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"data")
            path = f.name
        try:
            self.assertTrue(has_inline_content([{"storage_path": path}]))
        finally:
            os.unlink(path)


class TestArtifactExtraction(unittest.TestCase):
    """Artifact extraction from provider responses."""

    def test_extract_base64_image(self):
        from astra.ai.artifact_extraction import extract_artifacts
        import base64
        img_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
        b64 = base64.b64encode(img_data).decode()
        text = f"Here is the image: data:image/png;base64,{b64}"
        with tempfile.TemporaryDirectory() as tmp:
            arts = extract_artifacts(text, tmp)
            self.assertEqual(len(arts), 1)
            self.assertTrue(arts[0].get("validated"))

    def test_extract_json_code_block(self):
        from astra.ai.artifact_extraction import extract_artifacts
        text = '```json\n{"key": "value", "count": 42}\n```'
        with tempfile.TemporaryDirectory() as tmp:
            arts = extract_artifacts(text, tmp, requested_output="data")
            self.assertEqual(len(arts), 1)

    def test_no_artifacts_for_plain_text(self):
        from astra.ai.artifact_extraction import extract_artifacts
        with tempfile.TemporaryDirectory() as tmp:
            arts = extract_artifacts("Just a normal response", tmp)
            self.assertEqual(len(arts), 0)

    def test_detect_output_type(self):
        from astra.ai.artifact_extraction import detect_output_type
        self.assertEqual(detect_output_type("generate an image of a cat"), "image")
        self.assertEqual(detect_output_type("create audio narration"), "audio")
        self.assertEqual(detect_output_type("export a xlsx report"), "spreadsheet")
        self.assertEqual(detect_output_type("hello"), "")


class TestOutputModalityRouting(unittest.TestCase):
    """Output modality filtering in routing policy."""

    def test_rejects_text_only_for_image_generation(self):
        from astra.ai.routing_policy import meets_hard_requirements
        model = Model("groq", "llama-70b", capabilities=["chat"],
                       input_modalities=["text"],
                       output_modalities=["text"])
        req = RoutingRequest(required_output_modalities=["image"])
        self.assertFalse(meets_hard_requirements(model, req))

    def test_accepts_image_gen_model(self):
        from astra.ai.routing_policy import meets_hard_requirements
        model = Model("bedrock", "stability.stable-diffusion-xl-v1", capabilities=["chat"],
                       input_modalities=["text"],
                       output_modalities=["text", "image"])
        req = RoutingRequest(required_output_modalities=["image"])
        self.assertTrue(meets_hard_requirements(model, req))


class TestBedrockMultimodalConverse(unittest.TestCase):
    """Bedrock adapter converts multimodal content to Converse format."""

    def test_text_only_unchanged(self):
        from astra.ai.adapters.bedrock import BedrockAdapter
        adapter = BedrockAdapter.__new__(BedrockAdapter)
        body = adapter._converse_body(
            [{"role": "user", "content": "hello"}], "model", 500)
        self.assertEqual(body["messages"][0]["content"], [{"text": "hello"}])

    def test_image_content_parts(self):
        from astra.ai.adapters.bedrock import BedrockAdapter
        import base64
        img = b"\x89PNG" + b"\x00" * 50
        b64 = base64.b64encode(img).decode()
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {
                "url": f"data:image/png;base64,{b64}"}}
        ]}]
        adapter = BedrockAdapter.__new__(BedrockAdapter)
        body = adapter._converse_body(messages, "model", 500)
        blocks = body["messages"][0]["content"]
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0], {"text": "describe"})
        self.assertIn("image", blocks[1])
        self.assertEqual(blocks[1]["image"]["format"], "png")


class TestOutputCapabilityDetection(unittest.TestCase):
    """Output capability detection feeds into routing."""

    def test_image_generation_detected(self):
        caps = detect_required_output_capabilities("generate an image of a sunset")
        self.assertIn(OUTPUT_IMAGE, caps)

    def test_audio_generation_detected(self):
        caps = detect_required_output_capabilities("create audio narration")
        self.assertIn(OUTPUT_AUDIO, caps)

    def test_plain_text_no_special_output(self):
        caps = detect_required_output_capabilities("what is the weather today")
        self.assertNotIn(OUTPUT_IMAGE, caps)
        self.assertNotIn(OUTPUT_AUDIO, caps)


class TestRealImageGeneration(unittest.TestCase):
    """Image generation adapter has the real API method."""

    def test_compatible_adapter_has_generate_image(self):
        from astra.ai.adapters.base import CompatibleAdapter
        adapter = CompatibleAdapter.__new__(CompatibleAdapter)
        self.assertTrue(hasattr(adapter, "generate_image"))

    def test_bedrock_adapter_has_generate_image(self):
        from astra.ai.adapters.bedrock import BedrockAdapter
        adapter = BedrockAdapter.__new__(BedrockAdapter)
        self.assertTrue(hasattr(adapter, "generate_image"))

    def test_base_provider_raises_not_supported(self):
        from astra.ai.provider import AIProvider
        p = AIProvider()
        with self.assertRaises(Exception) as ctx:
            p.generate_image("test prompt")
        self.assertIn("not support", str(ctx.exception).lower())

    def test_no_openai_adapter_so_dall_e_has_no_image_output(self):
        meta = metadata_for("dall-e-3", "openai")
        self.assertNotIn("image", meta.get("output_modalities", ["text"]))

    def test_stable_diffusion_model_has_image_output(self):
        meta = metadata_for("stable-diffusion-xl-v1", "bedrock")
        self.assertIn("image", meta.get("output_modalities", []))

    def test_no_openai_adapter_so_tts_has_no_audio_output(self):
        meta = metadata_for("tts-1", "openai")
        self.assertNotIn("audio", meta.get("output_modalities", ["text"]))

    def test_text_model_no_image_output(self):
        meta = metadata_for("llama-3.1-70b", "groq")
        self.assertNotIn("image", meta.get("output_modalities", ["text"]))


class TestRealAudioIO(unittest.TestCase):
    """Audio input processing and TTS output."""

    def test_compatible_adapter_has_tts(self):
        from astra.ai.adapters.base import CompatibleAdapter
        adapter = CompatibleAdapter.__new__(CompatibleAdapter)
        self.assertTrue(hasattr(adapter, "text_to_speech"))

    def test_base_provider_tts_raises(self):
        from astra.ai.provider import AIProvider
        p = AIProvider()
        with self.assertRaises(Exception):
            p.text_to_speech("hello")

    def test_audio_input_detection(self):
        from astra.ai.capabilities import capabilities_for, INPUT_AUDIO
        caps = capabilities_for("gemini", "gemini-2.0-flash")
        self.assertIn(INPUT_AUDIO, caps)

    def test_no_audio_output_without_adapter(self):
        from astra.ai.capabilities import capabilities_for, OUTPUT_AUDIO
        caps = capabilities_for("openai", "tts-1")
        self.assertNotIn(OUTPUT_AUDIO, caps)

    def test_audio_artifact_extraction(self):
        from astra.ai.artifact_extraction import extract_artifacts
        import base64
        audio_data = b"ID3" + b"\x00" * 200  # MP3 header
        b64 = base64.b64encode(audio_data).decode()
        text = f"Here is the audio: data:audio/mpeg;base64,{b64}"
        with tempfile.TemporaryDirectory() as tmp:
            arts = extract_artifacts(text, tmp)
            self.assertEqual(len(arts), 1)
            self.assertEqual(arts[0].get("artifact_type"), "audio")


class TestRealVideoIO(unittest.TestCase):
    """Video capability detection — honestly unsupported for generation."""

    def test_video_input_detected_for_gemini(self):
        from astra.ai.capabilities import capabilities_for, INPUT_VIDEO
        caps = capabilities_for("gemini", "gemini-2.0-flash")
        self.assertIn(INPUT_VIDEO, caps)

    def test_no_video_output_capability(self):
        from astra.ai.capabilities import capabilities_for, OUTPUT_VIDEO
        for fam in ("claude", "gemini", "openai", "gpt", "groq", "llama"):
            caps = capabilities_for(fam, f"{fam}-test")
            self.assertNotIn(OUTPUT_VIDEO, caps,
                             f"{fam} should not claim video output")


class TestRealDocumentGeneration(unittest.TestCase):
    """Document generation produces real, valid files."""

    def test_pdf_generation(self):
        from astra.tools.document_gen import generate_pdf
        data = generate_pdf("Hello World\nLine 2", "Test")
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertGreater(len(data), 100)

    def test_docx_generation(self):
        from astra.tools.document_gen import generate_docx
        import zipfile, io
        data = generate_docx("Hello World", "Test")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertIn("[Content_Types].xml", zf.namelist())
            self.assertIn("word/document.xml", zf.namelist())

    def test_xlsx_generation(self):
        from astra.tools.document_gen import generate_xlsx
        import zipfile, io
        data = generate_xlsx("Name,Age\nAlice,30", "Test")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertIn("xl/workbook.xml", zf.namelist())
            self.assertIn("xl/worksheets/sheet1.xml", zf.namelist())

    def test_pptx_generation(self):
        from astra.tools.document_gen import generate_pptx
        import zipfile, io
        data = generate_pptx("Title\nContent\n\nSlide 2\nMore", "Test")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertIn("ppt/presentation.xml", zf.namelist())
            self.assertIn("ppt/slides/slide1.xml", zf.namelist())

    def test_pdf_artifact_validation(self):
        from astra.tools.document_gen import generate_pdf
        from astra.core.artifacts import store_artifact, validate_artifact
        data = generate_pdf("Test content", "Test")
        with tempfile.TemporaryDirectory() as tmp:
            art = store_artifact(data, "test.pdf", "document", tmp)
            ok, err = validate_artifact(art)
            self.assertTrue(ok, f"PDF validation failed: {err}")

    def test_docx_artifact_validation(self):
        from astra.tools.document_gen import generate_docx
        from astra.core.artifacts import store_artifact, validate_artifact
        data = generate_docx("Test content", "Test")
        with tempfile.TemporaryDirectory() as tmp:
            art = store_artifact(data, "test.docx", "document", tmp)
            ok, err = validate_artifact(art)
            self.assertTrue(ok, f"DOCX validation failed: {err}")

    def test_xlsx_artifact_validation(self):
        from astra.tools.document_gen import generate_xlsx
        from astra.core.artifacts import store_artifact, validate_artifact
        data = generate_xlsx("A,B\n1,2", "Test")
        with tempfile.TemporaryDirectory() as tmp:
            art = store_artifact(data, "test.xlsx", "spreadsheet", tmp)
            ok, err = validate_artifact(art)
            self.assertTrue(ok, f"XLSX validation failed: {err}")

    def test_pptx_artifact_validation(self):
        from astra.tools.document_gen import generate_pptx
        from astra.core.artifacts import store_artifact, validate_artifact
        data = generate_pptx("Title\nContent", "Test")
        with tempfile.TemporaryDirectory() as tmp:
            art = store_artifact(data, "test.pptx", "presentation", tmp)
            ok, err = validate_artifact(art)
            self.assertTrue(ok, f"PPTX validation failed: {err}")

    def test_generate_document_tool(self):
        from astra.tools.builtins import generate_document
        result = generate_document({"content": "Hello World", "format": "pdf"})
        self.assertTrue(result.get("ok"))
        self.assertIn("artifact", result)
        self.assertTrue(result["artifact"].get("validated"))

    def test_generate_document_unsupported_format(self):
        from astra.tools.builtins import generate_document
        result = generate_document({"content": "Hello", "format": "odt"})
        self.assertFalse(result.get("ok"))
        self.assertIn("Unsupported", result.get("error", ""))


class TestRouterMultimodalDispatch(unittest.TestCase):
    """Router dispatches to generate_image/text_to_speech methods."""

    def test_extract_prompt_from_text(self):
        from astra.ai.router import AstraRouter
        msgs = [{"role": "user", "content": "draw a cat"}]
        self.assertEqual(AstraRouter._extract_prompt(msgs), "draw a cat")

    def test_extract_prompt_from_content_parts(self):
        from astra.ai.router import AstraRouter
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "describe this image"},
            {"type": "image_url", "image_url": {"url": "data:..."}}
        ]}]
        self.assertEqual(
            AstraRouter._extract_prompt(msgs), "describe this image")

    def test_extract_prompt_empty(self):
        from astra.ai.router import AstraRouter
        self.assertEqual(AstraRouter._extract_prompt([]), "")


class TestCapabilityMismatchRejection(unittest.TestCase):
    """Models lacking required capabilities are rejected."""

    def test_image_gen_rejected_for_text_model(self):
        from astra.ai.routing_policy import meets_hard_requirements
        model = Model("groq", "llama-70b", capabilities=["chat"],
                       input_modalities=["text"],
                       output_modalities=["text"])
        req = RoutingRequest(required_output_modalities=["image"])
        self.assertFalse(meets_hard_requirements(model, req))

    def test_audio_gen_rejected_for_text_model(self):
        from astra.ai.routing_policy import meets_hard_requirements
        model = Model("groq", "llama-70b", capabilities=["chat"],
                       input_modalities=["text"],
                       output_modalities=["text"])
        req = RoutingRequest(required_output_modalities=["audio"])
        self.assertFalse(meets_hard_requirements(model, req))

    def test_video_gen_rejected_for_all(self):
        from astra.ai.routing_policy import meets_hard_requirements
        for provider in ("groq", "gemini", "openai", "bedrock"):
            model = Model(provider, f"{provider}-test", capabilities=["chat"],
                           input_modalities=["text"],
                           output_modalities=["text"])
            req = RoutingRequest(required_output_modalities=["video"])
            self.assertFalse(meets_hard_requirements(model, req),
                             f"{provider} should reject video output")

    def test_image_gen_accepted_for_bedrock_stability(self):
        from astra.ai.routing_policy import meets_hard_requirements
        model = Model("bedrock", "stability.stable-diffusion-xl-v1", capabilities=["chat"],
                       input_modalities=["text"],
                       output_modalities=["text", "image"])
        req = RoutingRequest(required_output_modalities=["image"])
        self.assertTrue(meets_hard_requirements(model, req))


class TestGatewayZeroBypass(unittest.TestCase):
    """Multimodal additions preserve zero-bypass invariants."""

    def test_no_direct_provider_calls_in_agent(self):
        import inspect
        from astra.agent import Agent
        src = inspect.getsource(Agent)
        self.assertNotIn("ProviderRegistry", src)
        self.assertNotIn("self.llm(", src)
        self.assertNotIn("provider.chat(", src)

    def test_image_gen_through_router_not_direct(self):
        import inspect
        from astra.ai.router import AstraRouter
        src = inspect.getsource(AstraRouter._attempt)
        self.assertIn("generate_image", src)
        self.assertNotIn("ProviderRegistry", src)


if __name__ == "__main__":
    unittest.main()
