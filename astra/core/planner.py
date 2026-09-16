"""Planner: turn a natural-language goal into ordered, tool-backed steps.

Offline-first: built-in intent patterns cover the everyday requests (discover
airdrops, check balances, research a project, review today's tasks, remember /
recall, file analysis, help). When an AI provider is configured and the goal
is not matched offline, the LLM is asked for a JSON step plan and the result
is parsed defensively — any failure drops back to offline planning, so the
agent never blocks on the network.
"""
from __future__ import annotations

import json
import re


class Planner:
    def __init__(self, router=None, tools=None, config=None):
        self.router = router          # AstraRouter (optional)
        self.tools = tools or []      # names the executor may call
        self.config = config

    # -- entry ---------------------------------------------------------------
    def plan(self, goal: str, ctx=None, max_goal_chars: int = 1500,
             max_steps: int = 6) -> list[dict]:
        g = goal.strip()
        if not g:
            return [self._answer(g, "Kichu bollen na. 'help' likhen.")]
        budget_goal = g[:max_goal_chars] if len(g) > max_goal_chars else g
        steps = self._offline_steps(budget_goal, ctx)
        if steps is None:
            steps = (self._ai_steps(budget_goal, max_steps=max_steps)
                     or self._offline_steps(budget_goal, ctx, fallback=True) or [])
        if not steps:
            steps = [self._answer(budget_goal)]
        return steps[:max_steps]

    # -- offline intent matching ---------------------------------------------
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
        if url or re.search(r"\b(research|report|about|review|analyse|analyze)\b", low):
            q = url.group(0).rstrip(".,;)" if url else "") if url else g.replace("research", "").replace("about", "").strip()
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
    def _ai_steps(self, g: str, max_steps: int = 6) -> list | None:
        if not self.router:
            return None
        prompt = (
            "You are the planner of a personal AI assistant. Split the user's "
            "goal into 1-4 concrete steps. For each step return ONLY JSON "
            "matching: "
            '{{"steps":[{{"id":"s1","tool":"<toolname>","params":{{...}},'
            '"description":"<human label>","depends_on":["s0"]}}]}}. '
            '"depends_on" lists step ids that must finish first (omit when '
            "none). Available tools: " + ", ".join(self.tools or ["(none)"]) +
            '. If no tool fits, use tool name "answer" with params '
            '{{"text":"<user_facing_reply>"}}. Goal: "{}"'.format(g[:1500]))
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