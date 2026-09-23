"""Astra AI Gateway provider coverage.

The Gateway has ten connections: the original Gemini / Groq / Cloudflare /
Bedrock services plus OpenRouter / Mistral / Cerebras / SambaNova / Cohere /
Z.AI (GLM). Every connection is independently configured through its own
GW_* env vars, and all ten share ONE execution contract (request →
normalized response → usage → classification → retry → failover), which is
what these tests pin.

Only the *upstream* is faked (a mock `urlopen` / a fake connection object):
the Gateway's own request construction, auth headers, normalization, retry,
error classification, cooldown, catalog and failover logic are the real
implementations.
"""
from __future__ import annotations

import io
import json
import os
import socket
import unittest
import urllib.error
from unittest import mock

from astra.core.config import Config
from astra.core.exceptions import ProviderError, TimeoutError
from astra.ai.gateway import (GATEWAY_CONNECTIONS, AstraAIGateway,
                              AstraGatewayBedrock, AstraGatewayCerebras,
                              AstraGatewayCloudflare, AstraGatewayCohere,
                              AstraGatewayGemini, AstraGatewayGroq,
                              AstraGatewayMistral, AstraGatewayOpenRouter,
                              AstraGatewaySambaNova, AstraGatewayZAI)
from astra.ai.gateway_routing import (GATEWAY_PROVIDER_SHORT,
                                      build_gateway_catalog,
                                      classify_gateway_request,
                                      eligible_targets, rank_targets)

# ── the ten connections, with the official endpoint each must use ──────────

EXISTING_PROVIDERS = (
    # class, env prefix, official base URL, a model id it must default to
    (AstraGatewayGemini, "GW_GEMINI",
     "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.5-flash"),
    (AstraGatewayGroq, "GW_GROQ", "https://api.groq.com/openai/v1",
     "openai/gpt-oss-120b"),
    (AstraGatewayCloudflare, "GW_CLOUDFLARE",
     "https://api.cloudflare.com/client/v4",
     "@cf/meta/llama-3.3-70b-instruct-fp8-fast"),
    (AstraGatewayBedrock, "GW_BEDROCK",
     "https://bedrock-runtime.us-east-1.amazonaws.com", "amazon.nova-lite-v1:0"),
)

NEW_PROVIDERS = (
    (AstraGatewayOpenRouter, "GW_OPENROUTER", "https://openrouter.ai/api/v1",
     "nvidia/nemotron-3-super-120b-a12b:free"),
    (AstraGatewayMistral, "GW_MISTRAL", "https://api.mistral.ai/v1",
     "mistral-small-2603"),
    (AstraGatewayCerebras, "GW_CEREBRAS", "https://api.cerebras.ai/v1",
     "gpt-oss-120b"),
    (AstraGatewaySambaNova, "GW_SAMBANOVA", "https://api.sambanova.ai/v1",
     "Meta-Llama-3.3-70B-Instruct"),
    (AstraGatewayCohere, "GW_COHERE", "https://api.cohere.ai/compatibility/v1",
     "command-a-03-2025"),
    (AstraGatewayZAI, "GW_ZAI", "https://api.z.ai/api/paas/v4", "glm-4.7-flash"),
)

ALL_PROVIDERS = EXISTING_PROVIDERS + NEW_PROVIDERS


def _cfg(**env):
    cfg = Config()
    cfg._runtime.update(env)
    return cfg


def _events():
    class _E:
        def __init__(self):
            self.rows = []
        def emit(self, kind, agent="", **data):
            self.rows.append({"kind": kind, "agent": agent, "data": data})
            return self.rows[-1]
        def kinds(self):
            return [r["kind"] for r in self.rows]
        def dump(self):
            return json.dumps(self.rows, default=str)
    return _E()


def _resp(body: bytes):
    class _R:
        def read(self):
            return body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def close(self):
            pass
    return _R()


def _openai_body(text="hello", usage=None):
    payload = {"choices": [{"message": {"content": text}}]}
    if usage is not None:
        payload["usage"] = usage
    return json.dumps(payload).encode()


def _sse(*frames: dict):
    chunks = [f"data: {json.dumps(f)}\n\n" for f in frames]
    chunks.append("data: [DONE]\n\n")
    return "".join(chunks).encode()


def _http_error(code, body=b"{}"):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body))


def _capture(responses, seen=None):
    """side_effect for urlopen: records each Request, then returns/raises the
    next scripted response (raising a callable when it is a callable)."""
    seen = seen if seen is not None else []
    queue = list(responses)

    def _side_effect(req, timeout=None):
        seen.append({"url": req.full_url, "headers": dict(req.headers),
                     "body": json.loads(req.data.decode()) if req.data else None,
                     "timeout": timeout})
        item = queue.pop(0) if queue else _resp(_openai_body())
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            raise item()
        return item
    return _side_effect, seen


def _conn(cls, cfg, **kw):
    """Build one connection the way the Gateway does (GW_* env only)."""
    env = {cls.api_keys_env: "test-key-1", cls.models_env: kw.pop(
        "models", None) or _default_model(cls)}
    env.update(kw.pop("env", {}))
    return cls(config=_cfg(**env), **kw)


def _default_model(cls):
    for c, _p, _u, model in ALL_PROVIDERS:
        if c is cls:
            return model
    return "m1"


# ═══════════════════════════════════════════════════════════════════════════
# Registration / configuration
# ═══════════════════════════════════════════════════════════════════════════
class GatewayProviderRegistrationTests(unittest.TestCase):
    def test_ten_connections_declared_in_order(self):
        names = [c.name for c in GATEWAY_CONNECTIONS]
        self.assertEqual(len(names), 10)
        self.assertEqual(names[:4], ["astra-gw-gemini", "astra-gw-groq",
                                     "astra-gw-cloudflare", "astra-gw-bedrock"])
        self.assertEqual(names[4:], ["astra-gw-openrouter", "astra-gw-mistral",
                                     "astra-gw-cerebras", "astra-gw-sambanova",
                                     "astra-gw-cohere", "astra-gw-zai"])

    def test_all_ten_have_a_short_provider_name(self):
        for cls in GATEWAY_CONNECTIONS:
            self.assertIn(cls.name, GATEWAY_PROVIDER_SHORT)
        # short names are the familiar provider names, used for model metadata
        self.assertEqual(GATEWAY_PROVIDER_SHORT["astra-gw-zai"], "zai")
        self.assertEqual(GATEWAY_PROVIDER_SHORT["astra-gw-sambanova"], "sambanova")

    def test_official_endpoints_and_gw_env_names(self):
        for cls, prefix, base_url, _model in ALL_PROVIDERS:
            # Bedrock derives its host from the AWS region (no class-level
            # base_url); every other connection declares its official one.
            if cls is not AstraGatewayBedrock:
                self.assertEqual(cls.base_url, base_url, cls.name)
            self.assertEqual(cls.api_keys_env, f"{prefix}_API_KEYS", cls.name)
            self.assertEqual(cls.models_env, f"{prefix}_MODELS", cls.name)
            self.assertEqual(cls.base_url_env, f"{prefix}_BASE_URL", cls.name)
        self.assertEqual(AstraGatewayBedrock().region, "us-east-1")

    def test_every_provider_is_registerable_when_configured(self):
        env, expected = {}, []
        for cls, _prefix, _url, model in ALL_PROVIDERS:
            env[cls.api_keys_env] = "key-1"
            env[cls.models_env] = model
            expected.append(cls.name)
        env["GW_CLOUDFLARE_ACCOUNT_IDS"] = "acct-1"
        gw = AstraAIGateway(config=_cfg(**env))
        self.assertEqual([c.name for c in gw.connections], expected)
        self.assertEqual(len(gw.connections), 10)
        self.assertTrue(gw.is_usable())

    def test_unconfigured_connections_are_absent_not_fatal(self):
        gw = AstraAIGateway(config=_cfg())
        self.assertEqual(gw.connections, [])
        self.assertFalse(gw.is_usable())
        self.assertEqual(gw.models, [])

    def test_only_configured_new_provider_is_built(self):
        gw = AstraAIGateway(config=_cfg(GW_CEREBRAS_API_KEYS="k",
                                        GW_CEREBRAS_MODELS="gpt-oss-120b"))
        self.assertEqual([c.name for c in gw.connections], ["astra-gw-cerebras"])

    def test_missing_api_key_means_no_connection(self):
        for cls, prefix, _url, model in NEW_PROVIDERS:
            gw = AstraAIGateway(config=_cfg(**{cls.models_env: model}))
            self.assertEqual(gw.connections, [], f"{prefix}: models without key")
            conn = cls(config=_cfg(**{cls.models_env: model}))
            self.assertFalse(bool(conn.pool))
            with self.assertRaises(ProviderError) as ctx:
                conn.chat([{"role": "user", "content": "hi"}])
            self.assertIn("no healthy credential", str(ctx.exception))

    def test_blank_api_key_is_treated_as_unconfigured(self):
        gw = AstraAIGateway(config=_cfg(GW_MISTRAL_API_KEYS="", GW_MISTRAL_MODELS="m"))
        self.assertEqual(gw.connections, [])

    def test_build_connection_never_raises_on_bad_config(self):
        from astra.ai.gateway import _build_connection
        self.assertIsNone(_build_connection(AstraGatewayMistral, None))
        self.assertIsNone(_build_connection(AstraGatewayMistral, _cfg()))

    def test_sambanova_accepts_canonical_and_short_prefixes(self):
        for env in ("GW_SAMBANOVA_API_KEYS", "GW_SAMBA_API_KEYS"):
            gw = AstraAIGateway(config=_cfg(**{
                env: "k", env.replace("_API_KEYS", "_MODELS"): "Meta-Llama-3.3-70B-Instruct"}))
            self.assertEqual([c.name for c in gw.connections],
                             ["astra-gw-sambanova"], env)
            self.assertEqual(gw.connections[0].models, ["Meta-Llama-3.3-70B-Instruct"])

    def test_model_configuration_is_read_per_connection(self):
        conn = _conn(AstraGatewayZAI, _cfg(), models="glm-4.6,glm-4.7-flash")
        self.assertEqual(conn.models, ["glm-4.6", "glm-4.7-flash"])

    def test_base_url_override_is_honored(self):
        conn = _conn(AstraGatewayCerebras, _cfg(), env={"GW_CEREBRAS_BASE_URL": "http://localhost:9/v1/"})
        self.assertEqual(conn.base_url, "http://localhost:9/v1")

    def test_capabilities_match_the_provider_adapter_declarations(self):
        for cls, prefix, _url, _m in NEW_PROVIDERS:
            self.assertIn("chat", cls.capabilities)
            self.assertIn("stream", cls.capabilities)
            self.assertIn("tools", cls.capabilities, prefix)
        self.assertIn("vision", AstraGatewayOpenRouter.capabilities)
        self.assertIn("coding", AstraGatewayMistral.capabilities)
        self.assertIn("vision", AstraGatewayCohere.capabilities)


# ═══════════════════════════════════════════════════════════════════════════
# Request construction / authentication
# ═══════════════════════════════════════════════════════════════════════════
class GatewayProviderRequestTests(unittest.TestCase):
    def test_chat_posts_to_official_chat_completions_path(self):
        for cls, prefix, base_url, model in NEW_PROVIDERS:
            conn = _conn(cls, _cfg())
            side, seen = _capture([_resp(_openai_body("ok"))])
            with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
                self.assertEqual(conn.chat([{"role": "user", "content": "hi"}]), "ok")
            self.assertEqual(len(seen), 1, prefix)
            self.assertEqual(seen[0]["url"], f"{base_url}/chat/completions", prefix)
            self.assertEqual(seen[0]["body"]["model"], model, prefix)
            self.assertEqual(seen[0]["body"]["messages"],
                             [{"role": "user", "content": "hi"}])
            self.assertNotIn("max_tokens", seen[0]["body"],
                             "unset budget must be omitted (no Astra-imposed cap)")

    def test_max_tokens_is_forwarded_only_when_explicit(self):
        conn = _conn(AstraGatewayMistral, _cfg())
        side, seen = _capture([_resp(_openai_body())])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            conn.chat([{"role": "user", "content": "hi"}], max_tokens=1234)
        self.assertEqual(seen[0]["body"]["max_tokens"], 1234)

    def test_explicit_model_overrides_the_default(self):
        conn = _conn(AstraGatewayCohere, _cfg())
        side, seen = _capture([_resp(_openai_body())])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            conn.chat([{"role": "user", "content": "hi"}], model="command-a-reasoning-08-2025")
        self.assertEqual(seen[0]["body"]["model"], "command-a-reasoning-08-2025")

    def test_bearer_authentication_uses_the_pooled_secret(self):
        for cls, prefix, _u, _m in NEW_PROVIDERS:
            conn = _conn(cls, _cfg(), env={cls.api_keys_env: "sk-secret-xyz"})
            side, seen = _capture([_resp(_openai_body())])
            with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
                conn.chat([{"role": "user", "content": "hi"}])
            self.assertEqual(seen[0]["headers"].get("Authorization"),
                             "Bearer sk-secret-xyz", prefix)
            self.assertEqual(seen[0]["headers"].get("Content-type",
                                                    seen[0]["headers"].get("Content-Type")),
                             "application/json", prefix)

    def test_openrouter_sends_attribution_headers(self):
        conn = _conn(AstraGatewayOpenRouter, _cfg())
        side, seen = _capture([_resp(_openai_body())])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            conn.chat([{"role": "user", "content": "hi"}])
        headers = {k.lower(): v for k, v in seen[0]["headers"].items()}
        self.assertEqual(headers.get("http-referer"),
                         "https://github.com/mainnetwallet/Astra-AI-Agent")
        self.assertEqual(headers.get("x-title"), "Astra AI Agent")

    def test_two_keys_are_used_across_calls(self):
        conn = _conn(AstraGatewayMistral, _cfg(),
                     env={"GW_MISTRAL_API_KEYS": "key-a,key-b"})
        side, seen = _capture([_resp(_openai_body()) for _ in range(6)])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            for i in range(6):
                conn.chat([{"role": "user", "content": str(i)}])
        auths = {s["headers"].get("Authorization") for s in seen}
        self.assertEqual(auths, {"Bearer key-a", "Bearer key-b"})


# ═══════════════════════════════════════════════════════════════════════════
# Response normalization / usage / streaming
# ═══════════════════════════════════════════════════════════════════════════
class GatewayProviderResponseTests(unittest.TestCase):
    def test_normalizes_choices_message_content(self):
        for cls, prefix, _u, _m in NEW_PROVIDERS:
            conn = _conn(cls, _cfg())
            side, _seen = _capture([_resp(_openai_body("  normalized text  "))])
            with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
                out = conn.chat([{"role": "user", "content": "hi"}])
            self.assertEqual(out, "normalized text", prefix)

    def test_empty_choice_degrades_to_placeholder_not_a_crash(self):
        conn = _conn(AstraGatewayCerebras, _cfg())
        side, _seen = _capture([_resp(b'{"choices": []}')])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            self.assertEqual(conn.chat([{"role": "user", "content": "hi"}]),
                             "(no reply)")

    def test_usage_metadata_is_preserved(self):
        usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
        conn = _conn(AstraGatewaySambaNova, _cfg())
        side, _seen = _capture([_resp(_openai_body("x", usage=usage))])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            conn.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(conn.last_usage(), usage)
        self.assertEqual(conn._last_usage, usage)

    def test_usage_is_empty_when_provider_reports_none(self):
        conn = _conn(AstraGatewayZAI, _cfg())
        side, _seen = _capture([_resp(_openai_body("x"))])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            conn.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(conn.last_usage(), {})

    def test_streaming_yields_deltas_and_emits_lifecycle_events(self):
        for cls, prefix, _u, _m in NEW_PROVIDERS:
            events = _events()
            conn = _conn(cls, _cfg(), events=events)
            body = _sse({"choices": [{"delta": {"content": "Hel"}}]},
                        {"choices": [{"delta": {"content": "lo"}}]})
            side, seen = _capture([_resp(body)])
            with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
                out = list(conn.stream([{"role": "user", "content": "hi"}]))
            self.assertEqual(out, ["Hel", "lo"], prefix)
            self.assertEqual(seen[0]["body"]["stream"], True, prefix)
            self.assertIn("ai.started", events.kinds(), prefix)
            self.assertIn("ai.completed", events.kinds(), prefix)

    def test_streaming_usage_from_final_frame_is_preserved(self):
        usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
        conn = _conn(AstraGatewayCohere, _cfg())
        body = _sse({"choices": [{"delta": {"content": "hi"}}]},
                    {"choices": [], "usage": usage})
        side, _seen = _capture([_resp(body)])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            list(conn.stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(conn.last_usage(), usage)

    def test_streaming_ignores_non_data_lines_and_done_sentinel(self):
        conn = _conn(AstraGatewayOpenRouter, _cfg())
        body = (b": keep-alive\n\n"
                b"data: {not json}\n\n"
                b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                b"data: [DONE]\n\n")
        side, _seen = _capture([_resp(body)])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            self.assertEqual(list(conn.stream([{"role": "user", "content": "x"}])),
                             ["ok"])
        self.assertEqual(conn.last_usage(), {})

    def test_stream_timeout_uses_the_stream_timeout(self):
        from astra.ai.gateway import GW_STREAM_TIMEOUT
        conn = _conn(AstraGatewayMistral, _cfg())
        side, seen = _capture([_resp(_sse({"choices": [{"delta": {"content": "x"}}]}))])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            list(conn.stream([{"role": "user", "content": "x"}]))
        self.assertEqual(seen[0]["timeout"], GW_STREAM_TIMEOUT)


# ═══════════════════════════════════════════════════════════════════════════
# Timeouts / retries / rate limits / upstream failures / cooldown
# ═══════════════════════════════════════════════════════════════════════════
class GatewayProviderErrorTests(unittest.TestCase):
    def test_http_timeout_is_classified_and_retried(self):
        conn = _conn(AstraGatewayCerebras, _cfg(),
                     env={"GW_CEREBRAS_API_KEYS": "k1,k2", "GW_RETRY_BACKOFF": "0"})
        side, seen = _capture([_http_error(408), _resp(_openai_body("second try"))])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            self.assertEqual(conn.chat([{"role": "user", "content": "x"}]), "second try")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1]["headers"]["Authorization"], "Bearer k2")

    def test_socket_timeout_raises_astra_timeout_error(self):
        conn = _conn(AstraGatewaySambaNova, _cfg(),
                     env={"GW_SAMBANOVA_API_KEYS": "k1,k2", "GW_RETRY_BACKOFF": "0"})

        def boom():
            raise socket.timeout("timed out")

        side, seen = _capture([boom, boom])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            with self.assertRaises(TimeoutError):
                conn.chat([{"role": "user", "content": "x"}])
        self.assertEqual(len(seen), 2, "transient timeout must be retried")

    def test_rate_limit_is_classified_retried_and_cools_the_key(self):
        conn = _conn(AstraGatewayZAI, _cfg(),
                     env={"GW_ZAI_API_KEYS": "k1,k2", "GW_RETRY_BACKOFF": "0"})
        side, seen = _capture([_http_error(429), _resp(_openai_body("ok"))])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            self.assertEqual(conn.chat([{"role": "user", "content": "x"}]), "ok")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1]["headers"]["Authorization"], "Bearer k2")

    def test_rate_limit_with_single_key_fails_over_with_a_truthful_error(self):
        conn = _conn(AstraGatewayCohere, _cfg(), env={"GW_RETRY_BACKOFF": "0"})
        side, seen = _capture([_http_error(429)])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            with self.assertRaises(ProviderError) as ctx:
                conn.chat([{"role": "user", "content": "x"}])
        self.assertIn("rate limit", str(ctx.exception))
        self.assertIsNone(conn.pool.pick(), "429 must cool the only key")
        self.assertEqual(len(seen), 1, "no point re-calling with no healthy key")

    def test_5xx_is_retried_then_surfaces(self):
        conn = _conn(AstraGatewayMistral, _cfg(),
                     env={"GW_MISTRAL_API_KEYS": "k1,k2", "GW_RETRY_BACKOFF": "0"})
        side, seen = _capture([_http_error(503), _http_error(503)])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            with self.assertRaises(ProviderError) as ctx:
                conn.chat([{"role": "user", "content": "x"}])
        self.assertIn("503", str(ctx.exception))
        self.assertEqual(len(seen), 2)

    def test_auth_failure_is_never_retried(self):
        conn = _conn(AstraGatewayOpenRouter, _cfg(),
                     env={"GW_OPENROUTER_API_KEYS": "k1,k2", "GW_RETRY_BACKOFF": "0"})
        side, seen = _capture([_http_error(401)])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            with self.assertRaises(ProviderError) as ctx:
                conn.chat([{"role": "user", "content": "x"}])
        self.assertIn("authentication failed", str(ctx.exception))
        self.assertEqual(len(seen), 1, "a bad key cannot be fixed by retrying")

    def test_model_level_404_keeps_the_key_usable_and_is_not_retried(self):
        conn = _conn(AstraGatewaySambaNova, _cfg(), env={"GW_RETRY_BACKOFF": "0"})
        side, seen = _capture([_http_error(404)])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            with self.assertRaises(ProviderError):
                conn.chat([{"role": "user", "content": "x"}], model="gone")
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(conn.pool.pick(), "404 must not cool the key")

    def test_network_error_is_classified_retryable(self):
        conn = _conn(AstraGatewayCerebras, _cfg(),
                     env={"GW_CEREBRAS_API_KEYS": "k1,k2", "GW_RETRY_BACKOFF": "0"})

        def boom():
            raise urllib.error.URLError("dns went away")

        side, seen = _capture([boom, _resp(_openai_body("recovered"))])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            self.assertEqual(conn.chat([{"role": "user", "content": "x"}]), "recovered")
        self.assertEqual(len(seen), 2)

    def test_bad_json_is_not_retried(self):
        conn = _conn(AstraGatewayMistral, _cfg(),
                     env={"GW_MISTRAL_API_KEYS": "k1,k2", "GW_RETRY_BACKOFF": "0"})
        side, seen = _capture([_resp(b"<html>not json</html>")])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            with self.assertRaises(ProviderError) as ctx:
                conn.chat([{"role": "user", "content": "x"}])
        self.assertIn("bad json", str(ctx.exception))
        self.assertEqual(len(seen), 1)

    def test_retries_are_configurable_and_bounded(self):
        conn = _conn(AstraGatewayZAI, _cfg(),
                     env={"GW_ZAI_API_KEYS": "k1,k2,k3", "GW_MAX_RETRIES": "2",
                          "GW_RETRY_BACKOFF": "0"})
        side, seen = _capture([_http_error(500), _http_error(500), _http_error(500)])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            with self.assertRaises(ProviderError):
                conn.chat([{"role": "user", "content": "x"}])
        self.assertEqual(len(seen), 3, "max_retries=2 means 3 attempts, then stop")

    def test_max_retries_zero_disables_retry(self):
        conn = _conn(AstraGatewayMistral, _cfg(),
                     env={"GW_MISTRAL_API_KEYS": "k1,k2", "GW_MAX_RETRIES": "0"})
        side, seen = _capture([_http_error(500)])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            with self.assertRaises(ProviderError):
                conn.chat([{"role": "user", "content": "x"}])
        self.assertEqual(len(seen), 1)

    def test_health_check_and_credential_summary(self):
        for cls, prefix, _u, _m in NEW_PROVIDERS:
            conn = _conn(cls, _cfg())
            self.assertTrue(conn.health_check(), prefix)
            summary = conn.credential_summary()
            self.assertEqual(summary["total_credentials"], 1, prefix)
            self.assertEqual(summary["credentials"], 1, prefix)
            self.assertTrue(summary["healthy"], prefix)
            self.assertNotIn("test-key-1", json.dumps(summary), prefix)


# ═══════════════════════════════════════════════════════════════════════════
# Credential isolation / redaction
# ═══════════════════════════════════════════════════════════════════════════
class GatewayProviderSecretRedactionTests(unittest.TestCase):
    SECRET = "sk-live-DO-NOT-LEAK-12345"

    def test_secret_never_appears_in_errors_events_or_summaries(self):
        for cls, prefix, _u, _m in NEW_PROVIDERS:
            events = _events()
            conn = cls(config=_cfg(**{cls.api_keys_env: self.SECRET,
                                      cls.models_env: _default_model(cls),
                                      "GW_RETRY_BACKOFF": "0"}),
                       events=events)
            side, _seen = _capture([_http_error(500), _http_error(500)])
            with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
                with self.assertRaises(ProviderError) as ctx:
                    conn.chat([{"role": "user", "content": "x"}])
            self.assertNotIn(self.SECRET, str(ctx.exception), prefix)
            self.assertNotIn(self.SECRET, events.dump(), prefix)
            self.assertNotIn(self.SECRET, json.dumps(conn.credential_summary()), prefix)
            self.assertNotIn(self.SECRET, str(conn), prefix)
            self.assertNotIn(self.SECRET, repr(conn.__dict__), prefix)

    def test_secret_never_reaches_model_visible_messages(self):
        # The Gateway brain drives the loop, so the model-visible context is
        # whatever the loop builds and sends — that transcript must never
        # carry the API key, on success OR on failure.
        from astra.ai.agent_tool_loop import AgentToolLoop, GatewayToolCaller
        from astra.core.permissions import Policy
        from astra.tools.registry import ToolRegistry

        def gateway_for(**env):
            conn = AstraGatewayMistral(config=_cfg(GW_MISTRAL_API_KEYS=self.SECRET,
                                                   GW_MISTRAL_MODELS="mistral-small-2603",
                                                   GW_RETRY_BACKOFF="0", **env))
            return AstraAIGateway(connections=[conn])

        reg = ToolRegistry(policy=Policy(granted=["read"]))
        loop = AgentToolLoop(reg, max_steps=2)
        # success path
        side, _seen = _capture([_resp(_openai_body(
            json.dumps({"action": "final", "answer": "done"})))])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            res = loop.run("do it", GatewayToolCaller(gateway_for()),
                           system_prompt="You are Astra.")
        self.assertTrue(res.ok)
        self.assertNotIn(self.SECRET, json.dumps(res.messages, default=str))
        # failure path
        side, _seen = _capture([_http_error(500), _http_error(500)])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            res = loop.run("do it", GatewayToolCaller(gateway_for()),
                           system_prompt="You are Astra.")
        self.assertFalse(res.ok)
        self.assertNotIn(self.SECRET, json.dumps(res.messages, default=str))
        self.assertNotIn(self.SECRET, json.dumps(res.to_dict(), default=str))

    def test_gateway_emits_its_own_events_without_secrets(self):
        events = _events()
        conn = _conn(AstraGatewayOpenRouter, _cfg(), events=events)
        gateway = AstraAIGateway(connections=[conn], events=events)
        gateway.attach_events(events)
        side, _seen = _capture([_resp(_openai_body("ok"))])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            gateway.chat([{"role": "user", "content": "hi"}])
        self.assertIn("astra_gateway.success", events.kinds())
        self.assertNotIn("test-key-1", events.dump())


# ═══════════════════════════════════════════════════════════════════════════
# Tool-call capability gating through the Gateway catalog
# ═══════════════════════════════════════════════════════════════════════════
class GatewayToolCallingTests(unittest.TestCase):
    def test_all_new_providers_declare_tool_support(self):
        for cls, prefix, _u, _m in NEW_PROVIDERS:
            self.assertTrue(cls(name="x").supports("tools") if False else
                            "tools" in cls.capabilities, prefix)

    def test_tool_use_category_requires_a_tool_capable_model(self):
        conn = _conn(AstraGatewayOpenRouter, _cfg(),
                     models="nvidia/nemotron-3-super-120b-a12b:free")
        gw = AstraAIGateway(connections=[conn],
                            config=_cfg(GW_OPENROUTER_API_KEYS="k",
                                        GW_OPENROUTER_MODELS=conn.models[0]))
        catalog = build_gateway_catalog(gw.connections)
        target = eligible_targets(catalog, gw.routing_state, category="tool_use")
        self.assertTrue(target, "a tool-capable OpenRouter model must be eligible")
        self.assertTrue(rank_targets(target, category="tool_use"))

    def test_gateway_tool_caller_uses_the_tool_use_category(self):
        from astra.ai.agent_tool_loop import GatewayToolCaller
        seen = {}
        gateway = mock.Mock()
        gateway.chat.side_effect = lambda messages, **kw: seen.update(kw) or "ok"
        GatewayToolCaller(gateway, category="tool_use").chat(
            [{"role": "user", "content": "x"}])
        self.assertEqual(seen["category"], "tool_use")


# ═══════════════════════════════════════════════════════════════════════════
# Failover through the existing Gateway attempt loop
# ═══════════════════════════════════════════════════════════════════════════
class _StubConn:
    """Minimal Gateway connection stand-in (models + health_check + chat)."""

    def __init__(self, name, models, text="", fail=True):
        self.name = name
        self.models = list(models)
        self.capabilities = ["chat", "stream", "tools", "json"]
        self.pool = None
        self.text = text or f"reply-from-{name}"
        self.fail = fail
        self.calls = 0

    def health_check(self):
        return True

    def chat(self, messages, model=None, max_tokens=None):
        self.calls += 1
        if self.fail:
            raise ProviderError(f"{self.name} down")
        return self.text

    def stream(self, messages, model=None, max_tokens=None):
        yield self.chat(messages, model=model, max_tokens=max_tokens)

    def supports(self, capability):
        return capability in self.capabilities


class GatewayFailoverTests(unittest.TestCase):
    def _gw(self, conns, **env):
        return AstraAIGateway(connections=conns, config=_cfg(**env))

    def test_new_provider_fails_over_to_an_existing_one(self):
        bad = _StubConn("astra-gw-mistral", ["mistral-small-2603"], fail=True)
        good = _StubConn("astra-gw-gemini", ["gemini-3.5-flash"], fail=False)
        gw = self._gw([bad, good])
        self.assertEqual(gw.chat([{"role": "user", "content": "hi"}]),
                         "reply-from-astra-gw-gemini")
        self.assertEqual(gw.last_connection, "astra-gw-gemini")
        self.assertEqual(gw.last_attempts, 2)

    def test_existing_provider_fails_over_to_a_new_one(self):
        bad = _StubConn("astra-gw-groq", ["openai/gpt-oss-120b"], fail=True)
        good = _StubConn("astra-gw-cerebras", ["gpt-oss-120b"], fail=False)
        gw = self._gw([bad, good])
        self.assertEqual(gw.chat([{"role": "user", "content": "hi"}]),
                         "reply-from-astra-gw-cerebras")
        self.assertEqual(gw.last_connection, "astra-gw-cerebras")

    def test_every_new_provider_can_serve_and_fail_over(self):
        for cls, _p, _u, model in NEW_PROVIDERS:
            good = _StubConn(cls.name, [model], fail=False)
            bad_conns = [_StubConn("astra-gw-gemini", ["gemini-3.5-flash"], fail=True),
                         _StubConn("astra-gw-groq", ["openai/gpt-oss-120b"], fail=True)]
            gw = self._gw([bad_conns[0], bad_conns[1], good])
            self.assertEqual(gw.chat([{"role": "user", "content": "hi"}]),
                             f"reply-from-{cls.name}", cls.name)
            # at least one failing target was tried first, then this one
            self.assertGreaterEqual(gw.last_attempts, 2, cls.name)

    def test_total_failure_is_reported_honestly(self):
        conns = [_StubConn(c.name, [m], fail=True) for c, _p, _u, m in NEW_PROVIDERS]
        gw = self._gw(conns)
        with self.assertRaises(ProviderError) as ctx:
            gw.chat([{"role": "user", "content": "hi"}])
        self.assertIn("failed", str(ctx.exception))
        self.assertEqual(gw.last_attempts, len(conns))

    def test_unhealthy_connection_is_skipped_not_tried(self):
        class _Down(_StubConn):
            def health_check(self):
                return False
        down = _Down("astra-gw-mistral", ["mistral-small-2603"])
        good = _StubConn("astra-gw-cohere", ["command-a-03-2025"], fail=False)
        gw = self._gw([down, good])
        gw.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(down.calls, 0, "an unhealthy pool must not be dialed")
        self.assertEqual(gw.last_connection, "astra-gw-cohere")

    def test_ordering_is_preserved_when_all_are_healthy(self):
        conns = [_StubConn(c.name, [m], fail=False) for c, _p, _u, m in ALL_PROVIDERS]
        gw = self._gw(conns)
        gw.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(gw.last_connection, "astra-gw-gemini")
        self.assertEqual(conns[0].calls, 1)
        self.assertTrue(all(c.calls == 0 for c in conns[1:]))

    def test_per_model_cooldown_does_not_disable_a_connection(self):
        class _ModelDown(_StubConn):
            def chat(self, messages, model=None, max_tokens=None):
                self.calls += 1
                if model == "bad-model":
                    raise ProviderError(f"{self.name} bad model")
                return f"ok:{model}"
        conn = _ModelDown("astra-gw-openrouter",
                          ["bad-model", "nvidia/nemotron-3-super-120b-a12b:free"])
        gw = self._gw([conn])
        gw.chat([{"role": "user", "content": "hi"}])
        # the first model failed, the second served — same connection
        self.assertEqual(gw.last_model, "nvidia/nemotron-3-super-120b-a12b:free")

    def test_failover_carries_the_identical_request(self):
        seen = {}
        bad = _StubConn("astra-gw-gemini", ["gemini-3.5-flash"], fail=True)

        class _Recorder(_StubConn):
            def chat(self, messages, model=None, max_tokens=None):
                seen["messages"] = messages
                seen["model"] = model
                seen["max_tokens"] = max_tokens
                return "ok"

        gw = self._gw([bad, _Recorder("astra-gw-zai", ["glm-4.7-flash"])])
        history = [{"role": "system", "content": "SYSTEM PROMPT"},
                   {"role": "user", "content": "earlier turn"},
                   {"role": "assistant", "content": "earlier answer"},
                   {"role": "user", "content": "current question"}]
        gw.chat(history, max_tokens=77)
        self.assertEqual(seen["messages"], history)
        self.assertEqual(seen["max_tokens"], 77)
        self.assertEqual(seen["model"], "glm-4.7-flash")


# ═══════════════════════════════════════════════════════════════════════════
# Integration: history / system prompt / decisions / tools / loop intact
# ═══════════════════════════════════════════════════════════════════════════
class GatewayIntegrationTests(unittest.TestCase):
    def test_history_and_system_prompt_reach_the_new_provider_unchanged(self):
        conn = _conn(AstraGatewayCerebras, _cfg())
        side, seen = _capture([_resp(_openai_body("ok"))])
        messages = [{"role": "system", "content": "You are Astra."},
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "answer"},
                    {"role": "user", "content": "second"}]
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            conn.chat(messages)
        self.assertEqual(seen[0]["body"]["messages"], messages)

    def test_execution_decision_path_still_selects_a_target(self):
        env = {"GW_MISTRAL_API_KEYS": "k",
               "GW_MISTRAL_MODELS": "mistral-small-2603,ministral-8b-2512"}
        gw = AstraAIGateway(config=_cfg(**env))
        category, ranked = gw._select_order(
            [{"role": "user", "content": "write a python function"}], None)
        self.assertIn(category, ("coding", "general", "simple", "reasoning"))
        self.assertTrue(ranked)
        self.assertEqual(gw.last_category, category)

    def test_catalog_exposes_new_providers_with_real_metadata(self):
        conn = _conn(AstraGatewayZAI, _cfg(), models="glm-4.7-flash")
        catalog = build_gateway_catalog([conn])
        self.assertEqual(len(catalog), 1)
        _c, model = catalog[0]
        self.assertEqual(model.provider, "zai")
        self.assertEqual(model.model_id, "glm-4.7-flash")
        self.assertGreater(model.context_window, 0)
        self.assertIn("tools", model.capabilities)

    def test_classification_is_unchanged_for_new_providers(self):
        self.assertEqual(classify_gateway_request("reply with json only"),
                         "structured_output")
        self.assertEqual(classify_gateway_request(
            "summarize this page", vision=True), "vision")

    def test_tool_execution_still_goes_through_the_tool_registry(self):
        from astra.core.context import ToolContext
        from astra.core.permissions import Policy
        from astra.terminal import TerminalManager, register_terminal_tools
        from astra.terminal.tools import terminal_exec
        from astra.tools.registry import ToolRegistry

        reg = ToolRegistry(policy=Policy(granted=["read", "system_action"]))
        mgr = TerminalManager()
        register_terminal_tools(reg, mgr)
        out = terminal_exec({"command": "echo gateway-provider-test"},
                            ctx=ToolContext(terminal=mgr, terminal_session_id="s"))
        self.assertEqual(out["status"], "completed")
        self.assertIn("gateway-provider-test", out["stdout"])
        # the Gateway exposes no tool surface of its own
        conn = _conn(AstraGatewayMistral, _cfg())
        self.assertFalse(hasattr(conn, "tool_registry"))
        self.assertFalse(hasattr(AstraAIGateway(connections=[conn]), "registry"))
        mgr.close_all()

    def test_provider_failure_does_not_break_the_agent_tool_loop(self):
        from astra.ai.agent_tool_loop import AgentToolLoop, GatewayToolCaller
        from astra.core.permissions import Policy
        from astra.tools.registry import ToolRegistry

        conn = _conn(AstraGatewayMistral, _cfg())
        gateway = AstraAIGateway(connections=[conn])
        reg = ToolRegistry(policy=Policy(granted=["read"]))
        loop = AgentToolLoop(reg, max_steps=2)
        side, _seen = _capture([_http_error(500), _http_error(500)])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            res = loop.run("do it", GatewayToolCaller(gateway),
                           system_prompt="You are Astra.")
        self.assertFalse(res.ok)
        self.assertEqual(res.stopped_reason, "error")
        self.assertTrue(res.error, "the loop must report the failure, not crash")

    def test_tool_loop_still_executes_tools_when_the_gateway_works(self):
        from astra.ai.agent_tool_loop import AgentToolLoop, GatewayToolCaller
        from astra.core.permissions import Policy
        from astra.runtime.tools import register_runtime_tools
        from astra.terminal import TerminalManager, register_terminal_tools
        from astra.tools.registry import ToolRegistry
        from tests.helpers import LocalRuntimeStub

        reg = ToolRegistry(policy=Policy(granted=["read", "system_action"]))
        mgr = TerminalManager()
        register_terminal_tools(reg, mgr)
        runtime = LocalRuntimeStub()
        register_runtime_tools(reg, runtime)
        conn = _conn(AstraGatewayOpenRouter, _cfg())
        gateway = AstraAIGateway(connections=[conn])
        loop = AgentToolLoop(reg, terminal=mgr, runtime=runtime, max_steps=3)
        # The Gateway connection IS the loop's brain here, so the mocked
        # upstream must speak the loop's tool protocol.
        side, _seen = _capture([
            _resp(_openai_body(json.dumps({
                "action": "tool", "tool": "runtime_command",
                "args": {"command": "echo loop-ok"}, "thought": "run"}))),
            _resp(_openai_body(json.dumps({"action": "final", "answer": "done"}))),
        ])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            res = loop.run("run echo", GatewayToolCaller(gateway),
                           system_prompt="You are Astra.", session_id="s")
        self.assertTrue(res.ok)
        self.assertEqual(res.tool_calls, 1)
        self.assertIn("loop-ok", json.dumps([s.result for s in res.steps], default=str))
        mgr.close_all()
        runtime.close_all()

    def test_gateway_connections_are_not_providers(self):
        from astra.ai.registry import build_providers
        reg = build_providers(config=_cfg(GEMINI_API_KEYS="k"))
        self.assertIsNone(reg.get("astra_ai_gateway"))
        for cls in GATEWAY_CONNECTIONS:
            self.assertIsNone(reg.get(cls.name))
        # and the Gateway is never a router fallback
        from astra.ai.router import AstraRouter
        gw = AstraAIGateway(config=_cfg(GW_MISTRAL_API_KEYS="k",
                                        GW_MISTRAL_MODELS="mistral-small-2603"))
        r = AstraRouter(providers=[], config=_cfg(), gateway=gw)
        self.assertNotIn("astra_ai_gateway", r.providers)



# ═══════════════════════════════════════════════════════════════════════════
# Bedrock model parity — every Provider BEDROCK_MODELS entry is routable here
# ═══════════════════════════════════════════════════════════════════════════
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env_file_list(key: str) -> list:
    """Read a comma-separated list var straight out of `.env.example`."""
    path = os.path.join(_REPO_ROOT, ".env.example")
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith(key + "="):
                return [m.strip() for m in line.split("=", 1)[1].split(",")
                        if m.strip()]
    return []


def _converse_body(text="ok", usage=None):
    """A minimal Bedrock Converse response body."""
    payload = {"output": {"message": {"content": [{"text": text}]}}}
    if usage is not None:
        payload["usage"] = usage
    return json.dumps(payload).encode()


def _converse_sse(*texts):
    """ConverseStream-style `data:` frames for one text delta each."""
    return "".join(
        "data: " + json.dumps(
            {"contentBlockDelta": {"delta": {"text": t}}}) + "\n\n"
        for t in texts).encode()


class GatewayBedrockModelParityTests(unittest.TestCase):
    """GW_BEDROCK_MODELS must cover every Bedrock model the Provider's
    BEDROCK_MODELS configures — while remaining a separate list."""

    @classmethod
    def setUpClass(cls):
        cls.provider_models = _env_file_list("BEDROCK_MODELS")
        cls.gateway_models = _env_file_list("GW_BEDROCK_MODELS")

    def test_provider_and_gateway_lists_are_non_empty(self):
        self.assertTrue(self.provider_models)
        self.assertTrue(self.gateway_models)

    def test_gateway_covers_every_provider_bedrock_model(self):
        missing = [m for m in self.provider_models
                   if m not in self.gateway_models]
        self.assertEqual(missing, [], f"missing from GW_BEDROCK_MODELS: {missing}")

    def test_existing_gateway_models_are_preserved_in_order(self):
        # the two entries the Gateway shipped with stay, order intact
        self.assertEqual(self.gateway_models[:2],
                         ["us.amazon.nova-lite-v1:0", "amazon.nova-lite-v1:0"])

    def test_no_duplicate_gateway_models(self):
        self.assertEqual(len(self.gateway_models), len(set(self.gateway_models)))

    def test_gateway_and_provider_model_configs_stay_separate(self):
        from astra.ai.adapters.bedrock import BedrockAdapter
        self.assertEqual(BedrockAdapter.models_env, "BEDROCK_MODELS")
        self.assertEqual(AstraGatewayBedrock.models_env, "GW_BEDROCK_MODELS")
        self.assertNotEqual(BedrockAdapter.models_env, AstraGatewayBedrock.models_env)
        # separate values, not the same list object/value
        self.assertNotEqual(self.gateway_models, self.provider_models)


class GatewayBedrockModelRoutingTests(unittest.TestCase):
    """The Gateway can actually build, select and request every added model."""

    def _bedrock_conn(self):
        cfg = _cfg(GW_BEDROCK_API_KEYS="k",
                   GW_BEDROCK_MODELS=",".join(_env_file_list("GW_BEDROCK_MODELS")))
        return AstraGatewayBedrock(config=cfg)

    def test_every_model_builds_a_catalog_entry_with_metadata(self):
        conn = self._bedrock_conn()
        catalog = build_gateway_catalog([conn])
        self.assertEqual([m.model_id for _c, m in catalog], conn.models)
        for _c, model in catalog:
            self.assertEqual(model.provider, "bedrock")
            self.assertIn("chat", model.capabilities)
            self.assertGreater(model.context_window, 0)

    def test_every_model_is_eligible_for_general_requests(self):
        from astra.ai.gateway_routing import GatewayRoutingState
        conn = self._bedrock_conn()
        catalog = build_gateway_catalog([conn])
        state = GatewayRoutingState(None)
        eligible = {m.model_id for _c, m, _h in eligible_targets(
            catalog, state, category="general", context_tokens=0)}
        for mid in conn.models:
            self.assertIn(mid, eligible)

    def test_every_model_is_requested_with_its_own_converse_url(self):
        conn = self._bedrock_conn()
        side, seen = _capture([_resp(_converse_body("ok", {"totalTokens": 3}))
                               for _ in conn.models])
        with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
            for mid in conn.models:
                out = conn.chat([{"role": "user", "content": "hi"}], model=mid)
                self.assertEqual(out, "ok")
                self.assertEqual(conn.last_usage().get("totalTokens"), 3)
        self.assertEqual(len(seen), len(conn.models))
        for mid, call in zip(conn.models, seen):
            self.assertIn(f"/model/{mid}/converse", call["url"])

    def test_every_model_serves_explicit_model_selection_through_gateway(self):
        conn = self._bedrock_conn()
        gw = AstraAIGateway(connections=[conn])
        for mid in conn.models:
            side, _seen = _capture([_resp(_converse_body("hi"))])
            with mock.patch("astra.ai.gateway.urllib.request.urlopen", side):
                out = gw.chat([{"role": "user", "content": "x"}], model=mid)
            self.assertEqual(out, "hi")
            self.assertEqual(gw.last_connection, "astra-gw-bedrock")
            self.assertEqual(gw.last_model, mid)
            self.assertEqual(gw.last_attempts, 1)

    def test_every_model_streams_through_converse_stream(self):
        conn = self._bedrock_conn()
        for mid in conn.models:
            with mock.patch("astra.ai.gateway.urllib.request.urlopen",
                            lambda req, timeout=None: io.BytesIO(
                                _converse_sse("he", "llo"))):
                out = "".join(conn.stream(
                    [{"role": "user", "content": "hi"}], model=mid))
            self.assertEqual(out, "hello", mid)

    def test_added_models_keep_expected_capability_metadata(self):
        conn = self._bedrock_conn()
        meta = {m.model_id: m for _c, m in build_gateway_catalog([conn])}
        # Anthropic (Claude) — tools + vision
        self.assertIn("tools", meta["us.anthropic.claude-opus-4-5"].capabilities)
        self.assertIn("vision", meta["us.anthropic.claude-opus-4-5"].capabilities)
        # DeepSeek — reasoning + coding
        self.assertIn("reasoning", meta["deepseek.v3.1"].capabilities)
        self.assertIn("coding", meta["deepseek.v3.2"].capabilities)
        # Pixtral — vision
        self.assertIn("vision", meta["mistral.pixtral-large-2502-v1:0"].capabilities)
        # Qwen coder — tools
        self.assertIn("tools", meta["qwen.qwen3-coder-480b-a35b-v1:0"].capabilities)
        # Kimi — tools, exposed as supports_tools
        self.assertTrue(meta["moonshotai.kimi-k2.5"].supports_tools)
        # Nova family — tools
        self.assertIn("tools", meta["us.amazon.nova-pro-v1:0"].capabilities)

    def test_tool_capable_added_models_survive_the_tool_use_filter(self):
        from astra.ai.gateway_routing import GatewayRoutingState
        conn = self._bedrock_conn()
        catalog = build_gateway_catalog([conn])
        state = GatewayRoutingState(None)
        eligible = [m for _c, m, _h in eligible_targets(
            catalog, state, category="tool_use", context_tokens=0)]
        ids = {m.model_id for m in eligible}
        self.assertIn("us.anthropic.claude-sonnet-4-5", ids)
        self.assertIn("deepseek.v3.1", ids)
        self.assertIn("moonshotai.kimi-k2.5", ids)
        self.assertIn("qwen.qwen3-coder-480b-a35b-v1:0", ids)
        # every eligible target genuinely declares the hard capability
        for m in eligible:
            self.assertIn("tools", m.capabilities)


if __name__ == "__main__":
    unittest.main()
