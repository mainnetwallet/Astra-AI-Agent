"""Astra AI Gateway — result supervision + bounded correction loop (§6-§12).

This is the piece that was missing from the otherwise-complete Gateway
execution-recovery wiring (astra/ai/gateway_recovery.py): everything in
that module decides WHO to execute against (target selection / cooldown /
failover between providers or models). Nothing in it ever looked at WHAT
came back. `astra.core.result_validation` / `astra.core.correction` do
validate-and-correct — but that loop is owned by `astra.core.executor.
Executor`, over TOOL step outputs (file-exists, non-empty text fields), and
never touches an AI provider's chat response or `ProviderExecutionPort`.

`GatewayResultSupervision` is the Gateway-owned equivalent for the Existing
Provider system's raw AI response text:

    Gateway
    → receives a ProviderExecutionResult (astra.ai.gateway_contract)
    → validates it deterministically (non-empty; valid JSON + required
      fields when the caller asked for structured output)
    → if invalid/partial: builds a correction instruction and appends it
      to the SAME conversation, then sends it back through the caller's
      `ProviderExecutionPort` to the SAME (provider, model) target — never
      a different one; that is `recover_execution_target`'s job, not this
      one's (§11: correction happens on the current step, not a failover)
    → validates the corrected result again
    → repeats up to the bound already established for Executor-level
      correction (`astra.core.correction.MAX_CORRECTION_ATTEMPTS`) — one
      authoritative "how many correction attempts" answer for the whole
      repo, never a second competing bound.

Gateway code never touches an adapter, credential, or ProviderRegistry to
do this: `ProviderExecutionPort.execute()` is the only thing it calls, and
that port is implemented on the Existing Provider side
(`astra.ai.router._RouterExecutionPort`), per §3.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from astra.ai.gateway_contract import (ProviderExecutionPort,
                                       ProviderExecutionResult,
                                       ProviderExecutionTarget)
from astra.ai.json_extract import loads_lenient
from astra.core.correction import MAX_CORRECTION_ATTEMPTS


@dataclass
class ExecutionValidationOutcome:
    status: str            # "valid" | "invalid" | "partial"
    reason: str = ""
    missing: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "valid"


def validate_execution_result(result: ProviderExecutionResult, *,
                               require_json: bool = False,
                               required_fields: tuple = ()) -> ExecutionValidationOutcome:
    """Deterministic checks only (§7/§9) — never a semantic ("does this text
    actually answer the question") judgement, mirroring the same
    intentional scope limit as `astra.core.result_validation`.

    With nothing declared to check (`require_json=False`,
    `required_fields=()`), only the non-empty check applies — every
    existing successful chat response still validates as `valid` by
    default, so nothing about today's behavior changes unless a caller
    actually opts into stricter checking (§36: don't drag a simple request
    into supervision it never asked for).
    """
    if not result.ok:
        return ExecutionValidationOutcome(
            "invalid", result.error or "provider execution failed")

    text = (result.text or "").strip()
    if not text:
        return ExecutionValidationOutcome("invalid", "empty response text")

    if require_json:
        try:
            # Lenient on purpose: many chat-tuned models (Cohere's
            # c4ai-aya-expanse-* family included) wrap valid JSON in a
            # ```json fence or add a short preamble even when told to
            # return ONLY JSON. A bare json.loads() rejects that and was
            # driving the correction loop to exhaustion on otherwise-good
            # responses. See astra/ai/json_extract.py.
            parsed = loads_lenient(text)
        except ValueError:
            return ExecutionValidationOutcome(
                "invalid", "response is not valid JSON")
        if required_fields and not isinstance(parsed, dict):
            return ExecutionValidationOutcome(
                "invalid", "expected a JSON object with required fields",
                list(required_fields))
        missing = [f for f in required_fields
                  if isinstance(parsed, dict) and f not in parsed]
        if missing:
            return ExecutionValidationOutcome(
                "partial", "required field(s) missing from JSON response",
                missing)

    return ExecutionValidationOutcome("valid")


def build_correction_messages(messages: list, result: ProviderExecutionResult,
                              outcome: ExecutionValidationOutcome) -> list:
    """Compact correction turn appended to the SAME conversation (§11 shape)
    — tells the target what already happened and what's still missing, so
    it continues rather than redoing/repeating the whole answer."""
    missing = ", ".join(outcome.missing) or "(unspecified)"
    correction_text = (
        "Your previous response did not satisfy the required output.\n"
        f"Missing / Invalid: {missing} — {outcome.reason}\n"
        "Already Completed: the previous attempt returned a response; do "
        "not repeat unrelated already-completed content.\n"
        "Required Next Action: continue and produce the corrected/complete "
        "output only. Do not invent a value for what is missing — actually "
        "produce it."
    )
    corrected = list(messages or [])
    corrected.append({"role": "assistant", "content": result.text or ""})
    corrected.append({"role": "user", "content": correction_text})
    return corrected


class GatewayResultSupervision:
    """Gateway-owned supervision loop (§6-§12).

    Stateless apart from event emission — every call is independent, bounded
    by `MAX_CORRECTION_ATTEMPTS`, and only ever talks to the target it was
    given through the supplied `ProviderExecutionPort` (never a different
    provider/model — that would be recovery, not correction).
    """

    def __init__(self, events=None):
        self.events = events

    def _emit(self, kind: str, **data) -> None:
        if self.events:
            try:
                self.events.emit(kind, agent="gateway.supervision", **data)
            except Exception:
                pass

    def supervise(self, port: ProviderExecutionPort,
                  target: ProviderExecutionTarget, messages: list,
                  result: ProviderExecutionResult, *, max_tokens: int = 500,
                  require_json: bool = False, required_fields: tuple = ()
                  ) -> tuple[ProviderExecutionResult, ExecutionValidationOutcome]:
        """Validate `result`; if invalid/partial, send a correction back
        through `port` to `target` (same provider/model) up to
        `MAX_CORRECTION_ATTEMPTS` times, validating each corrected result
        again. Returns the final (result, outcome) pair — the caller decides
        what to do with a still-invalid outcome after the bound is hit
        (§26: correction loop stays bounded, never infinite)."""
        outcome = validate_execution_result(
            result, require_json=require_json, required_fields=required_fields)
        current_messages, current_result = messages, result
        attempts = 0

        while not outcome.ok and attempts < MAX_CORRECTION_ATTEMPTS:
            attempts += 1
            self._emit("gateway.supervision.correction_requested",
                       provider=target.provider_id, model=target.model_id,
                       reason=outcome.reason, attempt=attempts,
                       got=(current_result.text or "")[:120])
            current_messages = build_correction_messages(
                current_messages, current_result, outcome)
            try:
                # §3/§10: the correction is sent back through the SAME
                # ProviderExecutionPort to the SAME target — the Existing
                # Provider actually executes it; Gateway never fabricates
                # the corrected answer itself.
                text = port.execute(target, current_messages,
                                    max_tokens=max_tokens)
                current_result = ProviderExecutionResult(ok=True, text=text)
            except Exception as e:
                current_result = ProviderExecutionResult(ok=False, error=str(e))
                self._emit("gateway.supervision.correction_failed",
                           provider=target.provider_id, model=target.model_id,
                           error=str(e), attempt=attempts)
                break

            outcome = validate_execution_result(
                current_result, require_json=require_json,
                required_fields=required_fields)
            if outcome.ok:
                self._emit("gateway.supervision.correction_succeeded",
                           provider=target.provider_id, model=target.model_id,
                           attempt=attempts)

        if not outcome.ok and attempts:
            self._emit("gateway.supervision.correction_exhausted",
                       provider=target.provider_id, model=target.model_id,
                       attempts=attempts, reason=outcome.reason)

        return current_result, outcome
