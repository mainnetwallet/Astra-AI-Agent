"""AI tools — the "AI / Agent" node of the Agent Workflow canvas.

A workflow step can only run something the `ToolRegistry` actually exposes
(see astra/workflows/engine.py), and the registry had no AI capability at
all: a workflow could browse, shell out, hit web3 or write files, but it
could not *think*. Without this, an "AI / Agent" node in the visual editor
would have been a picture of a feature that does not exist — so this module
registers the real one.

`ai_generate` runs a prompt through the EXISTING AI path and nothing else:

    workflow step -> ToolRegistry -> ai_generate
                  -> AstraRouter.route_request()
                  -> Gateway/Provider (with the router's normal fallback,
                     health tracking and credential rotation)

It is deliberately not a second model client: no HTTP, no provider list, no
key handling of its own. Whatever the router can reach right now, this tool
can reach — and if nothing is configured it fails honestly instead of
returning a placeholder answer.

Registration is separate from `register_builtins` because the router does
not exist when the builtins are registered (see astra/bootstrap.py).
"""
from __future__ import annotations

from astra.core.exceptions import ProviderError, ValidationError
from astra.core.permissions import Level
from astra.tools.schemas import Tool

TOOL_NAME = "ai_generate"

# A workflow step is a bounded unit of work, so an AI node that can hang
# forever would hang the whole run. 180s is far beyond a normal completion
# and still bounded; ToolRegistry enforces it (astra/tools/registry.py).
DEFAULT_TIMEOUT_S = 180.0


def _as_int(value, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError("max_tokens must be an integer")


def build_ai_generate(router, *, default_max_tokens: int | None = None):
    """Return the `ai_generate` callable bound to one live AstraRouter."""

    def ai_generate(args: dict, ctx=None) -> dict:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise ValidationError("prompt required")
        if router is None:
            raise ProviderError(
                "no AI router is wired into this Astra instance — the "
                "workflow AI node needs a configured provider or Gateway")

        from astra.ai.router import RoutingRequest

        system = str(args.get("system") or "").strip()
        provider = str(args.get("provider") or "").strip()
        model = str(args.get("model") or "").strip()
        max_tokens = _as_int(args.get("max_tokens"), default_max_tokens)

        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        req = RoutingRequest(
            task_type="simple_chat",
            messages=messages,
            preferred_provider=provider or None,
            preferred_model=model or None,
            max_tokens=max_tokens,
            # Both halves given = the operator pinned an exact target, so
            # fail honestly rather than silently answering from another
            # model. Only one half given stays a preference (best-effort).
            no_fallback=bool(provider and model),
        )
        rr = router.route_request(req)
        if not rr.ok or not (rr.text or "").strip():
            raise ProviderError(
                rr.error or "the AI provider returned no usable answer")

        return {
            "text": rr.text,
            "provider": rr.provider,
            "model": rr.model,
            "latency_ms": rr.latency_ms,
            "usage": rr.usage or {},
            "attempts": rr.attempts,
            "fallback_used": rr.fallback_used,
            "requested_provider": rr.requested_provider or provider,
            "requested_model": rr.requested_model or model,
            "estimated_cost_usd": rr.estimated_cost_usd,
        }

    ai_generate.__doc__ = (
        "Ask an Astra AI model to produce text for this workflow step. "
        "`prompt` is required and may reference earlier steps with "
        "{{step_id.param}}. `provider`/`model` pin an exact target when both "
        "are given (otherwise they are a best-effort preference); the result "
        "reports the provider/model that actually answered.")
    return ai_generate


def register_ai_tools(registry, router, *, default_max_tokens=None) -> int:
    """Register the AI node's tool on the ONE ToolRegistry. Returns count."""
    if registry is None:
        return 0
    registry.register(Tool(
        name=TOOL_NAME,
        fn=build_ai_generate(router, default_max_tokens=default_max_tokens),
        description=(
            "Ask an Astra AI model (AstraRouter -> Gateway/Provider) to "
            "generate text from a prompt. Workflow AI/Agent node."),
        category="ai",
        risk=Level.READ,
        requires_confirmation=False,
        timeout=DEFAULT_TIMEOUT_S,
        idempotent=False,
        input={
            "prompt": {"type": "string", "required": True},
            "system": {"type": "string", "required": False},
            "provider": {"type": "string", "required": False},
            "model": {"type": "string", "required": False},
            "max_tokens": {"type": "int", "required": False},
        },
        output={"text": {"type": "string"}, "provider": {"type": "string"},
                "model": {"type": "string"}},
        plugin="core",
    ))
    return 1
