"""Assistant-Chat approval for the HOST terminal fallback (spec §3-§9).

Execution priority is fixed and absolute:

  1. **Astra Agent Runtime** — primary, always first, NO user permission.
  2. **HOST terminal** — fallback only, and only after the user explicitly
     allows the exact operation in the Assistant Chat.

This module is the ONE place that authorises a host command. Nothing else
in Astra can grant it: the raw `terminal_exec` family stays `agent_forbidden`
(astra/tools/registry.py refuses it for any Agent/Provider/workflow call),
and the only agent-reachable host surface is `host_terminal_request`
(astra/terminal/fallback.py), which executes nothing — it records a request
and returns an `approval_required` result.

Why an approval object instead of a boolean:

  * A permanent "allow host terminal" switch would authorise every future
    command the model invents. An approval is *scoped*: it binds ONE
    conversation, ONE request/operation, ONE exact command and ONE cwd
    (§5). `decide()` executes the command stored on the request — never a
    command supplied at decision time — so a different command or cwd can
    never ride an existing approval.
  * Approval is *exactly once*: the pending -> approved transition is the
    execution claim, taken under the lock, so a double click, a page
    refresh, an SSE reconnect or a retried request resolves to the same
    single execution (§8).
  * Approval *expires*: an unanswered card is not a standing permission.

State lives in-process and is bounded, exactly like the rest of Astra's
in-flight bookkeeping (the chat transcript itself is persisted by
`astra/chat_log.py`; the approval's final status is written back into the
card the user is looking at through `meta_writer`).
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime

# §7 approval statuses. Only APPROVED may execute — everything else is a
# refusal to run the host command.
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = frozenset({STATUS_DENIED, STATUS_EXPIRED,
                               STATUS_CANCELLED, STATUS_COMPLETED,
                               STATUS_FAILED})

DEFAULT_TTL_S = 900.0          # an unanswered card stops being valid
MAX_RECORDS = 200              # bounded, newest kept
MAX_OUTPUT_CHARS = 4000        # what the resumed turn / card may carry
ENVIRONMENT_HOST = "host"
ENVIRONMENT_RUNTIME = "agent_runtime"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clip(text, limit: int = MAX_OUTPUT_CHARS) -> str:
    s = "" if text is None else str(text)
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n… [{len(s) - limit} more characters]"


class ApprovalRequest:
    """One scoped host-execution approval (§4)."""

    def __init__(self, *, approval_id: str, command: str, cwd: str = "",
                 reason: str = "", conversation_id=None, request_id: str = "",
                 trace_id: str = "", op_id: str = "", session_id: str = "",
                 ttl_s: float = DEFAULT_TTL_S, created_at: float | None = None,
                 message_id: int | None = None, runtime_id: str = ""):
        self.approval_id = approval_id
        self.conversation_id = (None if conversation_id in (None, "", 0)
                                else conversation_id)
        self.request_id = str(request_id or "")
        self.trace_id = str(trace_id or request_id or "")
        self.op_id = str(op_id or "")
        self.command = str(command or "").strip()
        self.cwd = str(cwd or "").strip()
        self.reason = str(reason or "").strip()
        self.session_id = str(session_id or "")
        self.runtime_id = str(runtime_id or "")
        self.created_at = float(created_at if created_at is not None
                                else time.time())
        self.expires_at = self.created_at + max(1.0, float(ttl_s))
        self.status = STATUS_PENDING
        self.decided_by = ""
        self.resolved_at = 0.0
        self.executed = False
        self.execution_id = ""
        self.result: dict = {}
        self.error = ""
        self.message_id = message_id
        self.environment = ENVIRONMENT_HOST

    # -- lifecycle ----------------------------------------------------------
    @property
    def expired(self) -> bool:
        return (self.status == STATUS_PENDING
                and time.time() > self.expires_at)

    @property
    def pending(self) -> bool:
        return self.status == STATUS_PENDING and not self.expired

    @property
    def resolved(self) -> bool:
        return self.status != STATUS_PENDING

    # -- serialization ------------------------------------------------------
    def to_dict(self) -> dict:
        out = {
            "approval_id": self.approval_id,
            "conversation_id": self.conversation_id,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "op_id": self.op_id,
            "command": self.command,
            "cwd": self.cwd,
            "reason": self.reason,
            "session_id": self.session_id,
            "runtime_id": self.runtime_id,
            "created_at": self.created_at,
            "created_at_text": datetime.fromtimestamp(
                self.created_at).strftime("%Y-%m-%d %H:%M:%S"),
            "expires_at": self.expires_at,
            "expires_in_s": max(0, int(self.expires_at - time.time())),
            "status": STATUS_EXPIRED if self.expired else self.status,
            "decided_by": self.decided_by,
            "resolved_at": self.resolved_at,
            "executed": bool(self.executed),
            "execution_id": self.execution_id,
            "environment": self.environment,
            "error": self.error,
            "message_id": self.message_id,
        }
        if self.result:
            out["result"] = {
                "status": self.result.get("status", ""),
                "exit_code": self.result.get("exit_code"),
                "stdout": _clip(self.result.get("stdout"), 1200),
                "stderr": _clip(self.result.get("stderr"), 1200),
                "duration_ms": self.result.get("duration_ms"),
                "truncated": bool(self.result.get("truncated")),
                "blob_id": self.result.get("blob_id", ""),
            }
        return out


class ApprovalManager:
    """Creates, stores, resolves and executes scoped host-execution
    approvals. The ONLY execution path for an approved host command."""

    def __init__(self, *, events=None, config=None, ttl_s: float | None = None,
                 executor=None, max_records: int = MAX_RECORDS):
        self.events = events
        self.config = config
        self._executor = executor          # fn(request) -> terminal result
        self._resumer = None               # fn(request, op, result) -> reply
        self._meta_writer = None           # fn(message_id, meta) -> None
        self._lock = threading.RLock()
        self._records: dict[str, ApprovalRequest] = {}
        self._order: list[str] = []
        self._operations: dict[str, dict] = {}
        self._max_records = max(8, int(max_records or MAX_RECORDS))
        try:
            cfg_ttl = None
            if config is not None:
                cfg_ttl = config.get("HOST_APPROVAL_TTL_S")
            self.ttl_s = float(ttl_s if ttl_s is not None
                               else (cfg_ttl or DEFAULT_TTL_S))
        except Exception:
            self.ttl_s = float(ttl_s or DEFAULT_TTL_S)

    # -- wiring -------------------------------------------------------------
    def set_executor(self, fn) -> None:
        """The trusted host executor (`terminal_exec` through the existing
        ToolRegistry, called with a NON-agent context)."""
        self._executor = fn

    def set_resumer(self, fn) -> None:
        """Continuation callback: `fn(request, operation, result) -> reply`."""
        self._resumer = fn

    def set_meta_writer(self, fn) -> None:
        """Persist the final approval state back into the chat card."""
        self._meta_writer = fn

    def available(self) -> bool:
        return self._executor is not None

    # -- events -------------------------------------------------------------
    def _emit(self, kind: str, **data) -> None:
        if not self.events:
            return
        try:
            self.events.emit(kind, agent="host_terminal", **data)
        except Exception:
            pass

    # -- creation -----------------------------------------------------------
    def request(self, *, command: str, cwd: str = "", reason: str = "",
                conversation_id=None, request_id: str = "", trace_id: str = "",
                op_id: str = "", session_id: str = "", runtime_id: str = "",
                operation: dict | None = None) -> ApprovalRequest:
        command = str(command or "").strip()
        if not command:
            raise ValueError("command required")
        req = ApprovalRequest(
            approval_id="ap-" + uuid.uuid4().hex[:12],
            command=command, cwd=cwd, reason=reason,
            conversation_id=conversation_id, request_id=request_id,
            trace_id=trace_id, op_id=op_id, session_id=session_id,
            runtime_id=runtime_id, ttl_s=self.ttl_s)
        with self._lock:
            self._prune_locked()
            self._records[req.approval_id] = req
            self._order.append(req.approval_id)
            if operation:
                self._operations[req.approval_id] = dict(operation)
        self._emit("host_terminal.approval_requested",
                   approval_id=req.approval_id,
                   conversation_id=req.conversation_id,
                   command=req.command, cwd=req.cwd, reason=req.reason,
                   environment=ENVIRONMENT_HOST, expires_at=req.expires_at,
                   request_id=req.request_id, trace_id=req.trace_id,
                   op_id=req.op_id)
        return req

    def attach_operation(self, approval_id: str, operation: dict) -> None:
        """Remember what to continue when the user decides (the original
        request text, its history, scope and session)."""
        with self._lock:
            if approval_id in self._records:
                self._operations.setdefault(approval_id, {}).update(
                    dict(operation or {}))

    def attach_message(self, approval_id: str, message_id) -> None:
        """Bind this approval to the chat message that renders its card, so
        the final decision can be written back into the transcript."""
        with self._lock:
            req = self._records.get(approval_id)
            if req is not None:
                try:
                    req.message_id = int(message_id)
                except (TypeError, ValueError):
                    req.message_id = None

    # -- lookup -------------------------------------------------------------
    def get(self, approval_id: str) -> ApprovalRequest | None:
        with self._lock:
            req = self._records.get(str(approval_id or ""))
            if req is not None and req.expired:
                self._expire_locked(req)
            return req

    def pending(self, conversation_id=None, request_id=None
                ) -> list[ApprovalRequest]:
        with self._lock:
            self._expire_all_locked()
            out = [r for r in self._records.values() if r.pending]
        if conversation_id not in (None, "", 0):
            cid = str(conversation_id)
            out = [r for r in out if str(r.conversation_id) == cid]
        if request_id not in (None, ""):
            rid = str(request_id)
            out = [r for r in out if r.request_id == rid]
        return out

    def pending_for_request(self, request_id) -> ApprovalRequest | None:
        items = self.pending(request_id=request_id)
        return items[-1] if items else None

    def pending_for(self, conversation_id) -> ApprovalRequest | None:
        items = self.pending(conversation_id)
        return items[-1] if items else None

    def operation(self, approval_id: str) -> dict:
        with self._lock:
            return dict(self._operations.get(str(approval_id or ""), {}))

    def all(self, limit: int = 50) -> list[dict]:
        with self._lock:
            self._expire_all_locked()
            ids = self._order[-max(1, int(limit or 50)):]
            return [self._records[i].to_dict() for i in ids
                    if i in self._records]

    # -- expiry -------------------------------------------------------------
    def _expire_locked(self, req: ApprovalRequest) -> None:
        req.status = STATUS_EXPIRED
        req.resolved_at = time.time()
        self._emit("host_terminal.approval_expired",
                   approval_id=req.approval_id,
                   conversation_id=req.conversation_id,
                   command=req.command, cwd=req.cwd,
                   environment=ENVIRONMENT_HOST, reason="approval expired")
        self._write_back(req)

    def _expire_all_locked(self) -> None:
        for req in list(self._records.values()):
            if req.expired:
                self._expire_locked(req)

    def _prune_locked(self) -> None:
        while len(self._order) > self._max_records:
            old = self._order.pop(0)
            req = self._records.get(old)
            if req is not None and req.pending:
                # never drop an unresolved request silently; requeue it
                self._order.append(old)
                break
            self._records.pop(old, None)
            self._operations.pop(old, None)

    # -- resolution (the ONE authorisation point) ---------------------------
    def decide(self, approval_id: str, allow: bool, *,
               by: str = "user") -> tuple[ApprovalRequest | None, dict | None]:
        """Resolve ONE approval.

        Returns `(request, resumed_reply_or_None)`. Deny never executes.
        Allow executes the request's stored command exactly once and then
        asks the registered resumer to continue the same logical operation.
        A second call for an already-resolved approval is a no-op that
        returns the SAME request and never executes again (§8).
        """
        with self._lock:
            req = self._records.get(str(approval_id or ""))
            if req is None:
                return None, None
            if req.status != STATUS_PENDING:
                return req, None            # already resolved — never re-run
            if req.expired:
                self._expire_locked(req)
                return req, None
            req.decided_by = str(by or "user")
            req.resolved_at = time.time()
            if not allow:
                req.status = STATUS_DENIED
                self._emit("host_terminal.approval_denied",
                           approval_id=req.approval_id,
                           conversation_id=req.conversation_id,
                           command=req.command, cwd=req.cwd,
                           environment=ENVIRONMENT_HOST,
                           decided_by=req.decided_by)
                self._write_back(req)
                # A denial must still produce a REAL reply in the same chat
                # (§1: inform the Provider/Agent, try a runtime alternative,
                # otherwise explain the limitation) — never silence.
                return req, self._resume(req, allowed=False)
            # The claim: from here the request can never be executed twice,
            # because every later decide() sees a non-pending status.
            req.status = STATUS_APPROVED
            self._emit("host_terminal.approval_allowed",
                       approval_id=req.approval_id,
                       conversation_id=req.conversation_id,
                       command=req.command, cwd=req.cwd,
                       environment=ENVIRONMENT_HOST,
                       decided_by=req.decided_by)
        self._execute(req)
        resumed = self._resume(req, allowed=True)
        return req, resumed

    def cancel(self, approval_id: str, *, reason: str = "cancelled"):
        with self._lock:
            req = self._records.get(str(approval_id or ""))
            if req is None or req.status != STATUS_PENDING:
                return req
            req.status = STATUS_CANCELLED
            req.resolved_at = time.time()
            req.error = str(reason)
            self._write_back(req)
            return req

    # -- execution ----------------------------------------------------------
    def _execute(self, req: ApprovalRequest) -> None:
        """Run the APPROVED host command through the trusted executor.

        The command/cwd come from the request, never from the caller — an
        approval authorises exactly the operation the user was shown.
        """
        if self._executor is None:
            req.status = STATUS_FAILED
            req.error = ("host terminal fallback is not available to Astra; "
                         "nothing was executed")
            self._emit("host_terminal.failed", approval_id=req.approval_id,
                       command=req.command, cwd=req.cwd, error=req.error,
                       environment=ENVIRONMENT_HOST)
            self._write_back(req)
            return
        started = time.time()
        self._emit("host_terminal.started", approval_id=req.approval_id,
                   conversation_id=req.conversation_id, command=req.command,
                   cwd=req.cwd, environment=ENVIRONMENT_HOST,
                   decided_by=req.decided_by)
        try:
            result = self._executor(req) or {}
        except Exception as exc:            # never let an executor bug escape
            req.status = STATUS_FAILED
            req.error = f"{type(exc).__name__}: {exc}"
            self._emit("host_terminal.failed", approval_id=req.approval_id,
                       command=req.command, cwd=req.cwd,
                       error=req.error, environment=ENVIRONMENT_HOST)
            self._write_back(req)
            return
        duration_ms = int((time.time() - started) * 1000)
        result = dict(result)
        result.setdefault("duration_ms", duration_ms)
        req.result = result
        req.executed = True
        req.execution_id = str(result.get("process_id") or
                               result.get("execution_id") or "")
        failed = (str(result.get("status") or "").lower() in ("failed",
                                                              "timeout")
                  or (result.get("exit_code") not in (None, 0)
                      and str(result.get("status") or "") != "running"))
        req.status = STATUS_FAILED if failed else STATUS_COMPLETED
        if failed:
            req.error = str(result.get("stderr") or "").strip()[:500]
        self._emit("host_terminal.completed" if not failed
                   else "host_terminal.failed",
                   approval_id=req.approval_id,
                   conversation_id=req.conversation_id,
                   command=req.command, cwd=req.cwd,
                   exit_code=result.get("exit_code"),
                   status=req.status, duration_ms=duration_ms,
                   environment=ENVIRONMENT_HOST,
                   stdout_blob_id=result.get("blob_id", ""))
        self._write_back(req)

    def _resume(self, req: ApprovalRequest, *, allowed: bool) -> dict | None:
        """Continue the SAME logical Agent operation after the user decided.

        `allowed=True` runs AFTER the approved host command has executed and
        hands its real result to the continuation; `allowed=False` tells the
        Agent to continue without host execution (a runtime alternative, or
        an honest explanation)."""
        if self._resumer is None:
            return None
        try:
            return self._resumer(req, self.operation(req.approval_id),
                                 (req.result if allowed else None), allowed)
        except Exception as exc:
            done = ("Host command executed" if allowed
                    else "Host command was not executed")
            return {"reply": (f"{done}, but continuing the task failed — "
                              f"`{type(exc).__name__}: {exc}`"),
                    "action": "none", "ok": False,
                    "data": {"approval": req.to_dict()}}

    # -- chat transcript write-back ----------------------------------------
    def _write_back(self, req: ApprovalRequest) -> None:
        if self._meta_writer is None or req.message_id is None:
            return
        try:
            self._meta_writer(req.message_id, {"approval": req.to_dict()})
        except Exception:
            pass

    # -- live context (§13) -------------------------------------------------
    def context_text(self, conversation_id=None, *, limit: int = 3) -> str:
        """Bounded, deterministic state of host-fallback approvals for the
        Gateway/Provider. Empty when there is nothing to say."""
        try:
            rows = self.pending(conversation_id)
        except Exception:
            return ""
        if not rows:
            return ""
        lines = ["Host terminal fallback — pending user approval:"]
        for req in rows[-max(1, int(limit or 3)):]:
            lines.append(
                f"- approval_id={req.approval_id} status={req.status} "
                f"command={req.command[:160]!r} cwd={req.cwd or '(default)'} "
                f"reason={req.reason[:160]!r} — the host command has NOT "
                "run; it runs only if the user selects Allow in the "
                "Assistant Chat.")
        return "\n".join(lines)

    def status_text(self, conversation_id=None) -> str:
        rows = self.pending(conversation_id)
        if not rows:
            return "none"
        return ", ".join(f"{r.approval_id}({r.status})" for r in rows)
