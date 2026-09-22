"""Astra chat pipeline — the single path every chat message takes.

    User message
      -> Gateway call #1  UNDERSTAND + ASSIGN
           - message incomplete?  -> Gateway completes it
           - message complete?    -> passed through unchanged (no "improving")
           - Gateway sees every usable provider/model and picks the best one
             for this job, and writes down what a 100%-complete answer needs
      -> Provider executes the work (its output is NOT shown to the user yet)
      -> Gateway call #2  VERIFY
           - the Gateway is handed everything call #1 decided (the request it
             assigned, the completion criteria, which provider/model got it)
             plus the provider's output, and judges: complete or not?
           - complete     -> the output goes to the user
           - not complete -> the Gateway tells the provider exactly what is
                             missing/wrong and whether to FIX it or REDO it
                             from scratch; the provider retries and the
                             Gateway verifies again
      -> User

The verify/fix loop is bounded (`astra.core.correction.MAX_CORRECTION_ATTEMPTS`)
so a stubborn provider can never spin forever. If the loop ends without the
Gateway confirming completion, the user still gets the best answer, together
with an honest note saying what is still missing — this pipeline never claims
100% unless the Gateway's last verification said so.

Building blocks reused (not re-implemented):
  - `AstraAIGateway.chat`            the Gateway's own AI connections
  - `AstraRouter.route_request`      provider execution + provider fallback
  - `AstraAIGateway.supervise_task`  bounded verify -> correct -> re-verify
                                     loop (astra.ai.gateway_task_completion)

Fail-open rules:
  - Gateway absent/unusable  -> the message goes straight to the router and the
                                answer is returned unverified (`data.gateway`
                                says so).
  - Call #1 fails            -> the raw message is used, routing is automatic.
  - Call #2 unusable         -> the provider's answer is returned with a note
                                that it could not be verified.
"""
from __future__ import annotations

import os
import tempfile

from astra.ai.artifact_extraction import detect_output_type, extract_artifacts
from astra.ai.capability_context import build_capability_context
from astra.ai.gateway_contract import (ProviderExecutionPort,
                                       ProviderExecutionResult,
                                       ProviderExecutionTarget)
from astra.ai.gateway_task_completion import (COMPLETE, FAILED, INCOMPLETE,
                                              build_task_completion_contract)
from astra.ai.json_extract import loads_lenient
from astra.ai.multimodal_messages import build_multimodal_content
from astra.ai.execution_history import AgentExecutionHistory
from astra.ai.router import RoutingRequest, RoutingResult, classify
from astra.ai.system_prompt import build_system_prompt
from astra.core.exceptions import ProviderError
from astra.core.events import new_op_id
from astra.terminal.manager import default_session_id_for

# Task types that are safe to hand the router for a plain chat turn.
# Everything else `classify()` can return (image/audio/video generation,
# structured_output -> forced JSON mode, browser/web3 -> tool territory)
# is served as ordinary chat here.
_CHAT_TASK_TYPES = frozenset({"simple_chat", "coding", "translation",
                              "summarization", "research", "planning"})

MAX_TARGETS_IN_PROMPT = 60
MAX_OUTPUT_CHARS_IN_VERIFY = 12000
# The Gateway picks ITS OWN model by keyword-classifying user-role text. These
# control prompts embed the provider catalogue ("...vision...") and provider
# output, which would trip the hard vision/json filters, so the pipeline
# states the category explicitly instead of letting it be guessed.
UNDERSTAND_MAX_TOKENS = 700
VERIFY_MAX_TOKENS = 600

# NOTE: each *_SYSTEM_PROMPT below is the specialized layer for its role
# only. The Astra Core System Prompt (identity, operating principles,
# tool-use/hallucination/recovery/internal-output rules shared by every AI
# call) lives in `astra.ai.system_prompt.ASTRA_CORE_SYSTEM_PROMPT` and is
# composed on top of each specialized layer, once, via `build_system_prompt`
# below — specialized prompts must never re-state identity/behavior rules
# the Core prompt already covers, and nothing else in this module (or any
# other caller) should inject the Core prompt a second time.
_PROVIDER_SPECIALIZED_PROMPT = (
    "Do the user's request fully and directly."
)

PROVIDER_SYSTEM_PROMPT = build_system_prompt(_PROVIDER_SPECIALIZED_PROMPT)

_UNDERSTAND_SPECIALIZED_PROMPT = (
    "You are the Astra AI Gateway. A user message arrives (it may be short, "
    "incomplete, or written in Bengali/Banglish/English). You do NOT answer "
    "it and you do NOT do the task — a Provider AI does that after you. You "
    "do three things and reply with ONE JSON object and nothing else.\n\n"
    "1) UNDERSTAND. Decide whether the message is complete.\n"
    "   - If it is incomplete (missing subject, vague reference like "
    "\"eita\"/\"that one\", cut-off sentence), rewrite it as a complete "
    "request. Use only what is in the message or the prior-conversation "
    "block; NEVER invent facts, names, numbers or intentions. If something "
    "essential truly cannot be inferred, keep the request as-is and add "
    "\"state clearly what information is missing instead of guessing\" to "
    "the criteria.\n"
    "   - If it is already complete, copy it EXACTLY into final_request. "
    "Do not improve, translate, restructure or expand a complete message.\n"
    "   - final_request is written in the user's voice, never as a reply.\n\n"
    "2) ASSIGN. From the provider/model list you are given, pick the single "
    "best provider+model for this job (coding -> a coding-capable model, "
    "hard reasoning -> a high-quality model, simple chat -> a fast one, "
    "images -> a vision model). Copy provider and model EXACTLY from the "
    "list. If nothing in the list is a clear fit, use \"\" for both.\n\n"
    "3) DEFINE DONE. List 1-5 short, checkable criteria a 100%-complete "
    "answer must satisfy.\n\n"
    "Reply with exactly this JSON shape:\n"
    "{\"final_request\": \"...\", \"was_incomplete\": true|false, "
    "\"provider\": \"...\", \"model\": \"...\", "
    "\"criteria\": [\"...\"], \"reason\": \"<one short line>\"}"
)

UNDERSTAND_SYSTEM_PROMPT = build_system_prompt(_UNDERSTAND_SPECIALIZED_PROMPT)

_VERIFY_SPECIALIZED_PROMPT = (
    "You are the Astra AI Gateway's verifier. Earlier you understood a user "
    "request, assigned it to a Provider AI and defined what a complete "
    "answer needs. The Provider has now produced an output that the user "
    "has NOT seen yet. Judge it against the user's request and the "
    "criteria. Do not answer the request yourself.\n\n"
    "Mark it \"complete\" only if it fully and correctly satisfies the "
    "request and every criterion, is in the user's language, contains the "
    "real output (not a promise, placeholder or description of what it "
    "would do), and does not leak internal machinery. If anything is "
    "missing, wrong, cut off or off-topic, mark it \"incomplete\".\n\n"
    "For an incomplete output choose the action:\n"
    "  - \"fix\":  most of it is usable; the provider must patch what is "
    "missing/wrong.\n"
    "  - \"redo\": it is mostly unusable; the provider must start again "
    "from scratch.\n"
    "Then write `instructions`: clear, specific, imperative text addressed "
    "to the provider saying exactly what to fix or why to redo it.\n\n"
    "Reply with ONE JSON object and nothing else:\n"
    "{\"verdict\": \"complete\"|\"incomplete\", \"missing\": [\"...\"], "
    "\"action\": \"fix\"|\"redo\", \"instructions\": \"...\"}"
)

VERIFY_SYSTEM_PROMPT = build_system_prompt(_VERIFY_SPECIALIZED_PROMPT)


# ── helpers ─────────────────────────────────────────────────────────────────
def _parse_json_object(raw: str) -> dict | None:
    try:
        data = loads_lenient((raw or "").strip())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _clean(text) -> str:
    return " ".join(str(text or "").split()).strip()


def _normalize_history(history) -> list[dict]:
    """Accepts `None`, a `ConversationContext`
    (astra.ai.conversation_context), or a plain list of
    {"role": "user"|"assistant", "content": str} dicts, and always returns
    the plain list form (chronological, oldest first) — the one shape the
    rest of this module deals with. Unknown/malformed entries are dropped
    rather than raising, so a bad history never breaks the whole turn."""
    if history is None:
        return []
    msgs = getattr(history, "messages", history)
    out = []
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        role = "user" if m.get("role") == "user" else "assistant"
        content = m.get("content")
        if not content:
            continue
        out.append({"role": role, "content": content})
    return out


def _history_as_text(hist_turns: list[dict]) -> str:
    lines = []
    for m in hist_turns:
        speaker = "User" if m["role"] == "user" else "Assistant"
        lines.append(f"{speaker}: {m['content']}")
    return "\n".join(lines)


def _describe_attachments(attachments) -> str:
    names = []
    for a in attachments or []:
        if isinstance(a, dict):
            name = a.get("original_filename") or a.get("filename") or "file"
            fam = a.get("family") or ""
        else:
            name = getattr(a, "original_filename", "") or "file"
            fam = getattr(a, "family", "")
        names.append(f"{name} ({fam})" if fam else str(name))
    return ", ".join(names)


def _cid_from_history(history):
    """Conversation id, when the caller handed us a ConversationContext
    (which carries one) instead of a bare list. Keeps terminal session and
    execution-history scoping correct even for callers that only pass the
    history object."""
    return getattr(history, "conversation_id", None)


def _with_tool_summary(messages: list, steps: list,
                       extra_context: str = "") -> list:
    """Clone provider messages and append a compact, bounded summary of the
    tool actions already taken — with NO tool protocol — plus the live
    terminal/execution context, for the Gateway's correction phase. The
    correcting model must see the same current state the loop saw."""
    clone = [dict(m) for m in (messages or [])]
    if not clone or clone[-1].get("role") != "user":
        return clone
    parts = []
    if steps:
        lines = []
        for s in steps:
            line = f"- {s.tool}: status={s.status or 'ok'}"
            code = (s.result.get("exit_code")
                    if isinstance(s.result, dict) else None)
            if code is not None:
                line += f", exit_code={code}"
            if not s.ok and s.error:
                line += f", error={s.error[:120]}"
            lines.append(line)
        parts.append("Tool actions ALREADY performed for this task (do not "
                     "repeat them; use their results):\n" + "\n".join(lines))
    extra = (extra_context or "").strip()
    if extra:
        parts.append("Current execution context (live terminal session + "
                     "actions already taken):\n" + extra)
    if not parts:
        return clone
    block = "\n\n" + "\n\n".join(parts)
    content = clone[-1].get("content")
    if isinstance(content, str):
        clone[-1]["content"] = content + block
    elif isinstance(content, list):
        clone[-1]["content"] = list(content) + [{"type": "text", "text": block}]
    return clone


def _has_image(attachments) -> bool:
    for a in attachments or []:
        fam = a.get("family") if isinstance(a, dict) else getattr(a, "family", "")
        if fam == "image":
            return True
    return False


class _ChatPort(ProviderExecutionPort):
    """Sends a correction back to the SAME provider/model that produced the
    output being corrected (never a different one — switching providers is
    the router's failover job, not the fix loop's). Remembers the last text
    it got back so the pipeline still has an answer if a later correction
    round fails."""

    def __init__(self, router, task_type: str, vision: bool, trace: str = ""):
        self._router = router
        self._task_type = task_type
        self._vision = vision
        self._trace = trace
        self.last_text = ""

    def execute(self, target, messages: list, max_tokens: int = 500,
                **kwargs) -> str:
        rr = self._router.route_request(RoutingRequest(
            task_type=self._task_type, messages=messages,
            preferred_provider=target.provider_id,
            preferred_model=target.model_id,
            vision=self._vision, max_tokens=max_tokens, no_fallback=True,
            trace=self._trace))
        if rr is None or not rr.ok:
            raise ProviderError(
                (rr.error if rr is not None else "") or "correction failed")
        self.last_text = rr.text
        return rr.text


class _GatewayPort(ProviderExecutionPort):
    """Sends a Gateway-driven turn's correction back to the Gateway (its own
    connections), so a gateway-brained loop is corrected by the same brain
    that produced it instead of falling back to the Provider system."""

    def __init__(self, gateway, trace: str = ""):
        self._gateway = gateway
        self._trace = trace
        self.last_text = ""

    def execute(self, target, messages: list, max_tokens: int = 500,
                **kwargs) -> str:
        text = self._gateway.chat(messages, max_tokens=max_tokens,
                                  category="general", trace=self._trace)
        if not (text or "").strip():
            raise ProviderError("gateway correction produced no output")
        self.last_text = text
        return text


# ── the pipeline ────────────────────────────────────────────────────────────
class ChatPipeline:
    def __init__(self, gateway, router, events=None, *,
                 max_tokens: int = 1500, registry=None, terminal=None,
                 execution_history=None, max_tool_steps: int = 8,
                 agent_brain: str = "provider"):
        self.gateway = gateway
        self.router = router
        self.events = events
        self.max_tokens = int(max_tokens or 1500)
        # Shared Terminal + tool surface. When wired (production, via
        # astra.bootstrap), the provider step becomes a real multi-step
        # agent tool loop over the SAME ToolRegistry the Gateway uses.
        # When a caller constructs the pipeline without them (many unit
        # tests), the original single-call provider path is used unchanged.
        self.registry = registry
        self.terminal = terminal
        self.max_tool_steps = max(1, int(max_tool_steps or 8))
        self.execution_history = execution_history or AgentExecutionHistory()
        # Which AI drives the agent tool loop: the Provider system (default)
        # or the Gateway's own connections ("gateway"). Both reach the same
        # ToolRegistry and therefore the same shared Terminal — see
        # astra/ai/agent_tool_loop.py.
        self.agent_brain = (str(agent_brain or "provider").strip().lower()
                            or "provider")

    def _tool_loop_usable(self) -> bool:
        if self.registry is None:
            return False
        try:
            return bool(self.registry.list("terminal"))
        except Exception:
            return False

    # -- plumbing ------------------------------------------------------------
    def _emit(self, kind: str, **data) -> None:
        if self.events:
            try:
                self.events.emit(kind, agent="chat.pipeline", **data)
            except Exception:
                pass

    def _gateway_usable(self) -> bool:
        try:
            return self.gateway is not None and bool(self.gateway.is_usable())
        except Exception:
            return False

    # -- step 1: Gateway call #1 (understand + assign) ------------------------
    def _understand(self, message: str, context: str, attachments,
                    req: str = "", extra_context: str = "") -> dict:
        """Returns {"final_request", "was_incomplete", "provider", "model",
        "criteria", "reason", "ok"}. `ok` False means the call failed and
        the raw message is being used as-is."""
        fallback = {"final_request": message, "was_incomplete": False,
                    "provider": "", "model": "", "criteria": [],
                    "reason": "", "ok": False}
        targets = []
        try:
            targets = self.router.available_targets()
        except Exception:
            pass
        catalogue = "\n".join(
            f"- provider={t['provider']} model={t['model']} "
            f"caps={','.join(t.get('capabilities') or []) or 'chat'} "
            f"quality={t.get('quality') or '?'} ctx={t.get('context_window') or '?'}"
            for t in targets[:MAX_TARGETS_IN_PROMPT]) or "(none listed)"
        parts = ["Available providers/models:\n" + catalogue]
        ctx = _clean(context)
        if ctx:
            parts.append("Prior conversation (only to resolve references in the "
                         "message; not a new request):\n" + ctx)
        # Terminal session state + agent/tool execution history: the Gateway
        # must see the same execution context the Provider sees (see the
        # module docstring and astra/ai/execution_history.py).
        extra = (extra_context or "").strip()
        if extra:
            parts.append("Current execution context (terminal session + "
                         "actions already taken):\n" + extra)
        att = _describe_attachments(attachments)
        if att:
            parts.append("Attachments sent with the message: " + att)
        parts.append("User message:\n" + message)
        try:
            raw = self.gateway.chat(
                [{"role": "system", "content": UNDERSTAND_SYSTEM_PROMPT},
                 {"role": "user", "content": "\n\n".join(parts)}],
                max_tokens=UNDERSTAND_MAX_TOKENS, category="general",
                trace=req)
        except Exception as e:
            self._emit("chat.pipeline.understand_failed", error=str(e),
                       request=req, trace=req)
            return fallback
        data = _parse_json_object(raw)
        rewrote = bool((data or {}).get("was_incomplete"))
        if not data or (rewrote and not _clean(data.get("final_request"))):
            self._emit("chat.pipeline.understand_failed",
                       error="unparsable Gateway reply", request=req, trace=req)
            return fallback

        provider, model = _clean(data.get("provider")), _clean(data.get("model"))
        valid = {(t["provider"], t["model"]) for t in targets}
        if (provider, model) not in valid:
            # Never trust an invented target. Keep a provider-only pick when
            # the provider itself is real; otherwise leave routing automatic.
            provider = provider if any(p == provider for p, _ in valid) else ""
            model = ""
        criteria = [_clean(c) for c in (data.get("criteria") or [])
                    if _clean(c)][:5]
        return {"final_request": _clean(data["final_request"]) if rewrote
                else message,
                "was_incomplete": rewrote,
                "provider": provider, "model": model, "criteria": criteria,
                "reason": _clean(data.get("reason")), "ok": True}

    # -- step 3: Gateway call #2 (verify) -------------------------------------
    def _make_verifier(self, brief: dict, state: dict, req: str = "",
                       session_id=None, scope=None):
        """Semantic verifier for `supervise_task`. It closes over `brief`, so
        verification knows everything call #1 decided — and it is shown the
        same current terminal/execution context the Provider saw, so it can
        judge whether the work is actually done rather than guess."""
        def verifier(contract, result, evidence):
            state["verifications"] += 1
            output = (result.text or "")[:MAX_OUTPUT_CHARS_IN_VERIFY]
            crit = "\n".join(f"- {c}" for c in brief["criteria"]) or "- (none)"
            exec_ctx = self.execution_history.context_text(scope or "") \
                if scope else ""
            term_ctx = self._terminal_context(session_id) if session_id else ""
            extra = "\n\n".join(b for b in (term_ctx, exec_ctx) if b)
            prompt = (
                f"Original user message:\n{brief['raw']}\n\n"
                f"Request you assigned (call #1):\n{brief['final_request']}\n\n"
                f"Completion criteria you defined:\n{crit}\n\n"
                f"Assigned to: {brief['assigned'] or 'automatic routing'}\n\n"
                f"Provider output to verify:\n{output}")
            if extra:
                prompt += ("\n\nExecution context (live terminal session + "
                           "tool actions already taken; use it only to judge "
                           "whether the work is really done):\n" + extra)
            try:
                raw = self.gateway.chat(
                    [{"role": "system", "content": VERIFY_SYSTEM_PROMPT},
                     {"role": "user", "content": prompt}],
                    max_tokens=VERIFY_MAX_TOKENS, category="reasoning",
                    trace=req)
            except Exception as e:
                state["unavailable"] = str(e)
                return (FAILED, f"verification unavailable: {e}")
            data = _parse_json_object(raw)
            verdict = str((data or {}).get("verdict") or "").lower()
            if verdict not in ("complete", "incomplete"):
                state["unavailable"] = "unparsable verifier reply"
                return (FAILED, "verification unavailable: unparsable reply")
            self._emit("chat.pipeline.verified", verdict=verdict,
                       n=state["verifications"], request=req, trace=req)
            if verdict == "complete":
                return (COMPLETE, "")
            missing = [_clean(m) for m in (data.get("missing") or [])
                       if _clean(m)]
            instructions = _clean(data.get("instructions"))
            state["last_missing"] = missing
            return (INCOMPLETE, instructions or "; ".join(missing) or
                    "output is not complete",
                    {"missing": missing, "action": data.get("action"),
                     "instructions": instructions})
        return verifier

    # -- terminal context + agent tool loop ---------------------------------------
    def _terminal_context(self, session_id) -> str:
        """Deterministic, bounded view of the terminal session this
        conversation owns. Empty when no terminal is wired."""
        if self.terminal is None:
            return ""
        try:
            return self.terminal.context_text(session_id) or ""
        except Exception:
            return ""

    def _run_tool_loop(self, brief, hist_turns, ctx_text, task_type, vision,
                       scope, session_id, terminal_context, exec_context,
                       req, trace, base_messages=None, task_content=None):
        """Run the shared `AgentToolLoop` with the Provider system as the
        brain. Returns a `RoutingResult` (so the verify stage is unchanged)
        or None on failure. The loop's final message list is attached so
        Gateway corrections carry the tool exchanges too."""
        from astra.ai.agent_tool_loop import (AgentToolLoop,
                                              GatewayToolCaller,
                                              ProviderToolCaller)
        use_gateway = (self.agent_brain == "gateway" and self._gateway_usable())
        if use_gateway:
            caller = GatewayToolCaller(self.gateway, category="tool_use")
        else:
            caller = ProviderToolCaller(
                self.router, task_type=task_type, vision=vision,
                provider=brief["provider"] or None,
                model=brief["model"] or None, trace=req)
        loop = AgentToolLoop(self.registry, terminal=self.terminal,
                             events=self.events,
                             max_steps=self.max_tool_steps,
                             execution_history=self.execution_history)
        blocks = []
        if ctx_text and not hist_turns:
            blocks.append("Recent conversation (for reference):\n" + ctx_text)
        if terminal_context:
            blocks.append("Live terminal session state:\n" + terminal_context)
        if exec_context:
            blocks.append(exec_context)
        task = (task_content if task_content is not None
                else brief["final_request"])
        # Reuse the SAME per-turn system prompt `_run_turn` just built
        # (Core + specialized + live capability runtime context) rather
        # than the bare static PROVIDER_SYSTEM_PROMPT, so a capability
        # question is grounded identically whether or not it happens to
        # go through the tool loop. `AgentToolLoop.run` appends
        # TOOL_PROTOCOL's own (internal, machine-facing) catalog after
        # this — see that module. Falls back to the static constant only
        # for a caller that invokes this method directly with no
        # base_messages (none in this codebase today; defensive only).
        system_prompt = PROVIDER_SYSTEM_PROMPT
        if base_messages:
            first = base_messages[0]
            if isinstance(first, dict) and first.get("role") == "system":
                system_prompt = first.get("content") or system_prompt
        try:
            result = loop.run(task, caller,
                              system_prompt=system_prompt,
                              history=hist_turns, context_blocks=blocks,
                              session_id=session_id, scope=scope,
                              max_tokens=self.max_tokens, trace=req)
        except Exception as e:
            trace["error"] = f"{type(e).__name__}: {e}"
            self._emit("chat.pipeline.tool_loop_error", error=trace["error"],
                       op=f"chat:{req}", request=req, trace=req)
            return None
        trace["tool_loop"] = {"tool_calls": result.tool_calls,
                              "stopped_reason": result.stopped_reason,
                              "steps": [s.to_dict() for s in result.steps]}
        if not result.ok:
            trace["error"] = (result.error or getattr(caller, "last_error", "")
                              or "agent tool loop failed")
            return None
        if use_gateway:
            provider, model = "astra_ai_gateway", (self.gateway.last_model or "")
        else:
            last = getattr(caller, "last_result", None)
            provider = (getattr(last, "provider", "")
                        or (brief["provider"] or "agent"))
            model = getattr(last, "model", "") or (brief["model"] or "")
        rr = RoutingResult(ok=True, text=result.text, provider=provider,
                           model=model)
        rr._port_kind = "gateway" if use_gateway else "provider"
        # Corrections must NOT see the tool protocol (a correcting model
        # could otherwise reply with a tool call instead of a fixed answer).
        # They get the original prompt plus a compact summary of what the
        # tools already did, so it can fix a missing/incomplete answer.
        correction_ctx = "\n\n".join(
            b for b in (self._terminal_context(session_id),
                        self.execution_history.context_text(scope)) if b)
        rr._loop_messages = _with_tool_summary(base_messages or [],
                                               result.steps,
                                               extra_context=correction_ctx)
        return rr

    # -- helpers -----------------------------------------------------------------
    @staticmethod
    def _task_type(text: str, attachments) -> str:
        if _has_image(attachments):
            return "vision"
        t = classify(text)
        return t if t in _CHAT_TASK_TYPES else "simple_chat"

    def _route(self, task_type, messages, provider, model, vision, req=""):
        rr = self.router.route_request(RoutingRequest(
            task_type=task_type, messages=messages,
            preferred_provider=provider or None,
            preferred_model=model or None,
            vision=vision, max_tokens=self.max_tokens, trace=req))
        if (rr is None or not rr.ok) and task_type not in ("simple_chat", "vision") \
                and "no eligible" in (getattr(rr, "error", "") or ""):
            # A hard capability filter (e.g. "coding") left nothing to run
            # on — a plain chat turn can still be served by any model.
            rr = self.router.route_request(RoutingRequest(
                task_type="simple_chat", messages=messages,
                preferred_provider=provider or None,
                preferred_model=model or None, max_tokens=self.max_tokens,
                trace=req))
        return rr

    @staticmethod
    def _artifacts(text: str, message: str) -> list:
        try:
            d = os.path.join(tempfile.gettempdir(), "astra", "artifacts")
            return extract_artifacts(text, d, detect_output_type(message))
        except Exception:
            return []

    @staticmethod
    def _reply(text, ok, data, artifacts=None, note=""):
        """Build the {reply, action, ok, data[, artifacts]} shape returned
        to the caller (and, from there, straight into ChatLog + the chat
        UI's message bubble).

        `note`, when given, is an internal diagnostic caveat — Gateway
        verification failed/couldn't confirm, what was still "Missing: ...",
        etc. That text must NEVER reach the user-facing `reply` string: it
        used to be concatenated onto `text` here, which is exactly how
        debug/verification detail leaked into the chat UI. It is recorded
        under `data["internal_note"]` instead, alongside the rest of this
        turn's trace (`data["verification"]`), for the Activity Log /
        server logs only — see test_no_internal_debug_text_in_chat_reply
        in tests/test_chat_pipeline.py."""
        out = {"reply": text, "action": "none", "ok": ok, "data": data}
        if note:
            data["internal_note"] = note.strip()
        if artifacts:
            out["artifacts"] = artifacts
        return out

    # -- public entry point ---------------------------------------------------------
    def run(self, message: str, *, context: str = "", history=None,
            attachments=None, conversation_id=None,
            session_id=None) -> dict:
        """`history`, when given, is the canonical provider-independent
        conversation history for this turn (a list of {"role", "content"}
        dicts, oldest first — see `astra.ai.conversation_context`, or a
        `ConversationContext` instance directly). It is built ONCE by the
        caller (from the SAME ChatLog conversation the current message was
        just appended to) and is handed to both the Gateway's understand
        call and the Provider call below, so they never see divergent
        context. `context` (a plain string) is kept for backward
        compatibility with callers that have no structured history; when
        both are given, `history` wins."""
        raw = _clean(message)
        # Correlation id for this chat turn: every event, Gateway call and
        # Router request made below carries it, so the Activity Log can pair
        # each start with its terminal event and resolve any child operation
        # still "running" when the turn ends.
        req = new_op_id()
        if not raw and not attachments:
            return self._reply("Kichu likhun — ami help korte ready.", False,
                               {"stage": "input"})
        raw = raw or "(attachment only)"
        gateway_ok = self._gateway_usable()
        trace = {"gateway": "used" if gateway_ok else "unavailable",
                 "raw": raw, "request": req}
        self._emit("chat.pipeline.started", gateway=trace["gateway"],
                   op=f"chat:{req}", request=req, trace=req)

        hist_turns = _normalize_history(history)
        # The conversation id scopes BOTH the terminal session and the
        # agent/tool execution history, so unrelated conversations never
        # share cwd, processes or "what I already did".
        if conversation_id in (None, "", 0):
            conversation_id = _cid_from_history(history)

        # A caller with no conversation id has nowhere to persist terminal
        # state. Instead of falling back to ONE process-wide "default"
        # session (which would leak cwd/history/processes between unrelated
        # callers), such a turn gets its own request-scoped session that is
        # closed when the turn ends. Callers that need continuity without a
        # conversation id pass an explicit `session_id` and keep it.
        ephemeral_session = (not session_id
                             and conversation_id in (None, "", 0))
        if ephemeral_session:
            session_id = f"req-{req}"

        # Any unexpected error must still close this turn's root operation:
        # without a terminal event the "Request received" row would stay
        # "… running" in the Activity Log forever (reconciliation keys off
        # the correlation ids, it never filters running rows away).
        try:
            return self._run_turn(raw, context, hist_turns, attachments, req,
                                  gateway_ok, trace, conversation_id,
                                  session_id)
        except Exception as e:
            self._emit("chat.pipeline.failed",
                       error=f"{type(e).__name__}: {e}",
                       op=f"chat:{req}", request=req, trace=req,
                       terminal=True)
            raise
        finally:
            if ephemeral_session:
                self._close_session(session_id)

    def _close_session(self, session_id) -> None:
        """Close one request-scoped terminal session (best-effort)."""
        if self.terminal is None:
            return
        try:
            self.terminal.close(session_id)
        except Exception:
            pass

    def _run_turn(self, raw, context, hist_turns, attachments, req,
                  gateway_ok, trace, conversation_id=None,
                  session_id_override=None) -> dict:
        """The rest of one chat turn, split out of `run()` so it can
        guarantee a terminal event even when a step raises unexpectedly."""

        # A flattened text view of the SAME history, used everywhere a
        # single string is needed (the Gateway's understand prompt, and the
        # legacy `context` fallback below). Built once so the Gateway and
        # the Provider are always looking at identical prior turns.
        ctx_text = _history_as_text(hist_turns) if hist_turns else _clean(context)

        # Execution context (terminal session state + actions already taken)
        # is built ONCE and shown to BOTH the Gateway and the Provider.
        scope = (str(conversation_id) if conversation_id not in (None, "", 0)
                 else req)
        # An explicit session_id lets an embedder isolate callers that have
        # no conversation id (the web layer always has one). Without either,
        # fall back to a per-request id — never one process-wide "default"
        # session shared by unrelated callers.
        session_id = (str(session_id_override) if session_id_override
                      else default_session_id_for(conversation_id,
                                                  fallback=f"req-{req}"))
        terminal_context = self._terminal_context(session_id)
        exec_context = self.execution_history.context_text(scope)
        extra_context = "\n\n".join(
            b for b in (terminal_context, exec_context) if b)
        trace["terminal_session"] = session_id
        trace["scope"] = scope

        # 1) Gateway understands + assigns
        if gateway_ok:
            brief = self._understand(raw, ctx_text, attachments, req=req,
                                     extra_context=extra_context)
        else:
            brief = {"final_request": raw, "was_incomplete": False,
                     "provider": "", "model": "", "criteria": [],
                     "reason": "", "ok": False}
        assigned = (f"{brief['provider']}/{brief['model']}" if brief["model"]
                    else brief["provider"])
        trace.update({"understood": brief["final_request"],
                      "was_incomplete": brief["was_incomplete"],
                      "assigned": assigned, "criteria": brief["criteria"],
                      "assign_reason": brief["reason"]})
        self._emit("chat.pipeline.assigned", provider=brief["provider"],
                   model=brief["model"], was_incomplete=brief["was_incomplete"],
                   request=req, trace=req)

        # 2) Provider executes (output stays internal until verified)
        vision = _has_image(attachments)
        content = build_multimodal_content(brief["final_request"], attachments)
        # The tool/capability picture is derived LIVE from the ToolRegistry
        # on every turn (never hardcoded, never baked into the static Core
        # prompt) and supplied as this call's runtime context — the same
        # Core+specialized+runtime-context composition `build_system_prompt`
        # already defines, just actually used here. See
        # astra.ai.capability_context for what is/isn't included, and
        # tests/test_system_prompt.py's CapabilityQuestion* tests for the
        # regression coverage this closes.
        capability_ctx = build_capability_context(self.registry)
        provider_system_prompt = build_system_prompt(
            _PROVIDER_SPECIALIZED_PROMPT, runtime_context=capability_ctx)
        messages = [{"role": "system", "content": provider_system_prompt}]
        if hist_turns:
            # The SAME canonical history the Gateway just saw, as real
            # conversation turns (not squashed into the current message) —
            # this is what lets the Provider itself resolve a bare "why?"
            # or "continue" against what it said last time.
            messages.extend(hist_turns)
        elif ctx_text:
            # Legacy path: only a flat `context` string was supplied (no
            # structured history) — fall back to embedding it in the
            # current turn's text, exactly as before this fix.
            content = build_multimodal_content(
                "Recent conversation (for reference):\n" + ctx_text +
                "\n\nCurrent request:\n" + brief["final_request"], attachments)
        messages.append({"role": "user", "content": content})
        task_type = self._task_type(brief["final_request"], attachments)

        # The Provider is the AI that does the work. With the shared
        # Terminal/tool surface wired, that work is a real multi-step agent
        # tool loop (inspect -> run -> read failure -> edit -> retest) whose
        # results return to the SAME execution. Without it, the original
        # single provider call is used unchanged.
        if self._tool_loop_usable():
            rr = self._run_tool_loop(
                brief, hist_turns, ctx_text, task_type, vision, scope,
                session_id, terminal_context, exec_context, req, trace,
                messages, content)
        else:
            rr = self._route(task_type, messages, brief["provider"],
                             brief["model"], vision, req=req)
        if rr is None or not rr.ok:
            err = (trace.get("error") or getattr(rr, "error", "") or
                   "unknown error")
            trace["error"] = err
            self._emit("chat.pipeline.failed", error=err, op=f"chat:{req}",
                       request=req, trace=req, terminal=True)
            return self._reply(
                "Provider theke kono uttor pawa jayni. Kichukkhon pore abar "
                f"try korun. (`{err}`)", False, trace)
        trace["served_by"] = f"{rr.provider}/{rr.model}"
        # Corrections go back through the loop's final message list (which
        # carries the tool exchanges), not just the opening prompt.
        supervised_messages = getattr(rr, "_loop_messages", None) or messages

        # gateway can't verify -> honest pass-through
        if not gateway_ok:
            trace["verification"] = {"status": "skipped"}
            # terminal event: the request is done even though verification was
            # skipped, so its started row never stays "running" in the log.
            self._emit("chat.pipeline.finished", status="skipped",
                       op=f"chat:{req}", request=req, trace=req, terminal=True)
            return self._reply(rr.text, True, trace,
                               self._artifacts(rr.text, raw))

        # 3) Gateway verifies; fix/redo loop until complete or bound reached
        state = {"verifications": 0, "unavailable": "", "last_missing": []}
        verify_brief = {"raw": raw, "final_request": brief["final_request"],
                        "criteria": brief["criteria"],
                        "assigned": f"{rr.provider}/{rr.model}"}
        contract = build_task_completion_contract(
            user_request=raw, goal=brief["final_request"],
            completion_criteria=brief["criteria"], require_semantic=True)
        if getattr(rr, "_port_kind", "") == "gateway":
            port = _GatewayPort(self.gateway, trace=req)
        else:
            port = _ChatPort(self.router, task_type, vision, trace=req)
        port.last_text = rr.text
        target = ProviderExecutionTarget(rr.provider, rr.model)
        try:
            final, outcome, attempts = self.gateway.supervise_task(
                port, target, supervised_messages,
                ProviderExecutionResult(ok=True, text=rr.text), contract,
                semantic_verifier=self._make_verifier(
                    verify_brief, state, req=req, session_id=session_id,
                    scope=scope),
                max_tokens=self.max_tokens)
        except Exception as e:      # a Gateway bug must not lose a good answer
            self._emit("chat.pipeline.verify_error", error=str(e),
                       op=f"chat:{req}", request=req, trace=req, terminal=True)
            trace["verification"] = {"status": "error", "reason": str(e)}
            return self._reply(
                rr.text, True, trace, self._artifacts(rr.text, raw),
                note="\n\n⚠️ Gateway verification kaj korenni — uttor ta "
                     "verify kora hoyni.")

        text = (final.text if final.ok and (final.text or "").strip()
                else port.last_text) or rr.text
        trace["verification"] = {"status": outcome.status, "attempts": attempts,
                                 "checks": state["verifications"],
                                 "reason": outcome.reason,
                                 "missing": outcome.missing or state["last_missing"]}
        self._emit("chat.pipeline.finished", status=outcome.status,
                   attempts=attempts, op=f"chat:{req}", request=req,
                   trace=req, terminal=True)
        arts = self._artifacts(text, raw)

        if outcome.status == COMPLETE:
            return self._reply(text, True, trace, arts)
        if state["unavailable"]:
            return self._reply(
                text, True, trace, arts,
                note="\n\n⚠️ Gateway ei uttor ta verify korte parenni, tai "
                     "100% confirm kora jayni.")
        missing = outcome.missing or state["last_missing"]
        detail = ("; ".join(missing) if missing else outcome.reason or
                  "kichu ongsho ekhono bakhi")
        tried = (f" ({attempts} bar fix korar chesta kora hoyeche)"
                 if attempts else "")
        return self._reply(
            text, True, trace, arts,
            note=f"\n\n⚠️ Gateway verification e ekhono 100% confirm hoyni"
                 f"{tried}. Missing: {detail}")
