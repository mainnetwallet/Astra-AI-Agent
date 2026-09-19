"""Astra Agent: non-chat surfaces (help/dashboard/export/import/resume)
around the orchestrator.

`handle()` — the chat entry point that used to hand every message to
`self.orchestrator.submit(...)` and turn the report into a reply — has
been removed along with its `_reply_from_report()` /
`_extract_response_artifacts()` helpers, pending a new chat entry point.

NOTE: the plugin system (astra.core.Plugin/Registry) has been removed.
`plugins/` is an empty placeholder for future plugins (see
plugins/README.md). help_text(), dashboard(), export_all(), and
import_all() below no longer aggregate anything from plugins — they
return placeholders until new plugins exist to aggregate.

Still breaking on the handle() removal:
  - `astra/web.py`'s `POST /api/chat` handler calls `server.agent.handle(...)`
  - `resume()` below still calls the now-removed `self._reply_from_report(...)`
    on its success path
Both need a new implementation once the chat path is redesigned.
"""
from __future__ import annotations


class Agent:
    def __init__(self, orchestrator=None):
        self.orchestrator = orchestrator  # optional multi-step executor
        # NOTE: `handle()` (the chat entry point) has been removed — see
        # module docstring. `resume()` below still uses `self.orchestrator`
        # directly for the approve/reject round-trip.

    def help_text(self) -> str:
        """No plugins are registered yet — see plugins/README.md."""
        return ("🤖 Astra AI Agent\nEkhono kono plugin add kora hoyni.\n\n"
                "Free-form question thakle kewo bujhle na — LLM chat "
                "(ANTHROPIC_API_KEY set korle) uttor dibe.")

    def resume(self, execution_id: str, allow: bool) -> dict:
        """Approve or reject a WAITING_USER execution's pending tool call.

        Backs the inline Approve/Reject buttons a chat bubble renders when
        `_reply_from_report` returns action "confirm" — the whole
        confirm/deny round-trip happens in chat, no separate tab involved.
        Formatted exactly like a normal chat reply so the frontend can
        render it (and, if the resumed plan hits *another* pending step,
        chain into a fresh pair of Approve/Reject buttons) the same way.
        """
        if not self.orchestrator:
            return {"reply": "Orchestrator available na — resume kora gelo na.",
                    "action": "none", "data": {}, "ok": False}
        report = self.orchestrator.resume(execution_id, allow)
        if not allow:
            return {"reply": "❌ Reject kora hoyeche — ei step ta বাতিল হলো.",
                    "action": "none", "data": report, "ok": False}
        if report.get("status") == "not waiting":
            return {"reply": "Ei kaj ta ar approval-er jonno wait korche na "
                              "(hoyto already handle hoye geche).",
                    "action": "none", "data": report, "ok": False}
        return self._reply_from_report(report.get("goal", ""), report)

    def dashboard(self) -> list[dict]:
        """No plugins are registered yet — see plugins/README.md."""
        return []

    def export_all(self) -> dict:
        """No plugins are registered yet — see plugins/README.md."""
        return {"_app": "Astra AI Agent", "_exports": {}}

    def import_all(self, payload: dict) -> dict:
        """No plugins are registered yet — see plugins/README.md."""
        return {"imported": False, "reason": "no plugins registered"}
