"""Iterative AI tool loop — the Agent's real multi-step execution.

Until now a chat turn was one model call: the Provider produced text and
the Gateway verified it. That cannot run a development workflow ("fix the
failing tests": inspect, run, read, edit, re-run, ...). This module adds
the missing loop, without introducing a second tool framework:

    AI call
      -> model decides: use a tool, or answer
      -> if tool: ToolRegistry.execute(...)   (the ONE registry)
      -> structured result goes back into the SAME conversation
      -> AI call again
      -> ... until the model answers or the bounded step budget is spent

The brain driving the loop is behind `ToolCaller`, so the identical loop
runs with either brain:

* `ProviderToolCaller` — the existing Provider system via `AstraRouter`
  (the default for a chat turn; the provider/model that owns the work
  decides which tools to use).
* `GatewayToolCaller` — the AI Gateway's own connections
  (`AstraAIGateway.run_tool_loop`).

Both reach the SAME `ToolRegistry` and therefore the SAME shared Terminal
— see `astra.terminal`. There is no provider-specific tool code.

The AI chooses dynamically: the loop hard-codes no command sequence. A
failed command is returned as structured data (status/exit_code/stderr),
so the model can read it, change approach, edit files and retry.

Models without native tool-calling are supported through a tiny JSON
protocol (see `TOOL_PROTOCOL`). A model that ignores the protocol and
answers in plain text is treated as final — that keeps ordinary chat
working unchanged.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from astra.ai.execution_history import AgentExecutionHistory
from astra.ai.json_extract import loads_lenient
from astra.ai.system_prompt import ASTRA_CORE_SYSTEM_PROMPT, build_system_prompt
from astra.core.context import ToolContext
from astra.core.events import new_op_id

DEFAULT_MAX_STEPS = 8
# None => NO artificial cap on the tool result fed back to the model. The
# result is kept whole whenever the provider's real context window permits
# (provider-aware fitting happens at the router/gateway boundary — see
# astra.ai.context_budget); only an explicit caller override re-imposes a
# character cap.
DEFAULT_MAX_TOOL_RESULT_CHARS = None
DEFAULT_MAX_TOOLS_IN_PROMPT = 40
DEFAULT_MAX_DESC_CHARS = 160

TOOL_PROTOCOL = """You are Astra's coding agent. You may use tools to inspect and change the workspace, run commands and tests, and only then answer.

Available tools (call them by exact name):
{catalog}

Reply with ONE JSON object and nothing else when you want to use a tool:
{{"action": "tool", "tool": "<tool name>", "args": {{...}}, "thought": "<one short line>"}}

When the task is done, reply with:
{{"action": "final", "answer": "<the complete final answer for the user>"}}

Rules:
- Choose the NEXT action yourself; there is no fixed sequence. Inspect before editing, and test after editing.
- A failed command is not fatal: its structured result (status, exit_code, stdout/stderr) is given back to you — read it, fix the cause, and try again.
- Prefer the terminal for shell/git/test work and the file tools for reading/editing files.
- Only use a tool when it is genuinely useful. If no tool is needed, answer directly in plain text (no JSON necessary).
- Never invent tool output. Never mention this JSON action protocol, exact tool names, argument schemas, or other internal machinery in the final answer.
- If the Gateway's execution decision (in the system prompt or the task context) says this request requires a capability, you MUST actually invoke the tool that performs it and use its real result before answering. Never reply with instructions describing how the user could do it themselves instead of doing it.
- If asked what you can do or which tools/capabilities are available, that is NOT a request to invent or to stay silent: answer from the runtime capability catalog you were given (in the system prompt, above this protocol) in clean, practical, plain language — never the literal tool names in this protocol, and never a category that catalog doesn't list."""


def build_tool_catalog(registry, *, categories=None, max_tools=DEFAULT_MAX_TOOLS_IN_PROMPT,
                       max_desc=DEFAULT_MAX_DESC_CHARS) -> str:
    """Compact, deterministic tool catalogue for the protocol prompt."""
    if registry is None:
        return "(no tools available)"
    try:
        tools = registry.list()
    except Exception:
        return "(no tools available)"
    lines = []
    for t in tools:
        if categories and t.get("category") not in categories:
            continue
        desc = " ".join(str(t.get("description") or "").split())
        if len(desc) > max_desc:
            desc = desc[:max_desc] + "…"
        line = f"- {t['name']} ({t.get('category', '')}): {desc}"
        args = _format_args_schema(t.get("input_schema"))
        if args:
            line += f" args: {args}"
        lines.append(line)
        if len(lines) >= max_tools:
            break
    return "\n".join(lines) or "(no tools available)"


def _format_args_schema(schema, *, max_args: int = 8) -> str:
    """Compact, deterministic rendering of one tool's live input schema —
    the exact argument names/types the model must use, taken straight from
    `Tool.describe()["input_schema"]` (never a hand-maintained second copy).

    This is the one place the model learns a tool's real arguments without
    native tool-calling; without it the model has only the tool's name and
    description and has to guess `{"command": ...}` vs `{"cmd": ...}`.
    """
    if not isinstance(schema, dict) or not schema:
        return ""
    # Two shapes exist on the live registry: a flat {"arg": {"type": ...,
    # "required": bool}} map (builtins/terminal/web3) and a full JSON-Schema
    # object {"type": "object", "properties": {...}, "required": [...]}
    # (browser tools). Normalise both before rendering.
    required = set()
    if isinstance(schema.get("properties"), dict):
        props = schema["properties"]
        if isinstance(schema.get("required"), (list, tuple)):
            required = {str(r) for r in schema["required"]}
    else:
        props = schema
        required = {str(k) for k, v in schema.items()
                    if isinstance(v, dict) and v.get("required")}
    parts = []
    for name in sorted(props):
        spec = props.get(name) if isinstance(props.get(name), dict) else {}
        typ = str(spec.get("type") or "any")
        parts.append(f"{name}:{typ}" + ("" if name in required else "?"))
        if len(parts) >= max_args:
            break
    return "{" + ", ".join(parts) + "}"


def _parse_action(text: str):
    """Return a dict for a well-formed protocol action, else None.

    ROOT CAUSE (see astra/ai/response_boundary.py for the full writeup):
    the model's tool-call/final JSON is not always returned as a bare,
    whole-string JSON object — chat-tuned models routinely wrap it in a
    ```json fence or, far more commonly, add a short preamble/trailing
    sentence around it, e.g.:

        I'll clone that repository for you.

        {"action": "tool", "tool": "terminal_exec", "args": {...}, ...}

    `loads_lenient` (imported above) already knows how to find a JSON
    object embedded anywhere in a string. The previous version of this
    function gated the call behind
    `raw.startswith("{") and raw.endswith("}")` — which is false for
    every prefaced/suffixed reply like the example above, so
    `loads_lenient` was never actually reached for them. The effect: the
    whole raw reply — internal tool-call JSON included (tool/args/
    session_id/thought) — was treated as the model's final
    natural-language answer and returned straight to the user instead of
    being executed. That is the exact "internal tool call leak" this
    function must prevent. Always attempt `loads_lenient` first; only
    fall back to "not an action" (plain-text final answer) when no JSON
    object can be found in the text at all, or it doesn't carry a
    recognized `action`.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        data = loads_lenient(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    action = data.get("action")
    if action == "tool" and data.get("tool"):
        return data
    if action == "final":
        return data
    return None


def _redact_text(text: str) -> str:
    """Best-effort secret redaction (`astra.security.redact_text`), never
    fatal: a redaction failure must not break a tool result."""
    try:
        from astra.security import redact_text
        return redact_text(text)
    except Exception:
        return text


def _cap(text: str, limit: int) -> str:
    if limit and len(text) > limit:
        return text[:limit] + "…(truncated)"
    return text


class ToolCaller:
    """A brain that can answer one model call. Implemented for the Gateway
    and for the Provider system so the loop is identical for both."""

    name = "caller"

    def chat(self, messages: list, *, max_tokens: int | None = None,
             trace: str = "") -> str:
        raise NotImplementedError


class GatewayToolCaller(ToolCaller):
    name = "gateway"

    def __init__(self, gateway, *, category: str = "tool_use"):
        self.gateway = gateway or None
        self.category = category

    def chat(self, messages, *, max_tokens=None, trace=""):
        return self.gateway.chat(messages, max_tokens=max_tokens,
                                 category=self.category, trace=trace)


class ProviderToolCaller(ToolCaller):
    """Executes through the existing Provider system (AstraRouter), using
    the same provider/model the Gateway assigned for this turn."""

    name = "provider"

    def __init__(self, router, *, task_type: str = "coding", vision: bool = False,
                 provider: str | None = None, model: str | None = None,
                 no_fallback: bool = False, trace: str = ""):
        self.router = router
        self.task_type = task_type
        self.vision = vision
        self.provider = provider or None
        self.model = model or None
        self.no_fallback = bool(no_fallback)
        self.trace = trace
        self.last_result = None
        self.last_error = ""

    def _request(self, messages, max_tokens, trace, task_type):
        from astra.ai.router import RoutingRequest

        return self.router.route_request(RoutingRequest(
            task_type=task_type, messages=messages,
            preferred_provider=self.provider, preferred_model=self.model,
            vision=self.vision, max_tokens=max_tokens,
            no_fallback=self.no_fallback, trace=trace or self.trace))

    def chat(self, messages, *, max_tokens=None, trace=""):
        rr = self._request(messages, max_tokens, trace, self.task_type)
        if (rr is None or not rr.ok) and self.task_type not in ("simple_chat",
                                                                "vision") \
                and "no eligible" in (getattr(rr, "error", "") or ""):
            # A hard capability filter left nothing to run on; a plain chat
            # turn can still be served by any model. Mirrors the pipeline's
            # own `_route` fallback so the loop never dies on a filter.
            rr = self._request(messages, max_tokens, trace, "simple_chat")
        if rr is None or not rr.ok:
            from astra.core.exceptions import ProviderError
            self.last_error = (rr.error if rr is not None else "") or \
                "provider call failed"
            raise ProviderError(self.last_error)
        self.last_result = rr
        return rr.text


@dataclass
class LoopStep:
    index: int
    action: str
    tool: str = ""
    args: dict = field(default_factory=dict)
    thought: str = ""
    ok: bool = False
    status: str = ""
    result: dict = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict:
        return {"index": self.index, "action": self.action, "tool": self.tool,
                "args": self.args, "thought": self.thought, "ok": self.ok,
                "status": self.status, "error": self.error,
                "result": _brief_result(self.result)}


@dataclass
class ToolLoopResult:
    text: str
    ok: bool = True
    steps: list = field(default_factory=list)
    tool_calls: int = 0
    stopped_reason: str = "final"
    error: str = ""
    messages: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "text": self.text, "tool_calls": self.tool_calls,
                "stopped_reason": self.stopped_reason, "error": self.error,
                "steps": [s.to_dict() for s in self.steps]}


class AgentToolLoop:
    def __init__(self, registry, *, terminal=None, events=None,
                 max_steps: int = DEFAULT_MAX_STEPS,
                 max_tool_result_chars: int | None = DEFAULT_MAX_TOOL_RESULT_CHARS,
                 execution_history: AgentExecutionHistory | None = None):
        self.registry = registry
        self.terminal = terminal
        self.events = events
        self.max_steps = max(1, int(max_steps))
        self.max_tool_result_chars = (None if max_tool_result_chars is None
                                      else int(max_tool_result_chars))
        self.execution_history = execution_history or AgentExecutionHistory()

    # -- events --------------------------------------------------------------
    def _emit(self, kind: str, **data) -> None:
        if self.events is None:
            return
        try:
            self.events.emit(kind, agent="agent.tool_loop", **data)
        except Exception:
            pass

    # -- the loop ------------------------------------------------------------
    def run(self, task, caller: ToolCaller, *, system_prompt: str,
            history=None, context_blocks=None, session_id: str | None = None,
            scope: str | None = None, max_tokens: int | None = None,
            trace: str = "") -> ToolLoopResult:
        catalog = build_tool_catalog(self.registry)
        protocol = TOOL_PROTOCOL.replace("{catalog}", catalog)
        base = (system_prompt or "").strip()
        # `base` is normally already Core+specialized (e.g. chat_pipeline's
        # PROVIDER_SYSTEM_PROMPT / gateway.py's Gateway prompts, each built
        # once via `build_system_prompt`). Only wrap it here if the caller
        # passed a bare specialized prompt with no Core layer yet — this
        # keeps the Core prompt present exactly once no matter which caller
        # invokes the tool loop, without ever duplicating it.
        if base and ASTRA_CORE_SYSTEM_PROMPT.strip() not in base:
            base = build_system_prompt(base)
        elif not base:
            base = ASTRA_CORE_SYSTEM_PROMPT.strip()
        system = base + "\n\n" + protocol

        blocks = [b for b in (context_blocks or []) if b]
        block_text = "\n\n".join(blocks)
        # `task` may be a multimodal content list (text + image/audio/
        # document parts). It must be preserved as parts — stringifying it
        # would silently drop every attachment — with the context blocks
        # appended as an extra text part.
        if isinstance(task, list):
            user_content = list(task)
            if block_text:
                user_content.append({"type": "text", "text": block_text})
        else:
            user_text = task if isinstance(task, str) else str(task)
            user_content = (user_text + "\n\n" + block_text
                            if block_text else user_text)

        messages: list[dict] = [{"role": "system", "content": system}]
        for turn in history or []:
            # Guard against a stray "system" role turn in prior conversation
            # history ever inserting a second system message alongside the
            # one built above — the Core+specialized system prompt must
            # appear exactly once per request.
            role = turn.get("role") or "user" if isinstance(turn, dict) else "user"
            if isinstance(turn, dict) and turn.get("content") and role != "system":
                messages.append({"role": role, "content": turn["content"]})
        messages.append({"role": "user", "content": user_content})

        ctx = ToolContext(registry=self.registry, events=self.events,
                          terminal=self.terminal,
                          terminal_session_id=session_id,
                          execution_history=self.execution_history,
                          execution_scope=scope)
        op = new_op_id()
        # The loop's own start, so its terminal event can report a real
        # duration (sub-second accurate) instead of the row inheriting a
        # per-tool `duration_ms` from a progress event — which is how a
        # ~108s tool loop once rendered as "COMPLETE · 3ms".
        started = time.monotonic()
        self._emit("agent.tool_loop.started", op=op, trace=trace,
                   scope=scope or "", session_id=session_id or "",
                   max_steps=self.max_steps, tools=len(
                       (self.registry.list() if self.registry else [])))
        steps: list[LoopStep] = []
        tool_calls = 0
        last_text = ""
        for index in range(self.max_steps):
            try:
                raw = caller.chat(messages, max_tokens=max_tokens, trace=trace)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                self._emit("agent.tool_loop.failed", op=op, trace=trace,
                           error=err, terminal=True,
                           duration_ms=round(
                               (time.monotonic() - started) * 1000.0, 2))
                return ToolLoopResult(text=last_text, ok=False, steps=steps,
                                      tool_calls=tool_calls,
                                      stopped_reason="error", error=err,
                                      messages=messages)
            last_text = raw or last_text
            action = _parse_action(raw)
            if action is None or action.get("action") != "tool":
                text = ""
                if action and action.get("action") == "final":
                    text = str(action.get("answer") or "").strip()
                text = text or (raw or "").strip()
                self._emit("agent.tool_loop.finished", op=op, trace=trace,
                           scope=scope or "", steps=len(steps),
                           tool_calls=tool_calls, stopped_reason="final",
                           terminal=True,
                           duration_ms=round(
                               (time.monotonic() - started) * 1000.0, 2))
                return ToolLoopResult(text=text, ok=True, steps=steps,
                                      tool_calls=tool_calls,
                                      stopped_reason="final", messages=messages)

            tool = str(action.get("tool") or "").strip()
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            thought = str(action.get("thought") or "").strip()
            step = LoopStep(index=index, action="tool", tool=tool, args=args,
                            thought=thought)
            steps.append(step)
            tool_calls += 1
            self._emit("agent.tool_call", op=op, trace=trace, step=index,
                       tool=tool, args=_safe_args(args))

            outcome = self._execute(tool, args, ctx, trace=trace, op=op)
            step.ok = bool(outcome.get("ok"))
            step.status = outcome.get("status", "")
            step.error = outcome.get("error", "")
            step.result = outcome
            self._emit("agent.tool_result", op=op, trace=trace, step=index,
                       tool=tool, ok=step.ok, status=step.status,
                       tool_duration_ms=outcome.get("duration_ms"))

            # Redact credential-shaped values from the tool result BEFORE it
            # is fed back into the model conversation and the execution
            # history: a command or error message can echo a token
            # (`git clone https://user:TOKEN@...`), and that must never
            # reach a model prompt, the persisted execution history or a
            # later final answer.
            result_json = _redact_text(json.dumps(outcome, ensure_ascii=False,
                                                  default=str))
            if self.execution_history is not None and scope is not None:
                self.execution_history.record(
                    scope, tool, ok=step.ok, status=step.status,
                    result=result_json, args=args, step=index)
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content":
                             "Tool result:\n" + _cap(
                                 result_json, self.max_tool_result_chars)})
            self._emit("agent.tool_loop.step", op=op, trace=trace, step=index,
                       tool=tool, ok=step.ok, terminal=False)

        self._emit("agent.tool_loop.finished", op=op, trace=trace,
                   scope=scope or "", steps=len(steps), tool_calls=tool_calls,
                   stopped_reason="max_steps", terminal=True,
                   duration_ms=round(
                       (time.monotonic() - started) * 1000.0, 2))
        return ToolLoopResult(text=last_text, ok=True, steps=steps,
                              tool_calls=tool_calls,
                              stopped_reason="max_steps", messages=messages)

    def _execute(self, tool: str, args: dict, ctx, *, trace: str,
                 op: str) -> dict:
        if self.registry is None or self.registry.get(tool) is None:
            return {"ok": False, "error": f"unknown tool: {tool}",
                    "status": "error"}
        try:
            wrapped = self.registry.execute(tool, args, ctx=ctx, trace=trace)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "status": "error"}
        if not wrapped.get("ok"):
            return {"ok": False, "status": wrapped.get("decision", "error"),
                    "error": wrapped.get("reason") or "tool did not execute",
                    "decision": wrapped.get("decision", "")}
        result = wrapped.get("result")
        out = result if isinstance(result, dict) else {"value": result}
        out = dict(out)
        out.setdefault("ok", True)
        out["duration_ms"] = wrapped.get("duration_ms", 0.0)
        return out


def _brief_result(result, limit: int = 400) -> dict:
    """Compact, output-capped view of a tool result for the loop trace /
    Activity Log — never the full unbounded output."""
    if not isinstance(result, dict):
        return {"value": str(result)[:limit]}
    out = {}
    for key in ("status", "exit_code", "process_id", "session_id", "cwd",
                "path", "written"):
        if key in result:
            out[key] = result[key]
    for key in ("stdout", "stderr", "content"):
        if result.get(key):
            text = str(result[key])
            out[key] = text if len(text) <= limit else text[:limit] + "…"
    if "error" in result:
        out["error"] = str(result["error"])[:limit]
    return out


def _safe_args(args: dict, limit: int = 300) -> str:
    """Compact, redacted JSON view of a tool's args for the Activity Log.
    A model may legitimately run `export API_KEY=...` or a `curl -H
    Authorization:...`; the raw secret must never be persisted into the event
    log (the tool registry redacts its own events — this path must too)."""
    try:
        text = json.dumps(args, ensure_ascii=False, default=str)
    except Exception:
        text = str(args)
    try:
        from astra.security import redact_text
        text = redact_text(text)
    except Exception:
        pass
    return text[:limit] + ("…" if len(text) > limit else "")
