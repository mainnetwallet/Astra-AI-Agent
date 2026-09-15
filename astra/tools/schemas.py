"""Tool descriptions + input validation for the universal tool registry.

A `Tool` is a callable with a JSON-ish schema, a permission level and a
confirmation flag. `ToolRegistry` turns these into safe, audited calls.
"""
from __future__ import annotations

import inspect

from astra.core.exceptions import ValidationError
from astra.core.permissions import Level


def _type_of(spec) -> str:
    if isinstance(spec, str):
        return spec
    t = getattr(spec, "type", None) or "any"
    return t


class Tool:
    def __init__(self, name: str, fn, *,
                 description: str = "", category: str = "general",
                 input: dict | None = None, output: dict | None = None,
                 risk: int = Level.READ, requires_confirmation: bool = False,
                 plugin: str = ""):
        self.name = name
        self.fn = fn
        self.description = description
        self.category = category
        self.input = input or {}
        self.output = output or {}
        self.risk = risk
        self.requires_confirmation = requires_confirmation
        self.plugin = plugin

    # -- schema ------------------------------------------------------------
    def satisfies(self, args: dict) -> None:
        """Validate args against the input schema (raises ValidationError)."""
        for pname, spec in self.input.items():
            required = bool(spec.get("required", False)) if isinstance(spec, dict) else False
            present = pname in args and args[pname] not in (None, "")
            if required and not present:
                raise ValidationError(f"tool '{self.name}': missing required "
                                      f"argument '{pname}'")
            if present and isinstance(spec, dict) and "type" in spec:
                self._check_type(pname, args[pname], spec["type"])
        # reject unknown args? no — tools accept kwargs; only enforce required

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

    def describe(self) -> dict:
        return {"name": self.name, "description": self.description,
                "category": self.category, "input_schema": self.input,
                "output_schema": self.output,
                "risk_level": Level.NAMES.get(self.risk, "read"),
                "requires_confirmation": self.requires_confirmation,
                "plugin": self.plugin}