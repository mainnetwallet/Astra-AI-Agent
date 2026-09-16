"""Bounded correction loop (Gateway spec §11-12).

Correction is distinct from provider/model failover (spec §20): it applies
when the SAME target is still healthy but the result it returned was
deterministically invalid/partial (astra.core.result_validation). The
instruction below tells the target what already happened and what's still
missing, so it continues rather than redoing completed work.

Correction is bounded (`MAX_CORRECTION_ATTEMPTS`) and only ever offered to
idempotent tools (astra.tools.schemas.Tool.idempotent) — a non-idempotent
step (a wallet tx, a browser mutation, ...) is never blindly re-invoked just
because its result looked incomplete; see astra.core.executor.Executor.
"""
from __future__ import annotations

MAX_CORRECTION_ATTEMPTS = 2


def build_correction_instruction(step: dict, outcome, output) -> str:
    """Compact, provider-ready correction instruction (spec §11 shape)."""
    missing = ", ".join(outcome.missing) or "(unspecified)"
    lines = [
        f"Task: {step.get('description') or step.get('tool', '')}",
        f"Current Step: {step.get('tool', '')}",
        "Already Completed: the previous attempt returned a result, but it "
        "did not satisfy the required output.",
        f"Missing / Invalid: {missing} — {outcome.reason}",
        "Required Next Action: continue the current step and produce the "
        "missing/valid output. Do NOT repeat already-completed work; do NOT "
        "invent a value for what's missing — actually produce it.",
    ]
    return "\n".join(lines)
