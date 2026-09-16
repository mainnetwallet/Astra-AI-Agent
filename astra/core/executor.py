"""Executor: run one planned step through the tool registry with the
agent's retry / verify / experience-learning rules.

Every step → {ok, output, error, error_code, decision, retries_used}. An
'ask' decision means the orchestrator must hold for WAITING_USER; a failed
step marks only itself failed, so siblings are not destroyed.

Phase E: failures are *classified*, never swallowed. A raising tool becomes
a classified step result carrying a canonical `error_code` (see
core.classification), and retries follow the RetryPolicy — non-retryable
codes (authorization, validation, transaction verdicts, …) are never
retried no matter what the step's `retries` says.

Gateway-spec supervision (§9-12, §19): two additions layered on top of the
above, both no-ops for steps that don't opt in, so no existing plan/tool
behavior changes unless a step actually uses the new fields:

  1. Side-effect safety (§19) — the backoff-retry loop below only ever
     retries a tool blindly when astra.tools.schemas.Tool.idempotent is
     True. A non-idempotent tool (write_file, a wallet tx, ...) gets exactly
     one attempt: if its response is lost to a network/timeout blip *after*
     the real action already happened, we must not resubmit it just because
     the reply didn't arrive.
  2. Correction loop (§11-12) — after a tool call reports ok=True,
     astra.core.result_validation checks any `verify`/`expect_file`/
     `expect_nonempty` the step declared. An invalid/partial result from an
     IDEMPOTENT tool gets up to MAX_CORRECTION_ATTEMPTS "please continue/fix"
     re-invocations (astra.core.correction) before the step is finally
     marked failed with error_code invalid_result/incomplete_result.
"""
from __future__ import annotations

import time

from astra.core.classification import RetryPolicy, code_for
from astra.core.correction import MAX_CORRECTION_ATTEMPTS, build_correction_instruction
from astra.core.result_validation import validate_step_result
from astra.core.timeutil import duration_ms, ms_now


class Executor:
    def __init__(self, registry, tasks=None, events=None, experiences=None,
                 retry_policy: RetryPolicy | None = None):
        self.registry = registry
        self.tasks = tasks
        self.events = events
        self.experiences = experiences
        self.retry_policy = retry_policy or RetryPolicy()

    def _emit(self, kind: str, **data) -> None:
        if self.events:
            try:
                self.events.emit(kind, agent="executor", **data)
            except Exception:
                pass

    def _is_idempotent(self, tool: str) -> bool:
        t = self.registry.get(tool) if self.registry else None
        return bool(getattr(t, "idempotent", False))

    def execute(self, step: dict, ctx, run_ctx) -> dict:
        sid, tool = step.get("id", "s"), step.get("tool", "answer")
        if tool == "answer":
            return {"step": sid, "tool": "answer", "decision": "allow",
                    "ok": True, "output": {"text": (step.get("params") or {}).get("text", "")},
                    "error": "", "error_code": "", "retries_used": 0, "duration_ms": 0}

        # 1. learn from similar past steps (experience memory)
        experience = self.experiences.recall(f"{tool} {step.get('params', {})}".lower(), k=1) \
            if self.experiences else []

        idempotent = self._is_idempotent(tool)
        requested_attempts = 1 + int(step.get("retries", 0))
        # §19 side-effect safety: a non-idempotent tool never gets a blind
        # backoff-retry — if its reply was lost after the real action
        # already happened, resubmitting would duplicate the side effect.
        attempts = requested_attempts if idempotent else 1
        params = step.get("params") or {}
        error, output, code = "", None, ""
        decision, duration = "allow", 0
        used = 0
        for attempt in range(attempts):
            t0 = ms_now()
            out = self._call(tool, params, ctx)
            duration = duration_ms(t0)

            if out.get("decision") in ("ask", "deny"):
                decision = out["decision"]
                error = out.get("reason", "")
                code = code_for(message=error) if error else "internal"
                used = attempt
                break
            if out.get("ok"):
                output, code = out.get("result"), ""
                used = attempt
                break

            # classify the failure: own code > fingerprint the message
            error = (out.get("error") or "").strip() or "tool returned ok=False"
            code = out.get("error_code") or code_for(message=error)

            # policy decides whether a retry happens at all
            if attempt == attempts - 1 or not self.retry_policy.can_retry(code):
                used = attempt
                break
            time.sleep(self.retry_policy.backoff_ms(attempt) / 1000.0)
        ok = decision == "allow" and (output is not None or step.get("is_answer"))

        # 2. verify: required keys present in output? (kept exactly as
        # before for backward compatibility with existing plans/tests)
        missing = []
        if ok and step.get("verify"):
            for k in step["verify"]:
                if not _deep_get(output, k):
                    missing.append(k)
        ok = ok and not missing

        # 2b. §9-12 deterministic result validation + bounded correction.
        # A no-op unless the step declares expect_file/expect_nonempty (or
        # verify, already folded above) — every other step behaves exactly
        # as before this addition.
        if ok:
            outcome = validate_step_result(step, output)
            correction_attempts = 0
            while not outcome.ok and idempotent and \
                    correction_attempts < MAX_CORRECTION_ATTEMPTS:
                correction_attempts += 1
                self._emit("supervision.correction_requested", tool=tool,
                           step=sid, reason=outcome.reason,
                           attempt=correction_attempts)
                hint = build_correction_instruction(step, outcome, output)
                corrected_params = dict(params, _correction_hint=hint)
                out = self._call(tool, corrected_params, ctx)
                if out.get("ok"):
                    output = out.get("result")
                    outcome = validate_step_result(step, output)
                    if outcome.ok:
                        self._emit("supervision.correction_succeeded", tool=tool,
                                   step=sid, attempt=correction_attempts)
                else:
                    # the tool itself failed on the correction attempt —
                    # stop correcting, fall through to the invalid-result path
                    break
            if not outcome.ok:
                ok = False
                error = f"result validation failed: {outcome.reason}"
                code = ("incomplete_result" if outcome.status == "partial"
                        else "invalid_result")
                self._emit("supervision.correction_exhausted" if
                           correction_attempts else "supervision.validation_failed",
                           tool=tool, step=sid, reason=outcome.reason,
                           correction_attempts=correction_attempts,
                           idempotent=idempotent)

        # 3. learn from the outcome (experience memory)
        if self.experiences:
            pattern = f"{tool}-{sid}"
            self.experiences.add(
                pattern, strategy=f"{tool}({step.get('params', {})})",
                result=str(output or "")[:200],
                failure=error or "", success=ok, tool=tool)
        return {"step": sid, "tool": tool, "decision": decision, "ok": ok,
                "output": output if ok else {}, "error": error,
                "error_code": "" if ok else code,
                "retries_used": used,
                "duration_ms": duration,
                "experience": experience[0] if experience else None}

    def _call(self, tool: str, params: dict, ctx) -> dict:
        """One registry attempt — the executor owns retry policy, so the
        registry's tool-level retries are disabled (`retries=0`) here to
        avoid double-retrying. A raising tool is classified, not swallowed."""
        try:
            return self.registry.execute(tool, params, ctx, retries=0)
        except Exception as e:
            return {"ok": False, "error": str(e), "error_code": code_for(e)}


def _deep_get(obj, path: str):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            cur = cur[int(part)] if int(part) < len(cur) else None
        else:
            return None
    return cur