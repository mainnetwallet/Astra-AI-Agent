"""Dedicated Image Provider/Model Router.

Required architecture:

    User -> Gateway -> classification -> ImageRouter -> Provider Adapter
    -> Actual Image API -> Artifact/BlobStore -> Chat UI

`AstraAIGateway` is the entry/classification/handoff layer. It must NOT
execute any image-provider HTTP API itself. `ImageRouter` is the single
owner of:

    - the eligible-FREE-image-target list and its deterministic order
      (delegated to `AstraAIGateway.image_targets()`, which only reads the
      curated priority / live-catalog discovery -- it performs no HTTP call
      of its own)
    - per-model capability validation (already enforced by
      `image_targets()` / `astra.ai.image_models`)
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
      - `image_targets(editing=..., discover=...)` -> ranked (conn, model,
        health) triples (selection only -- no HTTP call)
      - `_image_failure_reason(exc)` -> short classified reason string
      - `_emit(kind, **data)` -> event/logging sink

    This router -- not the Gateway -- performs `conn.generate_image(...)`,
    the actual provider HTTP request.
    """

    def __init__(self, gateway):
        self._gw = gateway

    def generate(self, prompt: str, model: str | None = None,
                size: str = "1024x1024", n: int = 1, *,
                editing: bool = False, trace: str = "",
                discover: bool = True) -> str:
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
        category = "image_editing" if editing else "image_generation"
        ranked = gw.image_targets(editing=editing, discover=discover)
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
                continue                      # one attempt per model per request
            attempted.add(key)
            attempts += 1
            gw.last_attempts = attempts
            gw._emit("image.generation.attempt", provider=tmodel.provider,
                     model=tmodel.model_id, attempt=attempts, op=op,
                     trace=trace)
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
                uri = conn.generate_image(prompt, model=tmodel.model_id,
                                          size=size, n=n)
            except Exception as e:
                reason = gw._image_failure_reason(e)
                duration_ms = round((time.perf_counter() - start) * 1000.0, 1)
                status_code = int(getattr(e, "code", 0) or 0)
                failures.append(f"{tmodel.provider}/{tmodel.model_id}: {reason}")
                gw._emit("astra_gateway.error", category=category,
                         provider=tmodel.provider,
                         model=tmodel.model_id, reason=reason,
                         status_code=status_code, duration_ms=duration_ms,
                         attempt=attempts, op=call_op, trace=trace,
                         terminal=True)
                gw._emit("image.generation.failure",
                         provider=tmodel.provider, model=tmodel.model_id,
                         attempt=attempts, duration_ms=duration_ms,
                         reason=reason, failure_category=reason, op=op,
                         trace=trace, terminal=False)
                nxt = next((m for _c, m, _h in ranked[idx + 1:]
                            if (m.provider, m.model_id) not in attempted), None)
                if nxt is not None:
                    gw._emit("image.generation.fallback",
                             provider=tmodel.provider, model=tmodel.model_id,
                             reason=reason, next_provider=nxt.provider,
                             next_model=nxt.model_id, attempt=attempts,
                             op=op, trace=trace)
                continue
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
                     terminal=True)
            return uri
        gw._emit("image.generation.exhausted", provider="", model="",
                 attempts=attempts,
                 reason="; ".join(failures) or "all image models failed",
                 op=op, trace=trace, terminal=True)
        raise ProviderError(IMAGE_EXHAUSTED_MESSAGE)
