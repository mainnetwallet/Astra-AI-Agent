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


class Agent:
    def __init__(self, orchestrator=None, pipeline=None):
        self.orchestrator = orchestrator   # legacy; no longer used for chat
        self.pipeline = pipeline           # astra.ai.chat_pipeline.ChatPipeline

    def handle(self, message: str, context: str = "", history=None,
               attachments: list | None = None) -> dict:
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
                                     attachments=attachments)
        except Exception as e:          # never let a bug become a blank 500
            return {"reply": f"Chat e ekta problem hoyeche — `{e}`",
                    "action": "none", "ok": False,
                    "data": {"error": str(e)}}

    def help_text(self) -> str:
        """No plugins are registered yet — see plugins/README.md."""
        return ("🤖 Astra AI Agent\nEkhono kono plugin add kora hoyni.\n\n"
                "Apni jei message-i pathan, Gateway seta bujhe best "
                "provider/model ke kaj dey, uttor verify kore tarpor apnake "
                "dey.")

    def resume(self, execution_id: str, allow: bool) -> dict:
        """The approve/reject round-trip belonged to the removed
        orchestrator; there is nothing to resume any more."""
        return {"reply": "Ei approval step ta ar available na — chat ekhon "
                         "Gateway pipeline diye cholche.",
                "action": "none", "data": {}, "ok": False}

    def dashboard(self) -> list[dict]:
        """No plugins are registered yet — see plugins/README.md."""
        return []

    def export_all(self) -> dict:
        """No plugins are registered yet — see plugins/README.md."""
        return {"_app": "Astra AI Agent", "_exports": {}}

    def import_all(self, payload: dict) -> dict:
        """No plugins are registered yet — see plugins/README.md."""
        return {"imported": False, "reason": "no plugins registered"}
