"""Planner: turn a natural-language goal into ordered, tool-backed steps.

No offline/deterministic shortcut: every normal request goes through the
same path —

    User -> Assistant -> Astra AI Gateway -> Provider-ready request
         -> Existing Provider System -> Provider AI decides which tool(s)
         (wallet balances, URL fetch, memory, tasks, browser, ...) to use
         -> tools execute -> final response

There is no keyword/regex matching in this module that decides a tool
directly from the raw text. `plan()` always hands the goal to `_ai_steps()`,
which is the Astra AI Gateway -> Provider path (see below); the only
non-Gateway fallback is a plain "answer" step, used solely when there is
literally no Provider configured to plan with (`self.router` is None/
unusable) or the Provider returns nothing usable. That fallback never picks
a tool — it is a graceful "can't plan this right now" reply, not a
deterministic shortcut.

Removing this deterministic layer does not remove any tool capability:
wallet balance checks, URL fetching, memory, task management, browser
actions, and every other registered tool remain fully available — the
Provider AI is the one that now decides when to use them, through the
normal plan -> execute flow, exactly like any other tool it can call.

Astra AI Gateway (optional, `gateway_intelligence`): the raw goal is first
run through the Gateway's Request Understanding/Enrichment layer
(astra/ai/gateway.py) — its own GW_* AI connections, completely separate
from the Provider system — so a short, incomplete or poorly structured
request becomes a clearer Provider-ready prompt before the *existing*
Provider system (`self.router`) actually executes it. The Gateway only
rewrites the request text; it never plans, never calls a tool, and is never
the one that answers. If the Gateway is absent/unusable/fails, the raw goal
is used unchanged — this layer is a pure quality improvement, never a
dependency, and its absence never causes a request to skip the Provider
system.
"""
from __future__ import annotations

import json


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

    # -- entry ---------------------------------------------------------------
    def plan(self, goal: str, ctx=None, max_goal_chars: int = 1500,
             max_steps: int = 6) -> list[dict]:
        g = goal.strip()
        if not g:
            return [self._answer(g, "Kichu bollen na. 'help' likhen.")]
        budget_goal = g[:max_goal_chars] if len(g) > max_goal_chars else g
        convo_context = (ctx or {}).get("conversation_context", "")
        # Every normal request goes through the Gateway -> Provider path.
        # There is no deterministic/offline tool-matching step here — the
        # Provider AI is the one that decides which tool(s), if any, the
        # request needs.
        steps = self._ai_steps(budget_goal, max_steps=max_steps,
                               context=convo_context)
        if not steps:
            # No Provider configured, or the Provider returned nothing
            # usable — a plain fallback reply, never a picked tool.
            steps = [self._answer(budget_goal)]
        return steps[:max_steps]

    # -- LLM-driven planning --------------------------------------------------
    def _ai_steps(self, g: str, max_steps: int = 6, context: str = "") -> list | None:
        if not self.router:
            return None
        # Astra AI Gateway: Request Understanding/Enrichment happens here,
        # right before the goal reaches the existing Provider system — see
        # the module docstring. Enrichment failure/absence is silent and
        # non-fatal: `enriched_goal` just falls back to the raw goal `g`.
        # `context` (recent prior conversation, when the caller has it) is
        # handed through unchanged so the Gateway can resolve references in
        # a short follow-up message — it never becomes part of the goal
        # itself and is empty by default, so callers without it are
        # unaffected.
        enriched_goal = g[:1500]
        self.last_gateway_enriched = False
        self.last_gateway_connection = ""
        if self.gateway_intelligence is not None:
            result = self.gateway_intelligence.process(
                g[:1500], context=context[:1500] if context else "")
            enriched_goal = result.get("text") or enriched_goal
            self.last_gateway_enriched = bool(result.get("enriched"))
            self.last_gateway_connection = result.get("gateway_connection", "")
        prompt = (
            "You are the planner of a personal AI assistant. Split the user's "
            "goal into 1-4 concrete steps. For each step return ONLY JSON "
            "matching: "
            '{{"steps":[{{"id":"s1","tool":"<toolname>","params":{{...}},'
            '"description":"<human label>","depends_on":["s0"]}}]}}. '
            '"depends_on" lists step ids that must finish first (omit when '
            "none). Available tools: " + ", ".join(self.tools or ["(none)"]) +
            '. If no tool fits, use tool name "answer" with params '
            '{{"text":"<user_facing_reply>"}}. Goal: "{}"'.format(enriched_goal))
        try:
            _, _, text = self.router.route([{"role": "user", "content": prompt}])
            if not text:
                return None
            data = json.loads(text.strip().strip("`"))
            steps = []
            issued: set[str] = set()
            for s in data.get("steps", [])[:max_steps]:
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
            return steps or None
        except Exception:
            return None

    # -- step factory ---------------------------------------------------------
    @staticmethod
    def _step(sid: str, tool: str, params: dict, description: str,
              verify=None, depends_on: list | None = None) -> dict:
        return {"id": sid, "tool": tool, "params": params,
                "description": description, "verify": verify or [],
                "retries": 2, "depends_on": depends_on or []}

    @staticmethod
    def _answer(g: str, text: str = "") -> dict:
        return {"id": "a1", "tool": "answer",
                "params": {"text": text or _fallback_text()},
                "description": "Direct reply (no Provider configured)",
                "verify": [], "retries": 0, "is_answer": True}


def _fallback_text() -> str:
    return ("Ei command ta ami bodhokorar chesta korlam, kintu kono AI Provider "
            "configure kora nai. 'help' likhe dekhte paren, ba ekta Provider "
            "(jemon GEMINI_API_KEYS / GROQ_API_KEYS) config korun.")
