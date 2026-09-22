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
from dataclasses import dataclass, field

from astra.ai.execution_history import AgentExecutionHistory
from astra.ai.json_extract import loads_lenient
from astra.core.context import ToolContext
from astra.core.events import new_op_id

DEFAULT_MAX_STEPS = 8
DEFAULT_MAX_TOOL_RESULT_CHARS = 6000
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
- Never invent tool output. Never mention this protocol, tools or internal machinery in the final answer."""


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
        lines.append(f"- {t['name']} ({t.get('category', '')}): {desc}")
        if len(lines) >= max_tools:
            break
    return "\n".join(lines) or "(no tools available)"


def _parse_action(text: str):
    """Return a dict for a well-formed protocol action, else None."""
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = raw.strip("`")
        nl = raw.find("\n")
        if nl != -1:
            raw = raw[nl + 1:]
        if raw.endswith("```"):
            raw = raw[:-3]
    if not (raw.startswith("{") and raw.endswith("}")):
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


def _cap(text: str, limit: int) -> str:
    if limit and len(text) > limit:
        return text[:limit] + "…(truncated)"
    return text


class ToolCaller:
    """A brain that can answer one model call. Implemented for the Gateway
    and for the Provider system so the loop is identical for both."""

    name = "caller"

    def chat(self, messages: list, *, max_tokens: int = 1500,
             trace: str = "") -> str:
        raise NotImplementedError


class GatewayToolCaller(ToolCaller):
    name = "gateway"

    def __init__(self, gateway, *, category: str = "tool_use"):
        self.gateway = gateway or None
        self.category = category

    def chat(self, messages, *, max_tokens=1500, trace=""):
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

    def chat(self, messages, *, max_tokens=1500, trace=""):
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
                 max_tool_result_chars: int = DEFAULT_MAX_TOOL_RESULT_CHARS,
                 execution_history: AgentExecutionHistory | None = None):
        self.registry = registry
        self.terminal = terminal
        self.events = events
        self.max_steps = max(1, int(max_steps))
        self.max_tool_result_chars = int(max_tool_result_chars)
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
            scope: str | None = None, max_tokens: int = 1500,
            trace: str = "") -> ToolLoopResult:
        catalog = build_tool_catalog(self.registry)
        protocol = TOOL_PROTOCOL.replace("{catalog}", catalog)
        system = (system_prompt or "").strip()
        system = (system + "\n\n" + protocol) if system else protocol

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
            if isinstance(turn, dict) and turn.get("content"):
                messages.append({"role": turn.get("role") or "user",
                                 "content": turn["content"]})
        messages.append({"role": "user", "content": user_content})

        ctx = ToolContext(registry=self.registry, events=self.events,
                          terminal=self.terminal,
                          terminal_session_id=session_id)
        op = new_op_id()
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
                           error=err, terminal=True)
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
                           terminal=True)
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
            if self.execution_history is not None and scope is not None:
                self.execution_history.record(
                    scope, tool, ok=step.ok, status=step.status,
                    result=outcome, args=args, step=index)
            self._emit("agent.tool_result", op=op, trace=trace, step=index,
                       tool=tool, ok=step.ok, status=step.status,
                       duration_ms=outcome.get("duration_ms"))

            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content":
                             "Tool result:\n" + _cap(
                                 json.dumps(outcome, ensure_ascii=False,
                                            default=str),
                                 self.max_tool_result_chars)})
            self._emit("agent.tool_loop.step", op=op, trace=trace, step=index,
                       tool=tool, ok=step.ok, terminal=False)

        self._emit("agent.tool_loop.finished", op=op, trace=trace,
                   scope=scope or "", steps=len(steps), tool_calls=tool_calls,
                   stopped_reason="max_steps", terminal=True)
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
