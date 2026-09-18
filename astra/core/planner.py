"""Planner: shared plan-shape helpers used around the agent's tool-step
pipeline.

`plan()` and `_ai_steps()` (the Gateway-enriched, AI-driven "turn a raw
goal into a step plan" entry point) have been removed — this class is
being rebuilt with a new planning entry point. What remains is
plan-shape plumbing other code still depends on:

  - `parse_plan_json()` / `_normalize_plan_shape()` — turn an
    already-JSON-decoded `{"steps": [...]}` (or a few tolerated near-miss
    shapes) into executor-ready step dicts. Still used by
    `Orchestrator._gateway_final_task_verification()` to turn a
    corrective AI reply into additional steps.
  - `_step()` / `_answer()` / `_fallback_text()` — step-dict factories.
    `_answer()` is still called directly by `Orchestrator.run()`'s
    replan path when a replan produces no usable steps.

`Orchestrator.run()` still calls `self.planner.plan(...)` in two places
(initial plan + replan) — those calls will raise `AttributeError` until
a new planning entry point is wired back in.
"""
from __future__ import annotations

class Planner:
    def __init__(self, router=None, tools=None, config=None,
                 gateway_intelligence=None):
        self.router = router          # AstraRouter (optional)
        self.tools = tools or []      # names the executor may call
        self.config = config
        # Astra AI Gateway's request-understanding layer (optional). Never
        # a Provider, never part of ProviderRegistry/AstraRouter — see
        # astra/ai/gateway.py. Purely rewrites the goal text handed to the
        # existing Provider system below.
        self.gateway_intelligence = gateway_intelligence
        self.last_gateway_enriched = False   # did the last _ai_steps use it?
        self.last_gateway_connection = ""    # which GW_* connection served it
        # §8 final result gate, surfaced for callers/tests: the
        # completion_status (COMPLETE/INCOMPLETE/FAILED/UNCERTAIN/"") the
        # Gateway's Task Completion supervisor reported for the most
        # recent planning call. "" means no contract-backed call has run
        # yet (e.g. no router configured).
        self.last_completion_status = ""
        # Why the last _ai_steps() call returned None, so plan()'s fallback
        # answer can say something true instead of always claiming "no
        # Provider configured" — a Provider can be fully configured, get
        # called (possibly several times, through the correction loop),
        # and still fail to produce a usable plan. One of:
        #   "no_provider"       - self.router is None/unusable (misconfig)
        #   "provider_no_plan"  - the Provider was called but never
        #                         returned a usable plan, even after the
        #                         Gateway's bounded correction attempts
        #   ""                  - no failure yet recorded
        self.last_plan_failure_reason = ""

    def parse_plan_json(self, data: dict, max_steps: int = 6) -> list[dict]:
        """Turn an already-JSON-decoded `{"steps":[...]}` payload into
        executor-ready step dicts (tool allowlist, dependency filtering,
        id issuance) — the same logic `_ai_steps` uses for the initial
        plan, factored out so a post-execution Gateway correction
        round-trip (Orchestrator._gateway_final_task_verification) can
        turn a corrective AI reply into additional steps without
        duplicating this parsing."""
        data = self._normalize_plan_shape(data)
        steps = []
        issued: set[str] = set()
        for s in (data.get("steps", []) if isinstance(data, dict) else [])[:max_steps]:
            tool = s.get("tool") or "answer"
            if tool not in (self.tools or []) + ["answer"]:
                tool = "answer"
            deps = [d for d in (s.get("depends_on") or [])
                    if isinstance(d, str) and d in issued]
            steps.append(self._step("s%d" % (len(issued) + 1), tool,
                                    s.get("params") or {},
                                    s.get("description", ""),
                                    depends_on=deps))
            issued.add(steps[-1]["id"])
        return steps

    # Synonyms a chatty/weak model reaches for instead of the requested
    # {"steps":[{"tool":"answer","params":{"text":...}}]} envelope when it
    # just wants to say something back (§8/§9: still a usable reply, not a
    # malformed one worth a correction round-trip or a fallback message).
    _ANSWER_TEXT_KEYS = ("answer", "text", "reply", "response", "message",
                        "content")

    @classmethod
    def _normalize_plan_shape(cls, data):
        """Tolerate a few common near-miss shapes instead of discarding an
        otherwise-usable reply as unparseable (previously: a "steps" field
        strictly required, so any reply lacking it fell through to
        `_plain_text_reply`, which in turn rejects anything starting with
        "{" — meaning these shapes were rejected TWICE and always ended up
        in the generic fallback message, even though the model's own
        answer was sitting right there):

          - a bare list of step dicts, with no {"steps": [...]} wrapper
          - a single step dict on its own (has a "tool" key), not wrapped
            in a list at all
          - a plain {"answer": "..."} — or "text"/"reply"/"response"/
            "message"/"content" — instead of an "answer" tool step

        Anything else (including a dict with none of the above, e.g.
        {"plan": [...]}) is returned unchanged so the existing
        required-field check and correction loop still run exactly as
        before; this only widens what counts as ALREADY usable, it never
        loosens what the Provider is asked to produce.
        """
        if isinstance(data, list):
            return {"steps": data}
        if not isinstance(data, dict):
            return data
        if "steps" in data:
            return data
        if "tool" in data:
            return {"steps": [data]}
        for key in cls._ANSWER_TEXT_KEYS:
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return {"steps": [{"id": "s1", "tool": "answer",
                                   "params": {"text": val},
                                   "description": "Direct reply"}]}
        return data

    # -- step factory ---------------------------------------------------------
    @staticmethod
    def _step(sid: str, tool: str, params: dict, description: str,
              verify=None, depends_on: list | None = None) -> dict:
        return {"id": sid, "tool": tool, "params": params,
                "description": description, "verify": verify or [],
                "retries": 2, "depends_on": depends_on or []}

    def _answer(self, g: str, text: str = "", description: str = "") -> dict:
        reason = getattr(self, "last_plan_failure_reason", "")
        return {"id": "a1", "tool": "answer",
                "params": {"text": text or _fallback_text(reason)},
                "description": description or (
                    "Direct reply (provider gave no usable plan)"
                    if reason == "provider_no_plan" else
                    "Direct reply (no Provider configured)"),
                "verify": [], "retries": 0, "is_answer": True}


def _fallback_text(reason: str = "") -> str:
    if reason == "provider_no_plan":
        # The Provider was configured and did respond — it just couldn't
        # produce a usable plan even after the Gateway's correction
        # attempts. Telling the user "no Provider configured" here would
        # be false, so this branch says what actually happened instead.
        return ("Ami AI Provider-ke koyekbar try korlam, kintu ekhono ekta "
                "thik plan/reply banate parlam na. Aro nirdisto kore ekbar "
                "bolun, ba 'help' likhe dekhte paren.")
    return ("Ei command ta ami bodhokorar chesta korlam, kintu kono AI Provider "
            "configure kora nai. 'help' likhe dekhte paren, ba ekta Provider "
            "(jemon GEMINI_API_KEYS / GROQ_API_KEYS) config korun.")

