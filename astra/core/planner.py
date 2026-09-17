"""Planner: turn a natural-language goal into ordered, tool-backed steps.

No offline/deterministic shortcut: every normal request goes through the
same path —

    User -> Assistant -> Astra AI Gateway -> Provider-ready request
         -> Task Completion Contract (astra.ai.gateway_task_completion)
         -> Existing Provider System -> Provider AI decides which tool(s)
         (wallet balances, URL fetch, memory, tasks, browser, ...) to use
         -> Gateway verifies the result against the Contract (valid JSON,
         a "steps" plan present) -> bounded correction if not -> tools
         execute -> final response

There is no keyword/regex matching in this module that decides a tool
directly from the raw text. `plan()` always hands the goal to `_ai_steps()`,
which is the Astra AI Gateway -> Task Completion Contract -> Provider path
(see below); the only non-Gateway fallback is a plain "answer" step, used
solely when there is literally no Provider configured to plan with
(`self.router` is None/unusable) or the Provider returns nothing usable
after Gateway verification/correction. That fallback never picks a tool —
it is a graceful "can't plan this right now" reply, not a deterministic
shortcut.

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

Task Completion Contract (§1-§12 of the Gateway spec, astra.ai.
gateway_task_completion): every planning call now builds a minimal
contract — "produce a valid step plan, as JSON, with a 'steps' field" —
and routes through `AstraRouter.route_request()` with that contract
attached. `route_request()` is the ONLY AI-backed planning path — there is
no fallback to the legacy bare `route()` tuple call. A router object that
does not implement `route_request()` cannot plan at all (`_ai_steps`
returns None -> `plan()` falls back to a plain "answer" step, never
another AI execution path). This is a pure promotion of a check the
planner already did by hand (json.loads + a "steps" key) into the
Gateway's own verify -> correct -> re-verify loop:
a malformed/incomplete plan now gets ONE bounded correction round-trip to
the SAME provider/model before falling back to "no usable plan", instead
of silently giving up on the first bad JSON. No semantic verifier is
attached here — a syntactic/shape check is all a "did the planner produce
a usable plan" question needs, keeping this cheap for every request,
including trivial ones (§8/§9 of the spec: no expensive verification for
simple asks). No new evidence exists at planning time (tools haven't run
yet), so the contract carries none — nothing is invented.
"""
from __future__ import annotations

from astra.ai.gateway_task_completion import build_task_completion_contract
from astra.ai.json_extract import loads_lenient
from astra.ai.router import RoutingRequest


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

    # -- entry ---------------------------------------------------------------
    def plan(self, goal: str, ctx=None, max_goal_chars: int = 1500,
             max_steps: int = 6) -> list[dict]:
        g = goal.strip()
        if not g:
            return [self._answer(g, "Kichu bollen na. 'help' likhen.")]
        budget_goal = g[:max_goal_chars] if len(g) > max_goal_chars else g
        convo_context = (ctx or {}).get("conversation_context", "")
        attachments = (ctx or {}).get("attachments") or []
        if attachments:
            att_desc = ", ".join(
                f"{a.get('original_filename', a.get('filename', '?'))} "
                f"({a.get('family', a.get('detected_type', '?'))})"
                for a in attachments[:10])
            budget_goal = f"{budget_goal}\n\n[Attached files: {att_desc}]"
        steps = self._ai_steps(budget_goal, max_steps=max_steps,
                               context=convo_context,
                               attachments=attachments)
        if not steps:
            steps = [self._answer(budget_goal)]
        return steps[:max_steps]

    # -- LLM-driven planning --------------------------------------------------
    def _ai_steps(self, g: str, max_steps: int = 6, context: str = "",
                  attachments: list | None = None) -> list | None:
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
        self.last_completion_status = ""
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
            # §1-§12: a minimal Task Completion Contract — "this call must
            # produce a usable step plan, as JSON, with a 'steps' field" —
            # attached to the RoutingRequest so the Gateway's own
            # verify/correct/re-verify loop runs on the SAME provider/model
            # (astra.ai.gateway_task_completion), instead of the planner
            # silently giving up on the first malformed reply. No evidence
            # or semantic verifier is attached: nothing exists yet at
            # planning time to check evidence against, and a JSON-shape
            # check is all this call needs (§8/§9: stay cheap).
            #
            # `route_request` is the real AstraRouter's full-pipeline
            # entry point (routing/failover/checkpoint/recovery/
            # supervision all run exactly as before — this only adds the
            # contract on top, per _maybe_supervise_result in
            # astra/ai/router.py). Zero-bypass: this is the ONLY AI
            # execution path for planning. A router duck-typed object that
            # only exposes the legacy `.route()` tuple interface is no
            # longer an alternate path to fall through to — it fails
            # planning clearly instead of silently taking another AI
            # route.
            if not hasattr(self.router, "route_request"):
                # Zero-bypass: `route_request()` (Gateway Request
                # Intelligence -> AstraRouter.route_request() -> Astra AI
                # Gateway -> Existing Provider -> AI Model) is the only
                # supported AI-backed planning path. A router object that
                # doesn't implement it is not a silently-degraded provider
                # to fall through to via the legacy `.route()` tuple
                # interface — it's a misconfiguration, and planning fails
                # clearly (no plan) rather than taking another AI path.
                return None
            contract = build_task_completion_contract(
                user_request=enriched_goal,
                goal="Produce a valid ordered step plan (JSON) for the "
                     "user's goal, or an \"answer\" step if no tool fits.",
                require_json=True, required_fields=("steps",))
            required_caps = []
            required_output_mods = []
            has_vision = False
            if attachments:
                try:
                    from astra.ai.capabilities import detect_required_input_capabilities
                    required_caps = detect_required_input_capabilities(attachments)
                except ImportError:
                    pass
                has_vision = any(a.get("family") == "image"
                                 for a in attachments)
            try:
                from astra.ai.capabilities import detect_required_output_capabilities
                from astra.ai.capabilities import OUTPUT_TEXT
                out_caps = detect_required_output_capabilities(enriched_goal)
                required_output_mods = [
                    c.replace("_generation", "")
                    for c in out_caps if c != OUTPUT_TEXT]
            except ImportError:
                pass
            msg_content = prompt
            if attachments:
                try:
                    from astra.ai.multimodal_messages import (
                        build_multimodal_content, has_inline_content)
                    if has_inline_content(attachments):
                        msg_content = build_multimodal_content(prompt, attachments)
                except ImportError:
                    pass
            req = RoutingRequest(
                messages=[{"role": "user", "content": msg_content}],
                task_contract=contract,
                required_capabilities=required_caps or None,
                required_input_modalities=(
                    ["image"] if has_vision else []),
                required_output_modalities=required_output_mods or None,
                vision=has_vision)
            rr = self.router.route_request(req)
            self.last_completion_status = rr.completion_status
            if not rr.ok or not rr.text:
                if required_output_mods and "no eligible" in (rr.error or ""):
                    cap_names = ", ".join(required_output_mods)
                    return [self._answer(g,
                        f"Ei request er jonno {cap_names} generation dorkar, "
                        f"kintu kono configured Provider/Model ei capability "
                        f"support kore na. Apni ekta compatible model configure "
                        f"korun (jemon: stable-diffusion-xl on Bedrock for image).")]
                return None
            text = rr.text
            # Lenient parse: the Gateway's own JSON check already passed
            # (or the router wouldn't report ok+text), but that check now
            # tolerates a ```json fence/preamble too (see
            # astra/ai/json_extract.py), so we must parse the same way
            # here rather than a bare json.loads that only trims stray
            # backtick characters.
            data = loads_lenient(text)
            return self.parse_plan_json(data, max_steps=max_steps) or None
        except Exception:
            return None

    def parse_plan_json(self, data: dict, max_steps: int = 6) -> list[dict]:
        """Turn an already-JSON-decoded `{"steps":[...]}` payload into
        executor-ready step dicts (tool allowlist, dependency filtering,
        id issuance) — the same logic `_ai_steps` uses for the initial
        plan, factored out so a post-execution Gateway correction
        round-trip (Orchestrator._gateway_final_task_verification) can
        turn a corrective AI reply into additional steps without
        duplicating this parsing."""
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

