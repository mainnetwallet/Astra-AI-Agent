"""The approval-aware HOST terminal fallback (§1, §2, §14).

Architecture (there is exactly one path to a host command):

    AgentToolLoop -> HostTerminalFallback -> ApprovalManager
                  -> Assistant Chat approval -> trusted terminal_exec

Never:

    AgentToolLoop -> raw terminal_exec        (structurally refused)

The agent-reachable tool registered here is `host_terminal_request`. It
**executes nothing**: it records a scoped approval request and returns a
structured `approval_required` result. The host command runs later, and
only if the user selects *Allow* on the Assistant Chat card — at which
point `ApprovalManager` calls the trusted executor, which is the existing
`terminal_exec` tool executed through the existing ToolRegistry with a
NON-agent context (`agent_execution=False`), exactly as Astra's own
diagnostics do.

Why not just let the model call `terminal_exec` after an approval?
Because the model would then be free to send a *different* command than
the one the user approved. Approval authorises one exact command + cwd
(§5), and only this module's executor can act on it.
"""
from __future__ import annotations

from astra.core.exceptions import ValidationError
from astra.core.permissions import Level
from astra.terminal.approval import ENVIRONMENT_HOST
from astra.tools.schemas import Tool

FALLBACK_RISK = Level.SYSTEM_ACTION

# The command that actually runs on the host is executed by THIS tool name
# through the existing registry/policy layer.
HOST_EXEC_TOOL = "terminal_exec"

_RUNTIME_NOTE = (
    "Astra Agent Runtime is the primary execution environment. Request host "
    "fallback only when the isolated runtime genuinely cannot perform the "
    "operation.")

NO_APPROVAL_NOTE = (
    "The host command has NOT run. Action: STOP and finish your turn with a "
    "short reply telling the user that a host-terminal approval is waiting "
    "in the chat, and what you will do after they allow it. Do NOT call this "
    "tool again for the same operation, and do NOT claim the command ran.")


class HostTerminalFallback:
    """Approval-gated access to the host terminal, for Agent execution."""

    def __init__(self, approvals, *, registry=None, terminal=None,
                 runtime=None, events=None, config=None):
        self.approvals = approvals
        self.registry = registry
        self.terminal = terminal
        self.runtime = runtime
        self.events = events
        self.config = config

    # -- availability -------------------------------------------------------
    def terminal_wired(self) -> bool:
        return self.terminal is not None

    def policy_allows(self) -> bool:
        """True when the operator's permission policy actually grants the
        host terminal risk level. Reported, never assumed."""
        if self.registry is None:
            return False
        try:
            decision = self.registry.policy.decision(
                FALLBACK_RISK, False, tool_name=HOST_EXEC_TOOL)
        except Exception:
            return False
        return decision == "allow"

    def available(self) -> bool:
        """Host fallback is usable only when ALL of: the approvals manager
        can execute, the host terminal is wired, and policy allows it."""
        return bool(self.approvals is not None and self.approvals.available()
                    and self.terminal_wired() and self.policy_allows())

    def status(self) -> dict:
        return {"available": self.available(),
                "terminal_wired": self.terminal_wired(),
                "policy_allows": self.policy_allows(),
                "executor": bool(self.approvals is not None
                                 and self.approvals.available()),
                "environment": ENVIRONMENT_HOST}

    # -- request ------------------------------------------------------------
    def context(self, ctx) -> dict:
        """The ids this request belongs to, taken from the live tool
        context so an approval is scoped to THIS conversation/operation."""
        scope = getattr(ctx, "execution_scope", None) if ctx is not None else None
        session_id = ""
        request_id = getattr(ctx, "request_id", "") if ctx is not None else ""
        runtime_id = ""
        if ctx is not None:
            session_id = (getattr(ctx, "runtime_session_id", "")
                          or getattr(ctx, "terminal_session_id", "") or "")
            runtime = getattr(ctx, "runtime", None)
            if runtime is not None:
                runtime_id = str(getattr(runtime, "runtime_id", "") or "")
        return {"conversation_id": scope, "session_id": session_id,
                "request_id": request_id or "", "runtime_id": runtime_id}

    def request_command(self, *, command: str, cwd: str = "", reason: str = "",
                        ctx=None, operation: dict | None = None) -> dict:
        """Record an approval request. Executes nothing."""
        command = str(command or "").strip()
        if not command:
            raise ValidationError("command required")
        if self.approvals is None:
            return {"ok": False, "decision": "unavailable",
                    "reason": "host terminal fallback is not wired",
                    "executed": False}
        if not self.terminal_wired():
            return {"ok": False, "decision": "unavailable",
                    "reason": "host terminal is not available",
                    "executed": False}
        if not self.policy_allows():
            return {"ok": False, "decision": "denied_by_policy",
                    "reason": ("the host terminal is not granted by the "
                               "permission policy (GRANTED_PERMISSIONS)"),
                    "executed": False}
        ids = self.context(ctx)
        req = self.approvals.request(
            command=command, cwd=str(cwd or ""), reason=str(reason or ""),
            conversation_id=ids["conversation_id"],
            request_id=ids["request_id"], trace_id=ids["request_id"],
            session_id=ids["session_id"], runtime_id=ids["runtime_id"],
            operation=operation)
        return {"ok": True, "decision": "approval_required",
                "executed": False, "approval": req.to_dict(),
                "environment": ENVIRONMENT_HOST,
                "note": _RUNTIME_NOTE + " " + NO_APPROVAL_NOTE}

    # -- resolution ---------------------------------------------------------
    def decide(self, approval_id: str, allow: bool, *,
               by: str = "user") -> tuple:
        if self.approvals is None:
            return None, None
        return self.approvals.decide(approval_id, allow, by=by)

    def pending_for(self, conversation_id):
        if self.approvals is None:
            return None
        return self.approvals.pending_for(conversation_id)

    def context_text(self, conversation_id=None) -> str:
        if self.approvals is None:
            return ""
        return self.approvals.context_text(conversation_id)


def host_terminal_request(args: dict, ctx=None, fallback=None) -> dict:
    """Ask the user for permission to run ONE command on the HOST system.

    Use this ONLY when the isolated Astra Agent Runtime genuinely cannot
    perform the operation — the runtime is always the primary environment
    and needs no permission. This tool never executes the command: it
    records a scoped request and returns `approval_required`; the command
    runs only if the user selects *Allow* on the card that appears in the
    Assistant Chat.

    The approval is bound to this conversation and to this EXACT command and
    working directory. If the user denies it, the host command must not be
    run — continue in the Agent Runtime or explain the limitation."""
    fb = fallback
    if fb is None and ctx is not None:
        fb = getattr(ctx, "fallback", None)
    if fb is None:
        return {"ok": False, "decision": "unavailable", "executed": False,
                "reason": "host terminal fallback is not wired in this runtime"}
    command = args.get("command", "")
    if not str(command).strip():
        raise ValidationError("command required")
    return fb.request_command(command=str(command),
                              cwd=str(args.get("cwd") or ""),
                              reason=str(args.get("reason") or ""),
                              ctx=ctx)


HOST_TERMINAL_TOOLS = [
    ("host_terminal_request", host_terminal_request,
     "Request user approval to run ONE command on the HOST system (outside "
     "the isolated Agent Runtime). The primary environment is the Agent "
     "Runtime and needs no approval; use this only when the runtime genuinely "
     "cannot do the operation and the host command would help. Returns "
     "approval_required; the user decides in the Assistant Chat.",
     {"command": {"type": "string", "required": True},
      "cwd": {"type": "string",
              "description": "Working directory for the host command"},
      "reason": {"type": "string",
                 "description": "Why the Agent Runtime cannot perform this "
                                "operation"}}),
]


def register_fallback_tools(reg, fallback) -> int:
    """Register the approval-aware host fallback tool on the ONE registry.

    It is deliberately NOT `agent_forbidden`: the Agent is allowed to *ask*
    for host access. It simply cannot *execute* anything — only the
    ApprovalManager resolves an approval, and only through the trusted
    host executor."""
    for name, fn, description, schema in HOST_TERMINAL_TOOLS:
        reg.register(Tool(
            name=name,
            fn=_bind(fn, fallback),
            description=description,
            category="terminal",
            input=schema,
            risk=FALLBACK_RISK,
            requires_confirmation=False,
            idempotent=False,
            agent_forbidden=False,
            plugin="core"))
    return len(HOST_TERMINAL_TOOLS)


def _bind(fn, fallback):
    def bound(args, ctx=None):
        return fn(args, ctx, fallback)
    bound.__name__ = fn.__name__
    bound.__doc__ = fn.__doc__
    return bound
