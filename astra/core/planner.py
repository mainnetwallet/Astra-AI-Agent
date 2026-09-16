"""Planner: turn a natural-language goal into ordered, tool-backed steps.

Deterministic-first, narrowly: a fixed set of *genuinely tool-specific*
intent patterns are matched offline — ones that need no AI reasoning at all
(system health, memory read/write, task listing) or hinge on a concrete
structural signal in the message (an actual URL, an explicit file path).
Everything else — any normal free-form request that actually requires AI
reasoning to understand or answer — falls through to the LLM planner, which
always goes through the Astra AI Gateway first (see below). The offline
patterns intentionally do NOT match on topic keywords alone (e.g. "review",
"about", "analyse" with no URL) precisely so they can't silently steal a
normal conversational request away from the Gateway -> Provider path.

Astra AI Gateway (optional, `gateway_intelligence`): when a goal falls
through to the LLM planner, the raw goal is first run through the Gateway's
Request Understanding/Enrichment layer (astra/ai/gateway.py) — its own
GW_* AI connections, completely separate from the Provider system — so a
short, incomplete or poorly structured request becomes a clearer
Provider-ready prompt before the *existing* Provider system (`self.router`)
actually executes it. The Gateway only rewrites the request text; it never
plans, never calls a tool, and is never the one that answers. If the
Gateway is absent/unusable/fails, the raw goal is used unchanged — this
layer is a pure quality improvement, never a dependency.
"""
from __future__ import annotations

import json
import re


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
        steps = self._offline_steps(budget_goal, ctx)
        if steps is None:
            convo_context = (ctx or {}).get("conversation_context", "")
            steps = (self._ai_steps(budget_goal, max_steps=max_steps,
                                    context=convo_context)
                     or self._offline_steps(budget_goal, ctx, fallback=True) or [])
        if not steps:
            steps = [self._answer(budget_goal)]
        return steps[:max_steps]

    # -- offline intent matching ---------------------------------------------
    # NOTE: every branch here is a genuinely tool-specific, deterministic
    # match — either it needs no AI reasoning at all (get_health, memory
    # read/write, listing tasks) or it hinges on a concrete structural
    # signal in the message itself (an actual URL, an explicit file path)
    # rather than a loose topic keyword. A normal free-form request that
    # merely *mentions* a related word (e.g. "can you review this idea?"
    # with no URL) must NOT be caught here — it falls through to
    # `_ai_steps()`, which is the Assistant -> Astra AI Gateway -> Provider
    # path. Bypassing that path is reserved for requests where a
    # deterministic tool call is unambiguously the right answer.
    def _offline_steps(self, g: str, ctx=None, fallback: bool = False):
        low = g.lower()
        url = re.search(r"https?://\S+", g)

        if re.search(r"\b(find|discover|new|latest|top)\b", low) and re.search(r"\bairdrop", low):
            return [self._step("s1", "search_web",
                               {"query": "new crypto airdrops this month 2026"},
                               "Discover campaigns",
                               verify=["count"])]
        if re.search(r"\b(balance|balanc|check my wallet|wallet balance)\b", low):
            return [self._step("w1", "wallet_balances", {"network": ""},
                               "Check wallet balances")]
        if re.search(r"\b(eligible|eligibility|am i in|qualify)\b", low):
            return [self._step("e1", "list_tasks", {"status": "pending"},
                               "Check pending eligibility tasks")]
        # Fetching a URL is only ever deterministic when a URL is actually
        # present — that is the unambiguous, structural signal. Words like
        # "research", "report", "about", "review", "analyse" on their own
        # are normal free-form language and must go through the Gateway ->
        # Provider path instead of being forced into fetch_url with a
        # non-URL "url" param.
        if url:
            q = url.group(0).rstrip(".,;)")
            return [self._step("r1", "fetch_url", {"url": q}, "Research project")]
        m = re.search(r"\b(analyze|read|analyse|look at)\s+(?:the\s+)?file\s+(.+)$", low)
        if m:
            return [self._step("f1", "read_file", {"path": m.group(2).strip().strip('"')},
                               "Read file for analysis")]
        if re.search(r"\b(today|what do i need|plan my day|pending|task list|due)\b", low):
            return [self._step("t1", "list_tasks", {"status": "pending"}, "List today's tasks"),
                    self._step("t2", "list_tasks", {"status": "ready"}, "List ready tasks",
                               depends_on=["t1"])]
        if re.search(r"\b(remember|save this|note down)\b", low):
            return [self._step("m1", "remember",
                               {"content": g, "category": "note"},
                               "Save to memory")]
        if re.search(r"\b(recall|what do i know|remembered|search memory)\b", low):
            q = re.sub(r"\b(recall|search memory|what do i know about|remembered)\b", "", low).strip()
            return [self._step("m2", "recall", {"query": q or g, "k": 5},
                               "Recall from memory")]
        if re.search(r"\b(help|what can you do|capabil|commands)\b", low):
            return [self._step("h1", "get_health", {}, "System health + capabilities")]
        if fallback:
            return [self._answer(g)]
        return None  # let AI plan

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
                "description": "Offline reply", "verify": [], "retries": 0,
                "is_answer": True}


def _fallback_text() -> str:
    return ("Ei command ta ami bodhokorar chesta korlam, kintu kono tool/plugin "
            "match korlo na. 'help' likhe dekhte paren — ba chotto theke shuru "
            "korun (jemon 'add airdrop Notcoin deadline 30 oct').")