"""Executor: run one planned step through the tool registry with the
agent's retry / verify / experience-learning rules.

Every step → {ok, output, error, decision, retries_used}. An 'ask' decision
means the orchestrator must hold for WAITING_USER; a failed step marks only
itself failed (retryed up to `retries`), so siblings are not destroyed.
"""
from __future__ import annotations

import time

from astra.core.timeutil import duration_ms, ms_now


class Executor:
    def __init__(self, registry, tasks=None, events=None, experiences=None):
        self.registry = registry
        self.tasks = tasks
        self.events = events
        self.experiences = experiences

    def execute(self, step: dict, ctx, run_ctx) -> dict:
        sid, tool = step.get("id", "s"), step.get("tool", "answer")
        if tool == "answer":
            return {"step": sid, "tool": "answer", "decision": "allow",
                    "ok": True, "output": {"text": (step.get("params") or {}).get("text", "")},
                    "error": "", "retries_used": 0, "duration_ms": 0}

        # 1. learn from similar past steps (experience memory)
        experience = self.experiences.recall(f"{tool} {step.get('params', {})}".lower(), k=1) \
            if self.experiences else []

        retries = int(step.get("retries", 0))
        error, output = "", None
        decision, duration = "allow", 0
        for attempt in range(retries + 1):
            t0 = ms_now()
            out = self.registry.execute(tool, step.get("params") or {}, ctx)
            duration = duration_ms(t0)
            if out.get("decision") in ("ask", "deny"):
                decision = out["decision"]
                error = out.get("reason", "")
                break
            if out.get("ok"):
                output, decision = out.get("result"), "allow"
                break
            error = (out.get("error") or "").strip() or "tool returned ok=False"
            if attempt < retries:
                time.sleep(min(0.5 * (attempt + 1), 3))
        ok = decision == "allow" and output is not None or (
            decision == "allow" and (step.get("is_answer") or False))

        # 2. verify: required keys present in output?
        missing = []
        if ok and step.get("verify"):
            for k in step["verify"]:
                if not _deep_get(output, k):
                    missing.append(k)
        ok = ok and not missing

        # 3. learn from the outcome (experience memory)
        if self.experiences:
            pattern = f"{tool}-{sid}"
            self.experiences.add(
                pattern, strategy=f"{tool}({step.get('params', {})})",
                result=str(output or "")[:200],
                failure=error or "", success=ok, tool=tool)
        return {"step": sid, "tool": tool, "decision": decision, "ok": ok,
                "output": output if ok else {}, "error": error,
                "retries_used": retries, "duration_ms": duration,
                "experience": experience[0] if experience else None}


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