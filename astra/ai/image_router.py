"""Dedicated Image Provider/Model Router.

Required architecture:

    User -> Gateway -> classification -> ImageRouter -> Provider Adapter
    -> Actual Image API -> Artifact/BlobStore -> Chat UI

`AstraAIGateway` is the entry/classification/handoff layer. It must NOT
execute any image-provider HTTP API itself. `ImageRouter` is the single
owner of:

    - the eligible-FREE-image-target list and its deterministic order
      (`ImageRouter._catalog()` / `.build_targets()` -- reads the canonical
      `*_IMAGE_MODELS` env vars, checks dedicated `IMAGE_*` credentials and
      the curated priority / live-catalog discovery; performs no HTTP call
      of its own)
    - per-model capability validation (enforced by `build_targets()` /
      `astra.ai.image_models`)
    - provider adapter dispatch (`conn.generate_image(...)`)
    - the actual image-generation HTTP request
    - serial fallback (one attempt per model, stop on first success)
    - image-specific error classification
    - the image API call lifecycle logging (`astra_gateway.request` /
      `.success` / `.error`, `image.generation.*`)

`AstraAIGateway.generate_image()` is kept only as a thin, backward
compatible delegating wrapper for existing callers that still hold a
Gateway reference directly -- it performs classification/handoff to this
router and nothing else. New callers (e.g. `ChatPipeline`) should call
`gateway.image_router.generate(...)` directly so the execution boundary is
explicit and testable: the Gateway object itself is never asked to reach a
provider image API.
"""
from __future__ import annotations

import time

from astra.core.events import new_op_id
from astra.core.exceptions import ProviderError


class ImageRouter:
    """Owns image-provider selection, dispatch and serial fallback.

    Constructed around a Gateway-like object that supplies:
      - `connections`, `config`, `routing_state` -> raw ingredients this
        router reads to build the eligible/ranked (conn, model, health)
        triples itself (`_catalog()` / `build_targets()`); the Gateway
        performs no selection of its own
      - `_image_failure_reason(exc)` -> short classified reason string
      - `_emit(kind, **data)` -> event/logging sink

    This router -- not the Gateway -- performs `conn.generate_image(...)`,
    the actual provider HTTP request.
    """

    def __init__(self, gateway):
        self._gw = gateway

    # -- image model pool ownership ------------------------------------------
    #
    # Moved here from `AstraAIGateway._image_catalog` / `.image_targets`
    # (the Gateway now keeps only thin, backward-compatible delegating
    # wrappers of the same names -- see astra/ai/gateway.py). This router,
    # not the Gateway, is the single owner of: reading the canonical
    # `*_IMAGE_MODELS` env vars (via each connection's own
    # `image_models_env`), checking dedicated `IMAGE_*` credentials (via
    # each connection's own image pool / `list_image_models` /
    # `live_image_models`), building the eligible provider/model pool, and
    # applying the image priority order.
    def _catalog(self, *, discover: bool = True) -> list:
        """(connection, Model) for every image-generation model the live
        connections can actually serve. OpenRouter merges its official
        discovery API; every other connection uses its configured list.
        Only models the evidence registry recognizes are included."""
        from astra.ai.gateway_routing import GATEWAY_PROVIDER_SHORT
        from astra.ai.image_models import (FREE_TRUE, PROTOCOL_OPENROUTER_IMAGES,
                                           documented_image_models,
                                           image_spec, make_image_spec)
        from astra.ai.models import Model, metadata_for
        out = []
        for conn in self._gw.connections:
            short = GATEWAY_PROVIDER_SHORT.get(getattr(conn, "name", ""),
                                               getattr(conn, "name", ""))
            mids = []
            fn = getattr(conn, "list_image_models", None)
            if callable(fn):
                try:
                    mids = fn(discover=discover) or []
                except Exception:
                    mids = []
            if not mids:
                mids = list(getattr(conn, "image_models", None) or [])
            if not mids:
                # No explicit env list for this connection: fall back to the
                # registry's documented FREE image models for this provider,
                # so the configured pool is used out of the box. A non-empty
                # env list always wins (requirement: configurable without
                # source edits).
                mids = list(documented_image_models(short))
            # A provider's LIVE discovery response is authoritative for the
            # exact image model ids it returns, even when the static registry
            # has no entry yet (spec section 11: OpenRouter).
            live = set()
            live_fn = getattr(conn, "live_image_models", None)
            if discover and callable(live_fn):
                try:
                    live = set(live_fn(discover=discover) or [])
                except Exception:
                    live = set()
            for mid in mids:
                spec = image_spec(short, mid)
                if spec is None and mid in live:
                    # Live-discovered ids are only ever accepted when the
                    # provider's own API reported them as FREE image models
                    # (OpenRouter's ":free" variants); otherwise a paid model
                    # could sneak into the free pool.
                    spec = make_image_spec(
                        short, mid, PROTOCOL_OPENROUTER_IMAGES,
                        capabilities=("image_generation",),
                        input_modalities=("text", "image"),
                        free_tier=FREE_TRUE,
                        free_evidence="provider live catalog: free image output")
                if spec is None or not spec.is_free:
                    continue
                meta = metadata_for(mid, short, image_spec_override=spec)
                meta.pop("provider", None)
                if "image" not in (meta.get("output_modalities") or []):
                    continue
                out.append((conn, Model(short, mid, **meta)))
        return out

    def build_targets(self, *, editing: bool = False,
                      operation: str | None = None,
                      discover: bool = True) -> list:
        """The eligible FREE image targets, in their deterministic serial
        order.

        Deliberately NO health filter and NO health-based ranking: image
        generation has no proactive health check, so a model is only ever
        considered "unavailable" after the real generation request for the
        current user turn actually failed. The order comes from
        astra.ai.image_models' curated priority, overridable per deployment
        with the CANONICAL ``GW_IMAGE_GENERATION_PRIORITY``. The legacy
        ``IMAGE_GENERATION_PRIORITY`` (no ``GW_`` prefix) is consulted only
        when the canonical var is unset/empty -- a documented backward-
        compatibility fallback, not a second independently-configurable
        priority list.
        """
        from astra.ai.gateway_routing import (
            eligible_image_generation_targets, rank_image_targets)
        from astra.ai.image_models import (GATEWAY_IMAGE_PRIORITY_ENV,
                                           IMAGE_PRIORITY_ENV)
        gw = self._gw
        catalog = self._catalog(discover=discover)
        category = operation or ("image_editing" if editing else "image_generation")
        targets = eligible_image_generation_targets(
            catalog, gw.routing_state, category=category)
        preferred = []
        if gw.config is not None:
            try:
                preferred = (gw.config.getlist(GATEWAY_IMAGE_PRIORITY_ENV)
                             or gw.config.getlist(IMAGE_PRIORITY_ENV))
            except Exception:
                preferred = []
        return rank_image_targets(targets, preferred_ids=preferred)

    def generate(self, prompt: str, model: str | None = None,
                size: str = "1024x1024", n: int = 1, *,
                editing: bool = False, source_image: dict | None = None,
                mask_image: dict | None = None, operation: str | None = None,
                trace: str = "", discover: bool = True) -> str:
        """Generate one image with a SIMPLE SERIAL FALLBACK.

        The eligible FREE image models are tried one after another in the
        deterministic priority order. Each model gets at most ONE attempt
        per request (an in-request attempted set), and the ACTUAL
        generation call -- never a proactive health probe -- decides
        success/failure. No health/cooldown state is written, so a failure
        here never disables the model for a later request.

        Returns a `data:` URI on the first success and stops immediately,
        or raises `ProviderError(IMAGE_EXHAUSTED_MESSAGE)` when every
        eligible FREE model failed. It NEVER falls back to a paid image
        model, a text model, a vision-only model or simple_chat.
        """
        from astra.ai.gateway import _gw_log_cap
        from astra.ai.image_models import IMAGE_EXHAUSTED_MESSAGE

        gw = self._gw
        op = new_op_id()
        category = operation or ("image_editing" if editing else "image_generation")
        if category in ("image_editing", "image_inpainting") and not (
                source_image and source_image.get("storage_path")):
            raise ProviderError("Image editing requires a reusable source image.")
        if category == "image_inpainting" and not (
                mask_image and mask_image.get("storage_path")):
            raise ProviderError("Inpainting requires a reusable mask image.")
        ranked = self.build_targets(operation=category, discover=discover)
        if model:
            ranked = ([t for t in ranked if t[1].model_id == model] +
                      [t for t in ranked if t[1].model_id != model])
        gw.last_attempts = 0
        gw.last_connection = ""
        gw.last_model = ""
        gw._emit("image.generation.start", category=category,
                 candidates=len(ranked),
                 targets=[t[1].model_id for t in ranked], op=op,
                 trace=trace, input=_gw_log_cap(prompt))
        if not ranked:
            gw._emit("image.generation.exhausted", provider="", model="",
                     attempts=0, reason="no eligible FREE image model",
                     op=op, trace=trace, terminal=True)
            raise ProviderError(
                "No image-generation model is currently configured or available.")
        attempted = set()
        failures = []
        attempts = 0
        for idx, (conn, tmodel, _health) in enumerate(ranked):
            key = (tmodel.provider, tmodel.model_id)
            if key in attempted:
                continue                      # one attempt-SET per model per request
            attempted.add(key)
            # Same-model, different-key retry: rate limiting is a PER-KEY
            # problem (each key in `conn.image_pool` has its own health/
            # cooldown -- see astra.ai.credentials.CredentialPool). By the
            # time a 429 exception reaches us, `_classify_http()` has
            # already cooled THAT key down, so `img_pool.healthy_count`
            # already excludes it -- if it's still >= 1, another key is
            # ready right now. Retry this SAME model immediately (the
            # pool's own `pick()` transparently hands out the next healthy
            # key); only fall through to a DIFFERENT model once every key
            # for this one has been tried (bounded by the pool's own key
            # count, so a provider where every key is genuinely rate-
            # limited still moves on instead of looping).
            img_pool = getattr(conn, "image_pool", None)
            max_key_tries = max(1, getattr(img_pool, "count", 1) or 1)
            key_try = 0
            succeeded = False
            last_reason = ""
            while key_try < max_key_tries:
                key_try += 1
                attempts += 1
                gw.last_attempts = attempts
                gw._emit("image.generation.attempt", provider=tmodel.provider,
                         model=tmodel.model_id, attempt=attempts,
                         key_attempt=key_try, op=op, trace=trace)
                # Report the ACTUAL provider API request on the SAME existing
                # "astra_gateway.*" API-call contract chat/text calls use, from
                # the exact execution point (`conn.generate_image` -> the
                # provider's HTTP API), now owned by ImageRouter rather than the
                # Gateway. Each attempt gets its own `op`, so a fallback chain
                # shows one START/terminal pair PER attempted model instead of a
                # single request-level row. The `image.generation.*` lifecycle
                # events above stay unchanged.
                call_op = new_op_id()
                # `category` is carried on ALL THREE of this call's events
                # (request/success/error), not just the START. The Activity Log
                # (static/js/log_model.js) uses it to render this row as
                # "Image API Call" instead of the generic "Gateway
                # call"/"Gateway error" a plain chat/text completion gets on the
                # same astra_gateway.* contract -- omitting it on the
                # success/error side (as this used to) left the SUCCESS/FAILED
                # rows mislabeled even though the START row was correct.
                gw._emit("astra_gateway.request", category=category,
                         provider=tmodel.provider, model=tmodel.model_id,
                         attempt=attempts, candidates=len(ranked),
                         op=call_op, trace=trace, input=_gw_log_cap(prompt))
                start = time.perf_counter()
                try:
                    uri = conn.generate_image(
                        prompt, model=tmodel.model_id, size=size, n=n,
                        source_image=source_image, mask_image=mask_image)
                except Exception as e:
                    cred = (img_pool.last_key()
                            if img_pool is not None and hasattr(img_pool, "last_key")
                            else None)
                    reason = gw._image_failure_reason(e)
                    duration_ms = round((time.perf_counter() - start) * 1000.0, 1)
                    status_code = int(getattr(e, "code", 0) or 0)
                    last_reason = reason
                    failures.append(f"{tmodel.provider}/{tmodel.model_id}: {reason}")
                    gw._emit("astra_gateway.error", category=category,
                             provider=tmodel.provider,
                             model=tmodel.model_id, reason=reason,
                             status_code=status_code, duration_ms=duration_ms,
                             attempt=attempts, op=call_op, trace=trace,
                             terminal=True,
                             key_id=cred.key_id if cred else "",
                             key_label=cred.label if cred else "")
                    gw._emit("image.generation.failure",
                             provider=tmodel.provider, model=tmodel.model_id,
                             attempt=attempts, duration_ms=duration_ms,
                             reason=reason, failure_category=reason, op=op,
                             trace=trace, terminal=False,
                             key_id=cred.key_id if cred else "",
                             key_label=cred.label if cred else "")
                    rate_limited = status_code == 429 or "rate limit" in reason
                    more_keys = bool(img_pool) and img_pool.healthy_count >= 1
                    if rate_limited and more_keys:
                        gw._emit("image.generation.key_retry",
                                 provider=tmodel.provider, model=tmodel.model_id,
                                 reason=reason, attempt=attempts, op=op,
                                 trace=trace,
                                 key_id=cred.key_id if cred else "",
                                 key_label=cred.label if cred else "")
                        continue   # SAME model -- pool hands out the next key
                    break          # give up on this model's keys entirely
                else:
                    succeeded = True
                    break
            if succeeded:
                latency_ms = (time.perf_counter() - start) * 1000.0
                gw.last_connection = conn.name
                gw.last_model = tmodel.model_id
                gw._emit("astra_gateway.success", category=category,
                         provider=tmodel.provider,
                         model=tmodel.model_id, status_code=200,
                         latency_ms=round(latency_ms, 1),
                         duration_ms=round(latency_ms, 1), attempt=attempts,
                         op=call_op, trace=trace, terminal=True,
                         output=f"<image data URI: {len(uri)} chars>")
                gw._emit("image.generation.success", provider=tmodel.provider,
                         model=tmodel.model_id, attempt=attempts,
                         duration_ms=round(latency_ms, 1),
                         latency_ms=round(latency_ms, 1), op=op, trace=trace,
                         terminal=True,
                         key_id=(img_pool.last_key().key_id
                                 if img_pool is not None and img_pool.last_key() else ""),
                         key_label=(img_pool.last_key().label
                                    if img_pool is not None and img_pool.last_key() else ""))
                return uri
            # Every key for this model is exhausted -- move to the next
            # DIFFERENT model, exactly as before.
            nxt = next((m for _c, m, _h in ranked[idx + 1:]
                        if (m.provider, m.model_id) not in attempted), None)
            if nxt is not None:
                gw._emit("image.generation.fallback",
                         provider=tmodel.provider, model=tmodel.model_id,
                         reason=last_reason, next_provider=nxt.provider,
                         next_model=nxt.model_id, attempt=attempts,
                         op=op, trace=trace)
            continue
        gw._emit("image.generation.exhausted", provider="", model="",
                 attempts=attempts,
                 reason="; ".join(failures) or "all image models failed",
                 op=op, trace=trace, terminal=True)
        raise ProviderError(IMAGE_EXHAUSTED_MESSAGE)
