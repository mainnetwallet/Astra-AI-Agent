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
    """Compact, provider-ready correction instruction (spec §11 shape).

    Same "is anything salvageable" branching as
    astra.ai.gateway_task_completion.build_task_correction_instruction:
    if every declared check failed (nothing the step asked for came back
    valid), there's nothing to "continue from" — ask for a fresh attempt
    instead of telling the model to build on a result that satisfied
    none of the requirements.
    """
    missing = ", ".join(outcome.missing) or "(unspecified)"
    total_checks = len(step.get("verify") or [])
    if step.get("expect_file"):
        total_checks += 1
    if step.get("expect_nonempty"):
        total_checks += 1
    nothing_salvaged = bool(outcome.missing) and (
        total_checks == 0 or len(outcome.missing) >= total_checks)
    lines = [
        f"Task: {step.get('description') or step.get('tool', '')}",
        f"Current Step: {step.get('tool', '')}",
    ]
    if nothing_salvaged:
        lines += [
            "Already Completed: none — the previous attempt did not "
            "satisfy any of the required output and cannot be salvaged.",
            f"Missing / Invalid: {missing} — {outcome.reason}",
            "Required Next Action: disregard the previous attempt "
            "entirely and redo this step from scratch, producing all of "
            "the required output above.",
        ]
    else:
        lines += [
            "Already Completed: the previous attempt returned a result, but it "
            "did not satisfy the required output.",
            f"Missing / Invalid: {missing} — {outcome.reason}",
            "Required Next Action: continue the current step and produce the "
            "missing/valid output. Do NOT repeat already-completed work; do NOT "
            "invent a value for what's missing — actually produce it.",
        ]
    return "\n".join(lines)
