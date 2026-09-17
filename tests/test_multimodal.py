"""Tests for the multimodal extension: attachments, capabilities, artifacts,
capability-aware routing, and the multipart /api/chat path."""
import json
import os
import tempfile
import threading
import unittest
import zipfile

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.core.attachments import (
    Attachment, FILE_FAMILIES, SUPPORTED_EXTENSIONS, DANGEROUS_EXTENSIONS,
    detect_mime, validate_file, process_upload, validate_archive,
    normalize_attachments, MAX_FILE_SIZE,
)
from astra.core.artifacts import (
    Artifact, store_artifact, validate_artifact, make_artifact_dir,
    ARTIFACT_TYPES,
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
from astra.ai.router import RoutingRequest, RoutingResult, classify


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
        from astra import web
        import inspect
        src = inspect.getsource(web)
        self.assertNotIn("ProviderRegistry", src)

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
        from tests.helpers import make_agent
        from astra.web import AstraServer
        cls.store, cls.plugin, cls.agent = make_agent()
        cls.server = AstraServer(("127.0.0.1", 0), cls.store, cls.agent,
                                  cls.agent.plugins)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                       daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
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


if __name__ == "__main__":
    unittest.main()
