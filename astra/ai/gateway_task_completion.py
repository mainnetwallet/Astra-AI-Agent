"""Astra AI Gateway — Task Completion Contract, evidence/semantic
verification, correction and final result gate.

This module extends `astra.ai.gateway_supervision` (which only ever checks
"non-empty / valid JSON / required fields present") with the additional
layer the Gateway spec asks for:

    User request
        -> Gateway builds a Task Completion Contract (goal, required
           actions, constraints, expected output, completion criteria,
           evidence needed)
        -> Existing Provider executes (via ProviderExecutionPort, §3 —
           unchanged; this module never touches ProviderRegistry, an
           adapter, or a credential)
        -> Gateway verifies the result AGAINST THE CONTRACT:
             1. deterministic result shape (reuses
                astra.ai.gateway_supervision.validate_execution_result)
             2. evidence (tool/test/file/artifact evidence the caller
                supplies — never invented here)
             3. an OPTIONAL, injectable semantic verifier, used only when
                the contract asks for it and only after 1-2 already pass,
                never as the sole check for a request that provided no
                evidence to check against
        -> if COMPLETE: the result is released
        -> if INCOMPLETE/UNCERTAIN: a precise correction instruction is
           built and sent back through the SAME ProviderExecutionPort to
           the SAME target (never a different provider/model — that is
           `astra.ai.gateway_recovery`'s job, not this one's), bounded by
           `astra.core.correction.MAX_CORRECTION_ATTEMPTS` (one
           authoritative bound for the whole repo)
        -> if FAILED (the port itself raised, i.e. a genuine
           provider/network/tool failure mid-correction): the loop stops
           immediately — that is a recovery/failover concern, not a
           correction concern (§7: "do not mix these two mechanisms"),
           and is left for the caller's existing recovery path
        -> the final gate never silently reports COMPLETE unless the last
           verification actually said so.

Scope discipline (mirrors gateway_supervision.py):
  - Nothing here invents a requirement the caller/user did not ask for —
    `build_task_completion_contract` only records what it is given.
  - A request with no evidence_required and no semantic_verifier behaves
    exactly like plain `validate_execution_result` (§9/§36: don't drag a
    simple request into heavyweight verification it never needed).
  - This module imports nothing from astra.ai.registry / astra.ai.provider
    / astra.ai.adapters — see TestFailOpenAndIsolation-style checks in
    tests/test_gateway_task_completion.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from astra.ai.gateway_contract import (ProviderExecutionPort,
                                       ProviderExecutionResult,
                                       ProviderExecutionTarget)
from astra.ai.gateway_supervision import (build_correction_messages,
                                          validate_execution_result)
from astra.core.correction import MAX_CORRECTION_ATTEMPTS

# ── §8 completion taxonomy ───────────────────────────────────────────────────
COMPLETE = "COMPLETE"
INCOMPLETE = "INCOMPLETE"
FAILED = "FAILED"
UNCERTAIN = "UNCERTAIN"

COMPLETION_STATUSES = frozenset({COMPLETE, INCOMPLETE, FAILED, UNCERTAIN})


# ── §1 Task Completion Contract ──────────────────────────────────────────────
@dataclass
class TaskCompletionContract:
    """What the Gateway will hold the result to (§1).

    Every field defaults to empty/False — a contract built from a bare
    request with nothing else supplied is `is_minimal` and verification
    degrades to exactly `validate_execution_result` (§9). Nothing is ever
    invented: a caller must explicitly pass `required_actions`,
    `evidence_required`, etc. for the Gateway to check them.
    """
    user_request: str = ""
    goal: str = ""
    required_actions: tuple = field(default_factory=tuple)
    constraints: tuple = field(default_factory=tuple)
    expected_output: str = ""
    completion_criteria: tuple = field(default_factory=tuple)
    evidence_required: tuple = field(default_factory=tuple)
    require_json: bool = False
    required_fields: tuple = field(default_factory=tuple)
    require_semantic: bool = False

    @property
    def is_minimal(self) -> bool:
        return not (self.required_actions or self.constraints or
                    self.expected_output or self.completion_criteria or
                    self.evidence_required or self.require_json or
                    self.required_fields or self.require_semantic)

    def to_dict(self) -> dict:
        return {"user_request": self.user_request, "goal": self.goal,
                "required_actions": list(self.required_actions),
                "constraints": list(self.constraints),
                "expected_output": self.expected_output,
                "completion_criteria": list(self.completion_criteria),
                "evidence_required": list(self.evidence_required),
                "require_json": self.require_json,
                "required_fields": list(self.required_fields),
                "require_semantic": self.require_semantic}


def build_task_completion_contract(
        user_request: str = "", *, goal: str = "",
        required_actions=(), constraints=(), expected_output: str = "",
        completion_criteria=(), evidence_required=(),
        require_json: bool = False, required_fields=(),
        require_semantic: bool = False) -> TaskCompletionContract:
    """Build a contract from exactly what the caller supplies (§1/§9).

    Never infers requirements the caller didn't pass. For a trivial
    request (nothing beyond `user_request`), the returned contract is
    `is_minimal` and downstream verification does the smallest possible
    amount of work — see `verify_task_completion`.
    """
    return TaskCompletionContract(
        user_request=user_request, goal=goal,
        required_actions=tuple(required_actions),
        constraints=tuple(constraints), expected_output=expected_output,
        completion_criteria=tuple(completion_criteria),
        evidence_required=tuple(evidence_required),
        require_json=require_json, required_fields=tuple(required_fields),
        require_semantic=require_semantic)


# ── §2/§3 verification ───────────────────────────────────────────────────────
@dataclass
class TaskVerificationOutcome:
    status: str             # COMPLETE | INCOMPLETE | FAILED | UNCERTAIN
    reason: str = ""
    missing: list = field(default_factory=list)
    evidence_checked: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == COMPLETE

    @property
    def correctable(self) -> bool:
        """INCOMPLETE/UNCERTAIN are worth a correction round-trip; FAILED
        (a genuine execution failure) is not — that's recovery's job."""
        return self.status in (INCOMPLETE, UNCERTAIN)


def _missing_evidence(contract: TaskCompletionContract, evidence: dict | None) -> list:
    ev = evidence or {}
    return [key for key in contract.evidence_required if not ev.get(key)]


def verify_task_completion(
        contract: TaskCompletionContract, result: ProviderExecutionResult,
        evidence: dict | None = None, semantic_verifier=None
        ) -> TaskVerificationOutcome:
    """Evidence-based + (optionally) semantic verification against
    `contract` (§2/§3). Deterministic checks always run first and are
    authoritative on their own for a minimal contract; a semantic
    verifier is only ever consulted once every deterministic check has
    already passed, and only when `contract.require_semantic` is set —
    it is never the sole gate for a task that supplied no evidence to
    check (§9: don't over-engineer a simple request).
    """
    # 1) provider/tool execution itself failed — this is a FAILED
    #    execution, not an incomplete task; correction must not retry
    #    this (that's recovery/failover's job, §7).
    if not result.ok:
        return TaskVerificationOutcome(FAILED, result.error or
                                       "provider execution failed")

    # 2) deterministic shape check (§9 default: just "not empty"; JSON +
    #    required_fields only if the contract actually asked for them).
    base = validate_execution_result(
        result, require_json=contract.require_json,
        required_fields=contract.required_fields)
    if base.status != "valid":
        return TaskVerificationOutcome(INCOMPLETE, base.reason, base.missing)

    # 3) evidence required by the contract (§2) — tool results, test
    #    results, file/artifact state, etc. Never invented here: the
    #    caller supplies `evidence` from whatever it actually observed
    #    (executor step outputs, test runner exit code, file existence
    #    checks...). A bare "Done." with no matching evidence key present
    #    does NOT pass (§2: "do not rely only on the Provider's
    #    statement").
    missing_evidence = _missing_evidence(contract, evidence)
    if missing_evidence:
        return TaskVerificationOutcome(
            INCOMPLETE,
            "required evidence not present: " + ", ".join(missing_evidence),
            missing_evidence, list(contract.evidence_required))

    # 4) optional semantic verification (§3) — only reached once every
    #    deterministic/evidence check above has already passed.
    if contract.require_semantic and semantic_verifier is not None:
        try:
            status, reason = semantic_verifier(contract, result, evidence or {})
        except Exception as e:
            # fail open toward caution, never toward false completion:
            # an exception in the (external) verifier is UNCERTAIN, not
            # COMPLETE.
            return TaskVerificationOutcome(
                UNCERTAIN, f"semantic verifier error: {e}")
        status = status if status in COMPLETION_STATUSES else UNCERTAIN
        if status != COMPLETE:
            return TaskVerificationOutcome(status, reason or
                                           "semantic verification did not confirm completion")

    return TaskVerificationOutcome(COMPLETE, "", [], list(contract.evidence_required))


def build_task_correction_instruction(
        contract: TaskCompletionContract, result: ProviderExecutionResult,
        outcome: TaskVerificationOutcome) -> str:
    """Precise correction instruction (§4) — never a bare "try again".

    Branches on how much of the previous attempt is actually salvageable:
    if it satisfied NOTHING the contract asked for (failed the basic
    shape check entirely, or every single piece of required evidence is
    still missing), telling the model to "continue from where it left
    off" makes no sense — there's nothing to continue from, and doing so
    tends to produce a model that just repeats or rationalizes its first
    (wrong) answer. In that case the instruction explicitly discards the
    previous attempt and asks for a fresh one. Only when at least part of
    the requirement was actually met does it ask for an incremental
    patch, so real completed work is never thrown away and redone.
    """
    missing = ", ".join(outcome.missing) or "(unspecified)"
    lines = []
    if contract.goal:
        lines.append(f"Goal: {contract.goal}")
    if contract.required_actions:
        lines.append("Required actions: " + "; ".join(contract.required_actions))
    if contract.constraints:
        lines.append("Constraints: " + "; ".join(contract.constraints))
    lines.append(f"Status: task is {outcome.status.lower()} — {outcome.reason}")
    lines.append(f"Missing / Invalid: {missing}")

    nothing_salvaged = (
        not outcome.evidence_checked
        or (outcome.missing and len(outcome.missing) >= len(outcome.evidence_checked)))
    if nothing_salvaged:
        lines.append(
            "Already Completed: none — the previous attempt's response "
            "did not satisfy any part of the task and cannot be "
            "salvaged.")
        lines.append(
            "Required Next Action: disregard the previous attempt "
            "entirely and redo the task from scratch, addressing every "
            "requirement listed above. Do not just say the task is "
            "done — return the actual output and/or evidence requested.")
    else:
        lines.append(
            "Already Completed: the previous attempt returned a response; "
            "do not repeat already-completed work.")
        lines.append(
            "Required Next Action: continue from where this left off and "
            "produce the missing requirement(s) and evidence above. Do not "
            "just say the task is done — return the actual changed output "
            "and/or evidence requested. Do not re-run anything that may have "
            "already succeeded; check state before repeating a side effect.")
    return "\n".join(lines)


def build_task_completion_messages(messages: list, result: ProviderExecutionResult,
                                   contract: TaskCompletionContract,
                                   outcome: TaskVerificationOutcome) -> list:
    """Same shape as `gateway_supervision.build_correction_messages`
    (assistant echo + a correction user turn), but using the richer §4
    instruction above and never mutating the caller's list."""
    correction_text = build_task_correction_instruction(contract, result, outcome)
    corrected = list(messages or [])
    corrected.append({"role": "assistant", "content": result.text or ""})
    corrected.append({"role": "user", "content": correction_text})
    return corrected


# ── §5-§8 supervisor: verify -> correct -> re-verify -> final gate ──────────
class GatewayTaskCompletionSupervisor:
    """Bounded verify/correct loop against a `TaskCompletionContract`.

    Distinct from `GatewayResultSupervision` (deterministic shape only):
    this adds evidence + optional semantic verification and a richer
    correction instruction, but shares the same bound
    (`MAX_CORRECTION_ATTEMPTS`) and the same "never touch a different
    target" rule (§10/§11).
    """

    def __init__(self, events=None):
        self.events = events

    def _emit(self, kind: str, **data) -> None:
        if self.events:
            try:
                self.events.emit(kind, agent="gateway.task_completion", **data)
            except Exception:
                pass

    def supervise(self, port: ProviderExecutionPort,
                  target: ProviderExecutionTarget, messages: list,
                  result: ProviderExecutionResult,
                  contract: TaskCompletionContract, *, evidence: dict | None = None,
                  semantic_verifier=None, max_tokens: int = 500
                  ) -> tuple[ProviderExecutionResult, TaskVerificationOutcome, int]:
        """Returns `(final_result, final_outcome, attempts)`. The caller
        is the one that ultimately tells the user "done" or "incomplete"
        — this method itself never claims completion that the last
        verification didn't actually confirm (§8 final result gate)."""
        outcome = verify_task_completion(contract, result, evidence, semantic_verifier)
        current_messages, current_result = messages, result
        attempts = 0

        while outcome.correctable and attempts < MAX_CORRECTION_ATTEMPTS:
            attempts += 1
            self._emit("gateway.task_completion.correction_requested",
                       provider=target.provider_id, model=target.model_id,
                       status=outcome.status, reason=outcome.reason,
                       attempt=attempts)
            current_messages = build_task_completion_messages(
                current_messages, current_result, contract, outcome)
            try:
                # §6/§10: correction goes back through the SAME port to
                # the SAME target, continuing the conversation — never a
                # restart, never a different provider/model.
                text = port.execute(target, current_messages,
                                    max_tokens=max_tokens)
                current_result = ProviderExecutionResult(ok=True, text=text)
            except Exception as e:
                # §7: a genuine execution failure mid-correction is a
                # recovery/failover concern, not something this loop
                # should keep hammering on.
                current_result = ProviderExecutionResult(ok=False, error=str(e))
                outcome = TaskVerificationOutcome(FAILED, str(e))
                self._emit("gateway.task_completion.correction_failed",
                           provider=target.provider_id, model=target.model_id,
                           error=str(e), attempt=attempts)
                break

            outcome = verify_task_completion(contract, current_result, evidence,
                                             semantic_verifier)
            if outcome.ok:
                self._emit("gateway.task_completion.correction_succeeded",
                           provider=target.provider_id, model=target.model_id,
                           attempt=attempts)

        if not outcome.ok and attempts:
            self._emit("gateway.task_completion.correction_exhausted",
                       provider=target.provider_id, model=target.model_id,
                       attempts=attempts, status=outcome.status,
                       reason=outcome.reason)

        # §8 final gate: never upgrade the status here — return exactly
        # what the last verification said.
        return current_result, outcome, attempts
