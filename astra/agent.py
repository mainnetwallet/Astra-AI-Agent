"""Astra Agent: routes chat text to the right plugin, then the orchestrator
(multi-step), then the LLM, then a gentle Banglish fallback.

The heart of the agent is deliberately tiny: it walks the loaded plugins in
order and the first plugin that says "handled=True" answers. Extending the
assistant never requires editing this file.
"""
from __future__ import annotations

from .core import Plugin

FALLBACK = (
    "Ei command ta ami bojhini. 'help' likhe dekhta paren — ba simple "
    "Banglish e bollun, jaate korte chai (jemon: 'add airdrop Notcoin "
    "deadline 30 oct').")
UNHANDLED_OFFLINE = (
    "Ei command ta ami bodhokorar chesta korlam, kintu kono tool/plugin "
    "match korlo na. 'help' likhe dekhte paren — ba chotto theke shuru "
    "korun.")


class Agent:
    def __init__(self, plugins: list[Plugin], llm=None, orchestrator=None):
        self.plugins = plugins
        self.llm = llm  # optional callable(message) -> str
        self.orchestrator = orchestrator  # optional multi-step executor

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
        # nobody claimed it -> orchestrator (multi-step) -> LLM -> fallback
        if self.orchestrator:
            try:
                report = self.orchestrator.submit(msg, sync=True)
                return self._reply_from_report(msg, report)
            except Exception:
                pass  # orchestrator failed silently -> llm / fallback
        if self.llm:
            try:
                return {"reply": self.llm(msg), "action": "none", "data": {},
                        "ok": True}
            except Exception:
                pass  # LLM unavailable/broken -> local fallback
        return {"reply": FALLBACK, "action": "none", "data": {}, "ok": False}

    def _reply_from_report(self, message: str, report: dict) -> dict:
        """Turn an orchestration report into {reply, action, data, ok} like a
        plugin would. Real tool steps -> ok True + compact summary; a pure
        'answer' step -> its text with ok False (unhandled by plugins)."""
        results = report.get("results") or {}
        real, lines = 0, []
        for sid, out in results.items():
            if out.get("tool") == "answer":
                continue
            real += 1
            desc = (out.get("description") or out.get("tool"))
            if out.get("ok"):
                head = _summarize(out.get("output") or {})
                lines.append(f"✅ {desc}: {head}")
            else:
                lines.append(f"⚠️ {desc}: {out.get('error') or 'failed'}")
        if report.get("pending"):
            body = "\n".join(lines) or "Approval lagbe."
            return {"reply": ("Ekta permission-gated step approve korte hobe. "
                              "Live tab e 'Approve' press korun.\n" + body),
                    "action": "live", "data": report, "ok": False}
        if real:
            status = "COMPLETED" if report.get("status") == "COMPLETED" else \
                ("FAILED" if report.get("status") == "FAILED" else report.get("status"))
            head = f"📋 Plan complete ({status}) — {report.get('steps', 0)} step:\n"
            return {"reply": head + "\n".join(lines), "action": "live",
                    "data": report, "ok": status == "COMPLETED"}
        # pure answer step -> keep the gentle unknown-fallback contract
        for sid, out in results.items():
            if out.get("tool") == "answer":
                text = (out.get("output") or {}).get("text", "") or UNHANDLED_OFFLINE
                return {"reply": text, "action": "none", "data": report,
                        "ok": False}
        return {"reply": UNHANDLED_OFFLINE, "action": "none", "data": report,
                "ok": False}

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


def _summarize(payload: dict) -> str:
    """One-line human summary of a tool output dict (count/length aware)."""
    if not payload:
        return "(kono output nai)"
    if isinstance(payload, dict) and "text" in payload:
        return str(payload["text"])[:160]
    if isinstance(payload, dict) and "count" in payload:
        return f"{payload['count']} item"
    if isinstance(payload, dict) and "content" in payload:
        return str(payload["content"])[:160]
    if isinstance(payload, dict) and "results" in payload:
        n = len(payload["results"])
        return f"{n} item"
    if isinstance(payload, dict) and "wallets" in payload:
        n = len(payload["wallets"])
        return f"{n} wallet balance check\n{_balances_lines(payload['wallets'])}"
    if isinstance(payload, dict) and "tasks" in payload:
        return f"{len(payload['tasks'])} task"
    text = str(payload)[:160]
    return text if text else "(empty)"


def _balances_lines(wallets: list) -> str:
    lines = []
    for w in wallets[:5]:
        if w.get("error"):
            lines.append(f"  • {w.get('label')}: {w.get('error')}")
        else:
            lines.append(f"  • {w.get('label')} [{w.get('symbol')}]: "
                         f"{w.get('balance')} on {w.get('chain', '?').upper()}")
    return "\n".join(lines) or ""