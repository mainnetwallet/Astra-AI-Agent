"""Tool descriptions + input validation for the universal tool registry.

A `Tool` is a callable with a JSON-ish schema, a permission level and a
confirmation flag. `ToolRegistry` turns these into safe, audited calls.
"""
from __future__ import annotations

from astra.core.exceptions import ValidationError
from astra.core.permissions import Level


class Tool:
    def __init__(self, name: str, fn, *,
                 description: str = "", category: str = "general",
                 input: dict | None = None, output: dict | None = None,
                 risk: int = Level.READ, requires_confirmation: bool = False,
                 timeout: float = 0.0, retries: int = 0,
                 retry_backoff_s: float = 1.0, idempotent: bool = False,
                 supports_async: bool = False, rate_limit_per_min: int = 0,
                 strict: bool = False, plugin: str = "",
                 confirmation_delegate: str = ""):
        self.name = name
        self.fn = fn
        self.description = description
        self.category = category
        self.input = input or {}
        self.output = output or {}
        self.risk = risk
        self.requires_confirmation = requires_confirmation
        # Non-empty only for tools (e.g. `tx_prepare`) that own a further
        # deterministic confirm/allow/block gate downstream of this generic
        # permission layer — see astra.core.permissions.Policy.decision.
        self.confirmation_delegate = confirmation_delegate
        self.timeout = float(timeout or 0)             # seconds; 0 = none
        self.retries = max(0, int(retries or 0))
        self.retry_backoff_s = float(retry_backoff_s or 1.0)
        self.idempotent = bool(idempotent)             # safe to retry blindly
        self.supports_async = bool(supports_async)
        self.rate_limit_per_min = max(0, int(rate_limit_per_min or 0))
        self.strict = bool(strict)                     # reject unknown args
        self.plugin = plugin

    # -- schema ------------------------------------------------------------
    def satisfies(self, args: dict) -> None:
        """Validate args against the input schema (raises ValidationError).

        Strict mode (opt-in) rejects undeclared arguments — a tool that
        declares a schema is a contract, and passing something it can't use
        is a planning bug we want caught loudly.
        """
        for pname, spec in self.input.items():
            required = bool(spec.get("required", False)) if isinstance(spec, dict) else False
            present = pname in args and args[pname] not in (None, "")
            if required and not present:
                raise ValidationError(f"tool '{self.name}': missing required "
                                      f"argument '{pname}'")
            if present and isinstance(spec, dict) and "type" in spec:
                self._check_type(pname, args[pname], spec["type"])
        if self.strict:
            declared = set(self.input)
            if declared:
                extra = set(args) - declared
                if extra:
                    raise ValidationError(
                        f"tool '{self.name}': unknown argument(s) "
                        f"{sorted(extra)} — declaration only accepts "
                        f"{sorted(declared)}")

    def validates_output(self, result) -> None:
        """Optional output contract check (raises ValidationError). Only
        checks declared keys; extra result keys are allowed."""
        for oname, spec in self.output.items():
            if isinstance(spec, dict) and spec.get("type"):
                if oname in result:
                    self._check_type(oname, result[oname], spec["type"])

    @staticmethod
    def _check_type(name: str, value, t: str) -> None:
        bad = False
        if t == "string":
            bad = not isinstance(value, str)
        elif t == "int":
            bad = not isinstance(value, int) or isinstance(value, bool)
        elif t in ("number", "float"):
            bad = not isinstance(value, (int, float)) or isinstance(value, bool)
        elif t == "bool":
            bad = not isinstance(value, bool)
        elif t == "list":
            bad = not isinstance(value, list)
        elif t == "dict":
            bad = not isinstance(value, dict)
        if bad:
            raise ValidationError(f"tool argument '{name}' must be {t}, got "
                                  f"{type(value).__name__}")

    def _source(self) -> tuple[str, str]:
        """(repo-relative file, callable name) for the registered function.

        Introspection only — this is what lets the Agent Workflow node
        inspector show the real implementation behind a tool instead of a
        guess. Guarded so a builtin, a closure-bound terminal tool and a
        `functools.partial` all describe themselves instead of raising.
        `_wrap` (browser/) and `_bind` (terminal/) set `fn.__name__` back to
        the real method, so the reported name is the implementation's, not
        "wrapper"."""
        import os
        name = getattr(self.fn, "__name__", "") or ""
        try:
            import inspect
            src = inspect.getsourcefile(self.fn) or ""
        except Exception:
            src = ""
        if src:
            try:
                root = os.path.dirname(os.path.dirname(
                    os.path.dirname(os.path.abspath(__file__))))
                rel = os.path.relpath(os.path.abspath(src), root)
                if not rel.startswith(".."):
                    src = rel.replace(os.sep, "/")
            except Exception:
                pass
        return src, name

    def describe(self) -> dict:
        module, function = self._source()
        return {"name": self.name, "description": self.description,
                "category": self.category, "input_schema": self.input,
                "output_schema": self.output,
                "risk_level": Level.NAMES.get(self.risk, "read"),
                "requires_confirmation": self.requires_confirmation,
                "confirmation_delegate": self.confirmation_delegate,
                "timeout_s": self.timeout, "retries": self.retries,
                "retry_backoff_s": self.retry_backoff_s,
                "idempotent": self.idempotent,
                "supports_async": self.supports_async,
                "rate_limit_per_min": self.rate_limit_per_min,
                "strict": self.strict, "plugin": self.plugin,
                "module": module, "function": function}
