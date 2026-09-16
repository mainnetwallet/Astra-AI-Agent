"""Deterministic result validation (Gateway spec §9-10).

A tool/provider call can raise (already classified by `classification.py`)
or it can return `ok=True` while the *claim* it makes doesn't hold up —
"file created" when the file doesn't exist, a required output field simply
absent. This module never trusts a natural-language "done" claim when
deterministic, checkable state contradicts it, and it never invents a
semantic judgement of its own: with nothing declared to check, a result is
`valid` by default, so every existing step that declares neither `verify`
nor `expect_file` nor `expect_nonempty` behaves exactly as before.

A step opts into a check by declaring it in the plan step dict:
    {"verify": ["output.path"]}                       # already existed
    {"expect_file": "output.path"}                     # NEW: path must exist
    {"expect_nonempty": "output.text"}                 # NEW: must be non-blank

Only deterministic checks live here. Semantic ("does this text actually
answer the question") validation is intentionally out of scope — the spec
asks for it only when deterministic verification is insufficient, and nothing
in this repository provides real semantic-verification infrastructure today
(see the FINAL MASTER FIX PROMPT engineering report: adding a fake/guessed
semantic check would be worse than not having one).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class ValidationOutcome:
    status: str            # "valid" | "invalid" | "partial"
    reason: str = ""
    missing: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "valid"


def _deep_get(obj, path: str):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
    return cur


def validate_step_result(step: dict, output) -> ValidationOutcome:
    """Deterministically check a step's *claimed* output against reality.

    `output` is the step's `result["output"]` (already unwrapped) — callers
    pass the same shape `{{step.field}}` placeholders resolve against, so
    `expect_file`/`expect_nonempty` paths are written the same way `verify`
    paths already are (e.g. "path", "output.path" if the tool nests its own
    "output" key).
    """
    missing = []
    for key in step.get("verify") or []:
        if not _deep_get({"output": output}, key) and not _deep_get(output, key):
            missing.append(key)
    if missing:
        return ValidationOutcome("partial", "declared verify key(s) missing "
                                  "from output", missing)

    expect_file = step.get("expect_file")
    if expect_file:
        path = (_deep_get(output, expect_file) or
                _deep_get({"output": output}, expect_file))
        if not path or not isinstance(path, str):
            return ValidationOutcome(
                "invalid", f"expected a file path at '{expect_file}' but "
                f"found none in the output", [expect_file])
        if not os.path.exists(path):
            return ValidationOutcome(
                "invalid", f"claimed file '{path}' does not exist on disk",
                [expect_file])

    expect_nonempty = step.get("expect_nonempty")
    if expect_nonempty:
        val = (_deep_get(output, expect_nonempty) or
               _deep_get({"output": output}, expect_nonempty))
        if not (isinstance(val, str) and val.strip()):
            return ValidationOutcome(
                "invalid", f"expected non-empty text at "
                f"'{expect_nonempty}' but it was blank", [expect_nonempty])

    return ValidationOutcome("valid")
