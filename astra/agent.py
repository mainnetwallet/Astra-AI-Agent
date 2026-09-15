"""Astra Agent: routes chat text to the right plugin.

The heart of the agent is deliberately tiny: it walks the loaded plugins in
order and the first plugin that says "handled=True" answers. A command that
no plugin understands falls through to the optional LLM (when configured) or
to a gentle Banglish fallback. This makes the assistant extensible without
ever editing this file.
"""
from __future__ import annotations

from .core import Plugin


class Agent:
    def __init__(self, plugins: list[Plugin], llm=None):
        self.plugins = plugins
        self.llm = llm  # optional callable(message) -> str

    def handle(self, message: str) -> dict:
        """Returns {reply, action, data, ok} exactly as the old single-domain
        agent did, so callers/UI stay compatible."""
        msg = " ".join(str(message).split()).strip()
        if not msg:
            return {"reply": "Ki korte paren? 'help' likhun.", "action": "none",
                    "data": {}, "ok": False}
        for p in self.plugins:
            try:
                r = p.process(msg)
            except Exception:        # one plugin must never kill the agent
                continue
            if r:
                # plugins return (handled, reply, action, data) or
                # (handled, reply, action, data, ok)
                if len(r) == 5:
                    handled, reply, action, data, ok = r
                else:
                    handled, reply, action, data = r
                    ok = True
                if handled:
                    return {"reply": reply, "action": action, "data": data or {},
                            "ok": ok}
        # nobody claimed it -> LLM or fallback
        if self.llm:
            try:
                return {"reply": self.llm(msg), "action": "none", "data": {},
                        "ok": True}
            except Exception:
                pass  # LLM unavailable/broken -> local fallback
        return {
            "reply": "Ei command ta ami bojhini. 'help' likhe dekhta paren — ba "
                     "simple Banglish e bollun, jaate korte chai (jemon: 'add "
                     "airdrop Notcoin deadline 30 oct').",
            "action": "none", "data": {}, "ok": False}

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