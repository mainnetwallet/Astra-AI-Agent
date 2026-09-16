"""Central model registry for Astra.

Every model carries rich metadata (context window, capabilities, cost/quality
classes …) so the AstraRouter can score candidate routes instead of hard-coding
ordering assumptions. The registry is seeded from the configured provider model
lists (env / config.json / .env) with sensible per-family metadata defaults,
then refined by discovery and usage. It is a *resource* catalog — it is NOT a
provider and is NOT AstraRouter.
"""
from __future__ import annotations

import re

from astra.core.config import Config

# families: (name, base_provider, caps, context, quality, cost_speed)
# quality: high|mid|fast   cost_class: cheap|mid|premium
_FAMILIES: dict[str, tuple[str, list[str], int, str, str, list[str]]] = {
    "claude":   ("bedrock", ["chat", "tools", "json", "vision", "reasoning", "coding"], 200000, "high", "premium", ["stream"]),
    "gemini":   ("gemini",  ["chat", "tools", "json", "vision", "reasoning"], 1048576, "mid",  "cheap",   ["stream"]),
    "nova":     ("bedrock", ["chat", "tools", "json"], 200000, "mid", "cheap", ["stream"]),
    "gpt":      ("groq",    ["chat", "tools", "json", "coding"], 160000, "high", "mid",   ["stream"]),
    "openai":   ("bedrock", ["chat", "tools", "json", "coding", "vision"], 400000, "high", "premium", ["stream"]),
    "qwen":     ("groq",    ["chat", "tools", "json", "coding"], 200000, "mid",  "cheap", ["stream"]),
    "glm":      ("zai",     ["chat", "tools", "json"], 200000, "mid", "cheap", ["stream"]),
    "minimax":  ("openrouter", ["chat", "tools", "json"], 200000, "mid", "mid", ["stream"]),
    "kimi":     ("moonshotai", ["chat", "tools", "json", "reasoning"], 200000, "high", "mid", ["stream"]),
    "kling":    ("moonshotai", ["chat"], 128000, "mid", "mid", []),
    "nemotron": ("openrouter", ["chat", "tools", "json", "reasoning"], 260000, "mid", "mid", ["stream"]),
    "grok":     ("xai",     ["chat", "tools", "json", "reasoning"], 200000, "high", "premium", ["stream"]),
    "devstral": ("mistral", ["chat", "tools", "json", "coding"], 200000, "mid", "mid", ["stream"]),
    "pixtral":  ("mistral", ["chat", "tools", "json", "vision"], 128000, "mid", "mid", ["stream"]),
    "mistral":  ("mistral", ["chat", "tools", "json"], 128000, "mid", "cheap", ["stream"]),
    "ministral":("mistral", ["chat", "tools", "json"], 128000, "mid", "cheap", ["stream"]),
    "codestral":("mistral", ["chat", "tools", "json", "coding"], 256000, "high", "premium", ["stream"]),
    "deepseek": ("deepseek", ["chat", "tools", "json", "reasoning", "coding"], 128000, "high", "cheap", ["stream"]),
    "llama":    ("cloudflare", ["chat", "tools", "json"], 128000, "mid", "cheap", ["stream"]),
    "gemma":    ("cloudflare", ["chat", "tools", "json"], 128000, "mid", "cheap", ["stream"]),
    "sea-lion": ("aisingapore", ["chat"], 64000, "mid", "cheap", []),
    "granite":  ("ibm-granite", ["chat", "tools"], 64000, "mid", "cheap", []),
    "command":  ("cohere", ["chat", "tools", "json", "vision", "translation"], 128000, "high", "mid", ["stream"]),
    "aya":      ("cohere", ["chat", "tools", "json", "translation"], 128000, "mid", "cheap", []),
    "laguna":   ("poolside", ["chat", "json"], 128000, "mid", "cheap", []),
    "dots":     ("dots-studio", ["chat"], 128000, "mid", "cheap", []),
    "ling":     ("inclusionai", ["chat"], 64000, "mid", "cheap", []),
    "lfm":      ("liquid", ["chat"], 64000, "mid", "cheap", []),
    "mercury":  ("xai", ["chat", "tools"], 200000, "high", "premium", ["stream"]),
    "nova-lite":("bedrock", ["chat", "tools", "json"], 200000, "mid", "cheap", ["stream"]),
}

_FAST_WORDS = ("flash", "lightning", "lite", "nano", "small", "mini", "micro",
               "speedy", "scarlet", "sapphire", "amber", "gray", "swift")
_HIGH_WORDS = ("opus", "pro", "sonnet", "reasoning", "ultra", "max", "large",
               "super", "master", "k2", "k2.5", "m3", "top")

# how provider list vars map to the canonical provider name used by adapters
PROVIDER_VAR = {
    "gemini": "GEMINI_MODELS",
    "groq": "GROQ_MODELS",
    "mistral": "MISTRAL_MODELS",
    "openrouter": "OPENROUTER_MODELS",
    "cerebras": "CEREBRAS_MODELS",
    "cloudflare": "CLOUDFLARE_MODELS",
    "sambanova": "SAMBA_MODELS",
    "cohere": "COHERE_MODELS",
    "zai": "ZAI_MODELS",
    "bedrock": "BEDROCK_MODELS",
}


def _tokens_from_context(context_window: int) -> int:
    return int(context_window)


class Model:
    """One AI model resource. Immutable-ish metadata, mutable status."""
    __slots__ = ("provider", "model_id", "display_name", "capabilities", "context_window",
                 "input_modalities", "output_modalities", "supports_streaming",
                 "supports_tools", "supports_json", "supports_vision",
                 "reasoning_level", "speed_class", "quality_class", "cost_class",
                 "availability", "preferred", "disabled")

    def __init__(self, provider: str, model_id: str, *, display_name: str = "",
                 capabilities: list[str] | None = None, context_window: int = 128000,
                 input_modalities: list[str] | None = None, output_modalities: list[str] | None = None,
                 supports_streaming: bool = True, supports_tools: bool = False,
                 supports_json: bool = False, supports_vision: bool = False,
                 reasoning_level: str = "auto", speed_class: str = "mid",
                 quality_class: str = "mid", cost_class: str = "mid",
                 availability: str = "configured", preferred: bool = False,
                 disabled: bool = False):
        self.provider = provider
        self.model_id = model_id
        self.display_name = display_name or model_id
        self.capabilities = list(capabilities or ["chat"])
        self.context_window = context_window
        self.input_modalities = list(input_modalities or ["text"])
        self.output_modalities = list(output_modalities or ["text"])
        self.supports_streaming = supports_streaming
        self.supports_tools = supports_tools
        self.supports_json = supports_json
        self.supports_vision = supports_vision
        self.reasoning_level = reasoning_level
        self.speed_class = speed_class or "mid"
        self.quality_class = quality_class or "mid"
        self.cost_class = cost_class or "mid"
        self.availability = availability
        self.preferred = preferred
        self.disabled = disabled

    # -- capability helpers ---------------------------------------------------
    def has(self, cap: str) -> bool:
        return cap in self.capabilities

    @property
    def cost_multiplier(self) -> float:
        return {"cheap": 1.0, "mid": 3.0, "premium": 9.0}[self.cost_class]

    def to_dict(self) -> dict:
        return {
            "provider": self.provider, "model": self.model_id,
            "display_name": self.display_name, "capabilities": list(self.capabilities),
            "context_window": self.context_window,
            "input_modalities": list(self.input_modalities),
            "output_modalities": list(self.output_modalities),
            "supports_streaming": self.supports_streaming,
            "supports_tools": self.supports_tools, "supports_json": self.supports_json,
            "supports_vision": self.supports_vision, "reasoning_level": self.reasoning_level,
            "speed_class": self.speed_class, "quality_class": self.quality_class,
            "cost_class": self.cost_class, "availability": self.availability,
            "preferred": self.preferred, "disabled": self.disabled,
        }


def _family_of(model_id: str) -> str:
    """Pick the metadata family that best matches a model id string."""
    low = model_id.lower()
    for key, _info in sorted(_FAMILIES.items(), key=lambda kv: -len(kv[0])):
        if key in low:
            return key
    return ""


def metadata_for(model_id: str, provider: str | None = None) -> dict:
    """Derive metadata for a model id from family heuristics.

    `provider`, when given, is the real adapter/provider identity — the
    ProviderRegistry entry that will actually serve requests for this model
    (e.g. "openrouter" for a deepseek/kimi/qwen model routed through
    OpenRouter). It is always authoritative for the returned "provider"
    field. Family metadata (`_FAMILIES`) is a *capability/quality* hint
    table only, keyed by a substring match on the model id — it must never
    be allowed to overwrite who actually owns/serves the model. The
    family's own `base_provider` is used as a provider guess only when the
    caller did not supply a real one (e.g. building a catalog entry with no
    adapter context yet).

    Capability inference is conservative: unknown-family models start from
    a bare "chat" capability rather than assuming tool/JSON support, and
    vision is only inferred from fairly specific signals. Explicit
    overrides passed by the caller (e.g. via ModelRegistry.add(**kw)) always
    win over anything derived here, since those are applied on top of this
    dict's keys.
    """
    fam = _family_of(model_id)
    info = _FAMILIES.get(fam)
    if info:
        base_provider, caps, ctx, q, cost, _mods = info
        caps = list(caps)
    else:
        base_provider = None
        # Unknown family → conservative fallback. Do NOT assume tools/json
        # support just because most families happen to have them; that
        # would falsely grant capabilities to a model we know nothing
        # about. Callers that know more can override via explicit kwargs.
        caps, ctx, q, cost = ["chat"], 128000, "mid", "cheap"
    low = model_id.lower()
    # Vision inference stays narrow: a bare "vl" substring is too easy to
    # false-positive on unrelated model ids, so it's only honoured as a
    # distinct token (e.g. "...-vl-...", "qwen2-vl"), not any substring.
    vision = bool(
        "vision" in low
        or "pixtral" in low
        or re.search(r"(?:^|[-_/])vl(?:[-_]|$)", low)
        or ("gpt" in low and "4o" in low)
        or "vision" in caps
    )
    if vision and "vision" not in caps:
        caps.append("vision")
    if "flash" in low or "lightning" in low or any(w in low for w in
                                                   ("-lite", "nano", "small", "-mini", "micro", "-8b", "-20b", "-31b", "-reka")):
        q = "fast"; cost = "cheap"
    if any(w in low for w in ("opus", "pro", "sonnet", "reasoning", "ultra", "-120b", "k2.5", "m3")):
        q = "high"; cost = "premium" if fam not in ("deepseek",) else cost
    # JSON/structured-output support: only claimed for a verified family
    # that lists "json" and isn't a fast/lite variant (those are more
    # likely to drop strict structured-output adherence under load).
    struct = bool(info) and "json" in caps and "flash" not in low and "lite" not in low
    return {
        "provider": provider or base_provider or fam or "unknown",
        "capabilities": caps, "context_window": ctx,
        "quality_class": q, "cost_class": cost,
        "supports_vision": vision, "supports_json": bool(struct),
        "supports_tools": "tools" in caps,
    }


class ModelRegistry:
    """Holds the universe of known models keyed by (provider, model_id).

    Seed order: env/config list vars per provider, then explicit registry.add()
    from discovery. NOT a routing decision-maker — the AstraRouter consumes it.
    """

    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self._models: dict[str, dict[str, Model]] = {}
        self._categories: dict[str, str] = {}
        self._seed_from_config()

    # -- seeding --------------------------------------------------------------
    def _seed_from_config(self) -> None:
        for provider, var in PROVIDER_VAR.items():
            for mid in self.config.getlist(var, default=[]):
                self.add(provider, mid)

    def add(self, provider: str, model_id: str, **kw) -> Model:
        meta = metadata_for(model_id, provider)
        meta.pop("provider", None)         # provider comes from the position
        m = Model(provider, model_id, **{**meta, **kw})
        self._models.setdefault(provider, {})[model_id] = m
        return m

    def update_status(self, provider: str, model_id: str, *, preferred: bool | None = None,
                      disabled: bool | None = None, availability: str | None = None) -> Model | None:
        m = self.get(provider, model_id)
        if not m:
            return None
        if preferred is not None:
            for other in self.all_models():
                other.preferred = False
            m.preferred = preferred
        if disabled is not None:
            m.disabled = disabled
        if availability is not None:
            m.availability = availability
        return m

    # -- queries --------------------------------------------------------------
    def get(self, provider: str, model_id: str) -> Model | None:
        return self._models.get(provider, {}).get(model_id)

    def providers(self) -> list[str]:
        return sorted(self._models)

    def for_provider(self, provider: str) -> list[Model]:
        return list(self._models.get(provider, {}).values())

    def all_models(self) -> list[Model]:
        out = []
        for provider in self._models:
            out.extend(self._models[provider].values())
        return out

    def filter(self, *, capabilities: list[str] | None = None,
               providers: list[str] | None = None, status: str | None = None,
               context_min: int | None = None) -> list[Model]:
        caps = set(capabilities or [])
        out = []
        for m in self.all_models():
            if providers and m.provider not in providers:
                continue
            if caps and not caps.issubset(m.capabilities):
                continue
            if status and m.availability != status:
                continue
            if context_min and m.context_window < context_min:
                continue
            out.append(m)
        return out

    def require_capabilities(self, capabilities: list[str]) -> list[Model]:
        return self.filter(capabilities=capabilities)

    def have_provider(self, provider: str) -> bool:
        return bool(self._models.get(provider))

    def count(self) -> int:
        return sum(len(v) for v in self._models.values())

    def status_summary(self) -> dict:
        models = self.all_models()
        return {
            "total": len(models),
            "preferred": sum(1 for m in models if m.preferred),
            "disabled": sum(1 for m in models if m.disabled),
            "by_provider": {p: len(v) for p, v in self._models.items()},
        }