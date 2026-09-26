# -*- coding: utf-8 -*-
"""Hard architectural boundary: AstraAIGateway must never execute a
provider image API itself. Image-generation execution is owned exclusively
by `astra.ai.image_router.ImageRouter`.

Required call graph for every image_generation / image_editing request::

    User -> ChatPipeline -> Gateway (classification/handoff only)
    -> gateway.image_router.ImageRouter.generate(...)
    -> Provider Adapter (conn.generate_image) -> Actual Image API

These tests fail if any of the following holds:
  - an image request is not detected/classified as `image_generation`,
  - the Gateway is never entered, or the ImageRouter is never called,
  - `ChatPipeline` calls `Gateway.generate_image()` directly instead of
    `gateway.image_router.generate()`, or
  - the Gateway object itself performs the provider HTTP call (i.e. any
    connection's `generate_image` is invoked outside image_router.py), or
  - `ChatPipeline` reaches the Provider router's separate image dispatch
    instead of the ImageRouter.
"""
from __future__ import annotations

import base64
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.ai.chat_pipeline import ChatPipeline
from astra.ai.gateway import AstraAIGateway
from astra.ai.image_router import ImageRouter
from astra.ai.router import classify

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 400
DATA_URI = "data:image/png;base64," + base64.b64encode(PNG).decode()


class _FakeConn:
    """Minimal provider-adapter double: records every call it receives."""

    def __init__(self, name, short, image_models=()):
        self.name = name
        self.short = short
        self.image_models = list(image_models)
        self.pool = True
        self.image_calls = []

    def health_check(self):
        return True

    def supports(self, cap):
        return True

    def list_image_models(self, *, discover=True):
        return list(self.image_models)

    def live_image_models(self, *, discover=True):
        return []

    def generate_image(self, prompt, model=None, size="1024x1024", n=1):
        self.image_calls.append((model, prompt))
        return DATA_URI


GEMINI_IMG = "gemini-2.5-flash-image"


class TestGatewayNeverExecutesImageProviderAPIs(unittest.TestCase):
    """The execution boundary test required by the architecture: the
    Gateway must delegate, never dial the provider itself."""

    def _gw(self):
        conn = _FakeConn("astra-gw-gemini", "gemini",
                         image_models=[GEMINI_IMG])
        return AstraAIGateway(connections=[conn]), conn

    def test_gateway_owns_an_image_router_not_execution(self):
        gw, _conn = self._gw()
        self.assertIsInstance(gw.image_router, ImageRouter)

    def test_image_router_generate_reaches_the_provider_adapter(self):
        gw, conn = self._gw()
        uri = gw.image_router.generate("a red bicycle")
        self.assertEqual(uri, DATA_URI)
        self.assertEqual(len(conn.image_calls), 1)

    def test_gateway_generate_image_is_a_pure_delegating_wrapper(self):
        """`Gateway.generate_image()` must do nothing but hand off to
        `image_router.generate()` -- kept only for backward compatibility.
        It must never itself reach a provider connection."""
        gw, _conn = self._gw()
        with mock.patch.object(gw.image_router, "generate",
                               return_value=DATA_URI) as spy:
            result = gw.generate_image("a red bicycle")
        spy.assert_called_once()
        self.assertEqual(result, DATA_URI)

    def test_chat_pipeline_calls_image_router_not_gateway_generate_image(self):
        """This is the hard architectural test: ChatPipeline must call
        `gateway.image_router.generate`, and must NEVER call
        `gateway.generate_image` directly."""
        gw, conn = self._gw()
        pipeline = ChatPipeline.__new__(ChatPipeline)
        pipeline.gateway = gw
        pipeline._gateway_usable = lambda: True

        with mock.patch.object(
                gw, "generate_image",
                side_effect=AssertionError(
                    "ChatPipeline must not call Gateway.generate_image()")
        ) as gateway_execute, \
             mock.patch.object(gw.image_router, "generate",
                               wraps=gw.image_router.generate) as router_generate:
            rr = pipeline._route_image(
                "image_generation",
                [{"role": "user", "content": "draw a red bicycle"}],
                model=None, req="t1")

        gateway_execute.assert_not_called()
        router_generate.assert_called_once()
        self.assertTrue(rr.ok)
        self.assertEqual(len(conn.image_calls), 1)


class TestImageGenerationBoundaryHops(unittest.TestCase):
    """Asserts each hop of the required call graph explicitly, so a
    regression in any single hop is localised and obvious."""

    def _gw(self):
        conn = _FakeConn("astra-gw-gemini", "gemini",
                         image_models=[GEMINI_IMG])
        return AstraAIGateway(connections=[conn]), conn

    def test_image_generation_is_detected_then_the_gateway_is_entered(self):
        # Hop 1: the request really classifies as image_generation.
        text = "generate an image of a sunset"
        self.assertEqual(classify(text), "image_generation")
        # Hop 2/3: the Gateway is entered and hands off to ImageRouter,
        # which must reach the provider adapter for the request to succeed.
        gw, conn = self._gw()
        pipeline = ChatPipeline.__new__(ChatPipeline)
        pipeline.gateway = gw
        pipeline._gateway_usable = lambda: True
        rr = pipeline._route_image(
            "image_generation",
            [{"role": "user", "content": text}],
            model=None, req="t9")
        self.assertTrue(rr.ok)
        self.assertEqual([m for m, _p in conn.image_calls], [GEMINI_IMG])

    def test_gateway_alone_never_reaches_a_provider_image_api(self):
        # With ImageRouter stubbed out, the Gateway performs NO provider
        # image call: execution ownership stays with the router.
        gw, conn = self._gw()
        with mock.patch.object(gw.image_router, "generate",
                               return_value=DATA_URI):
            self.assertEqual(gw.generate_image("draw a red bicycle"), DATA_URI)
        self.assertEqual(conn.image_calls, [])

    def test_provider_adapter_image_call_exists_only_in_the_image_router(self):
        # Static proof of the boundary: no module may invoke
        # `conn.generate_image(...)` except astra/ai/image_router.py.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for dirpath, _dirs, files in os.walk(os.path.join(root, "astra")):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as fh:
                    src = fh.read()
                if "conn.generate_image" in src and name != "image_router.py":
                    offenders.append(os.path.relpath(path, root))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
