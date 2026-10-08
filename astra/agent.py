"""Astra Agent: the chat entry point plus the non-chat surfaces
(help/dashboard/export/import/resume).

`handle()` sends every chat message through the ChatPipeline
(astra/ai/chat_pipeline.py):

    User -> Gateway (understand + assign) -> Provider
         -> Gateway (verify; fix/redo loop) -> User

The provider's raw output never reaches the user before the Gateway has
verified it. See that module for the full flow and its fail-open rules.

Invariant (enforced by tests/test_zero_bypass_hardening.py):
there is no raw/direct LLM callable in this module. Every AI call goes
through the ChatPipeline, i.e. the Astra AI Gateway and the AstraRouter,
never straight to a provider SDK or HTTP endpoint.

NOTE: the plugin system (astra.core.Plugin/Registry) has been removed.
`plugins/` is an empty placeholder for future plugins (see
plugins/README.md). help_text(), dashboard(), export_all(), and
import_all() below no longer aggregate anything from plugins — they
return placeholders until new plugins exist to aggregate.

The old orchestrator/planner approve-reject round-trip no longer exists, so
`resume()` reports that honestly instead of pretending to continue a run.
"""
from __future__ import annotations

from astra.ai.response_boundary import sanitize_final_response


class Agent:
    def __init__(self, orchestrator=None, pipeline=None, approvals=None):
        self.orchestrator = orchestrator   # legacy; no longer used for chat
        self.pipeline = pipeline           # astra.ai.chat_pipeline.ChatPipeline
        # Approval-gated HOST terminal fallback (astra/terminal/approval.py).
        # Present so the Assistant Chat's Allow/Deny can resolve a pending
        # request; a host command runs only through it.
        self.approvals = approvals

    def handle(self, message: str, context: str = "", history=None,
               attachments: list | None = None,
               conversation_id=None, session_id=None) -> dict:
        """Chat entry point. Always returns the reply shape the frontend
        renders: {reply, action, ok, data[, artifacts]}.

        `history`: the canonical conversation history for this turn (see
        `astra.ai.conversation_context.ConversationContextBuilder`), built
        by the caller from the SAME conversation the current message
        belongs to. Forwarded as-is to the pipeline, which hands it to both
        the Gateway and the Provider call. `context` (a plain string) is
        kept for backward compatibility."""
        if self.pipeline is None:
            return {"reply": "Chat pipeline configure kora nei — Gateway/"
                             "Provider setup check korun.",
                    "action": "none", "ok": False, "data": {}}
        try:
            return self.pipeline.run(message, context=context or "",
                                     history=history,
                                     attachments=attachments,
                                     conversation_id=conversation_id,
                                     session_id=session_id)
        except Exception as e:          # never let a bug become a blank 500
            # `e` can legitimately contain a credential (e.g. a failed
            # `git clone https://<token>@github.com/...` surfaces the URL,
            # token included, in its exception message) or, in principle,
            # stray internal tool-protocol text. Route it through the same
            # response boundary as every other reply — see
            # astra/ai/response_boundary.py.
            safe = sanitize_final_response(f"Chat e ekta problem hoyeche — `{e}`")
            return {"reply": safe, "action": "none", "ok": False,
                    "data": {"error": sanitize_final_response(str(e))}}

    def help_text(self) -> str:
        """No plugins are registered yet — see plugins/README.md."""
        return ("🤖 Astra AI Agent\nEkhono kono plugin add kora hoyni.\n\n"
                "Apni jei message-i pathan, Gateway seta bujhe best "
                "provider/model ke kaj dey, uttor verify kore tarpor apnake "
                "dey.")

    def _approvals(self):
        mgr = self.approvals
        if mgr is None:
            mgr = getattr(self.pipeline, "approvals", None)
        return mgr

    def resume(self, execution_id: str, allow: bool) -> dict:
        """Resolve a HOST-terminal approval from the Assistant Chat.

        `execution_id` is the approval id shown on the card
        (`POST /api/chat/resume` and `POST /api/terminal/approval/<id>` both
        land here). Allow executes the approved command exactly once and
        resumes the same logical operation; Deny never executes it. An
        unknown/already-resolved approval is reported honestly — it is never
        re-executed."""
        mgr = self._approvals()
        if mgr is None:
            return {"reply": "Host terminal approval is not wired in this "
                             "process, so nothing was run.",
                    "action": "none", "data": {}, "ok": False}
        req, resumed = mgr.decide(execution_id, bool(allow), by="user")
        if req is None:
            return {"reply": "Ei approval request ta ar paoa jacche na — hoy "
                             "expire hoyeche, noy server restart hoyeche. "
                             "Kichu execute kora hoyni.",
                    "action": "none", "data": {}, "ok": False}
        if resumed:
            data = dict(resumed.get("data") or {})
            data["approval"] = req.to_dict()
            resumed = dict(resumed)
            resumed["data"] = data
            return resumed
        return self._approval_only_reply(req)

    @staticmethod
    def _approval_only_reply(req) -> dict:
        """The card's final state when there is nothing further to continue
        (e.g. Deny, expiry, or a resumer that produced no reply)."""
        from astra.terminal.approval import (STATUS_COMPLETED, STATUS_DENIED,
                                             STATUS_EXPIRED, STATUS_FAILED)
        data = {"approval": req.to_dict()}
        if req.status == STATUS_DENIED:
            return {"reply": ("✕ Denied — the host command was not executed.\n\n"
                              f"Command: `{req.command}`"),
                    "action": "none", "ok": True, "data": data}
        if req.status == STATUS_EXPIRED:
            return {"reply": ("⌛ This approval expired, so the host command was "
                              "not executed.\n\n"
                              f"Command: `{req.command}`"),
                    "action": "none", "ok": True, "data": data}
        if req.status == STATUS_COMPLETED:
            code = (req.result or {}).get("exit_code")
            return {"reply": ("✓ Approved by you — the host command executed "
                              f"(exit code {code}).\n\nCommand: "
                              f"`{req.command}`"),
                    "action": "none", "ok": True, "data": data}
        if req.status == STATUS_FAILED:
            return {"reply": ("Approved, but the host command failed"
                              f" — `{req.error or 'unknown error'}`.\n\n"
                              f"Command: `{req.command}`"),
                    "action": "none", "ok": False, "data": data}
        return {"reply": f"Approval status: {req.status}.",
                "action": "none", "ok": False, "data": data}

    def dashboard(self) -> list[dict]:
        """No plugins are registered yet — see plugins/README.md."""
        return []

    def export_all(self) -> dict:
        """No plugins are registered yet — see plugins/README.md."""
        return {"_app": "Astra AI Agent", "_exports": {}}

    def import_all(self, payload: dict) -> dict:
        """No plugins are registered yet — see plugins/README.md."""
        return {"imported": False, "reason": "no plugins registered"}
