"""Astra Agent: non-chat surfaces (help/dashboard/export/import/resume)
around the plugin list and the orchestrator.

`handle()` — the chat entry point that used to hand every message to
`self.orchestrator.submit(...)` and turn the report into a reply — has
been removed along with its `_reply_from_report()` /
`_extract_response_artifacts()` helpers, pending a new chat entry point.

Still breaking on this removal:
  - `astra/web.py`'s `POST /api/chat` handler calls `server.agent.handle(...)`
  - `resume()` below still calls the now-removed `self._reply_from_report(...)`
    on its success path
Both need a new implementation once the chat path is redesigned.
"""
from __future__ import annotations

from .core import Plugin

class Agent:
    def __init__(self, plugins: list[Plugin], orchestrator=None):
        # `plugins` is kept for the non-chat surfaces below that still use
        # it directly — help_text(), dashboard(), export_all()/import_all()
        # — and because HTTP routes / the tool registry are wired from the
        # same plugin list elsewhere in bootstrap.py.
        self.plugins = plugins
        self.orchestrator = orchestrator  # optional multi-step executor
        # NOTE: `handle()` (the chat entry point) has been removed — see
        # module docstring. `resume()` below still uses `self.orchestrator`
        # directly for the approve/reject round-trip.

    def help_text(self) -> str:
        """Concatenated help from every plugin that offers one."""
        blocks = []
        for p in self.plugins:
            h = getattr(p, "help_text", None)
            if callable(h) and h():
                blocks.append((p.icon, p.title, h()))
        lines = ["🤖 Astra AI Agent", "Plugin gulo (" + str(len(blocks)) + "):"]
        for icon, title, text in blocks:
            lines.append(f"\n{icon} **{title}**\n" + text)
        lines.append("\nFree-form question thakle kewo bujhle na — LLM chat "
                     "(ANTHROPIC_API_KEY set korle) uttor dibe.")
        return "\n".join(lines)

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
        """Aggregate all plugin summary() blocks into one shared dashboard."""
        blocks = []
        for p in self.plugins:
            s = p.summary()
            if s:
                blocks.append({"slug": p.slug, "title": p.title, "icon": p.icon,
                               "data": s})
        return blocks

    def export_all(self) -> dict:
        out = {"_app": "Astra AI Agent", "_exports": {}}
        for p in self.plugins:
            d = p.export()
            if d:
                out["_exports"][p.slug] = d
        return out

    def import_all(self, payload: dict) -> dict:
        report = {}
        exports = payload.get("_exports", payload)
        for p in self.plugins:
            d = exports.get(p.slug)
            if d:
                r = p.import_data(d) or {}
                report.update({f"{p.slug}_{k}": v for k, v in r.items()})
        return report or {"imported": True}


