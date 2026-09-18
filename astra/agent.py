"""Astra Agent: routes chat text to the right plugin, then the orchestrator
(Planner -> Astra AI Gateway -> Existing Provider System), then a gentle
Banglish fallback.

The heart of the agent is deliberately tiny: it walks the loaded plugins in
order and the first plugin that says "handled=True" answers. Extending the
assistant never requires editing this file.

Every request a plugin does NOT claim is deterministic, non-AI command
matching (see astra/core plugin base) — the moment a request needs an AI
model, it goes through `orchestrator`, which is the only path to the
Astra AI Gateway / Existing Provider System. There is intentionally no
raw/direct LLM callable on this class; see the constructor docstring.
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
    def __init__(self, plugins: list[Plugin], orchestrator=None):
        self.plugins = plugins
        self.orchestrator = orchestrator  # optional multi-step executor
        # NOTE: there is deliberately no raw/direct LLM callable here. Any
        # normal request not claimed by a plugin MUST go through
        # `orchestrator` (Orchestrator -> Planner -> Astra AI Gateway ->
        # Existing Provider System). A prior version accepted an optional
        # `llm` callable and would call it directly on orchestrator
        # failure/absence, which is exactly the kind of legacy bypass path
        # that lets a normal AI request skip the Gateway. It has been
        # removed on purpose — do not re-add a direct model call here.

    def handle(self, message: str, context: str = "",
               attachments: list | None = None) -> dict:
        """Returns {reply, action, data, ok, artifacts} exactly as the old
        single-domain agent did, so callers/UI stay compatible.

        `context` is optional recent conversation the caller already has.
        `attachments` is an optional list of processed Attachment dicts
        from the multimodal upload layer. When present, the request is
        multimodal and capability-aware routing is required.
        """
        msg = " ".join(str(message).split()).strip()
        if not msg and not attachments:
            return {"reply": "Ki korte paren? 'help' likhun.", "action": "none",
                    "data": {}, "ok": False}
        if not msg and attachments:
            msg = f"[{len(attachments)} file(s) attached]"
        for p in self.plugins:
            if attachments:
                break
            try:
                r = p.process(msg)
            except Exception:
                continue
            if r:
                if len(r) == 5:
                    handled, reply, action, data, ok = r
                else:
                    handled, reply, action, data = r
                    ok = True
                if handled:
                    return {"reply": reply, "action": action, "data": data or {},
                            "ok": ok}
        if self.orchestrator:
            try:
                report = self.orchestrator.submit(
                    msg, sync=True, context=context,
                    attachments=attachments or None)
                return self._reply_from_report(msg, report)
            except Exception:
                pass
        return {"reply": FALLBACK, "action": "none", "data": {}, "ok": False}

    def _reply_from_report(self, message: str, report: dict) -> dict:
        """Turn an orchestration report into {reply, action, data, ok, artifacts}
        like a plugin would. Real tool steps -> ok True + compact summary; a
        pure 'answer' step -> its text with ok False (unhandled by plugins).

        When the response contains generated artifacts (images, code blocks,
        data files), they are extracted, stored, validated, and returned in
        the 'artifacts' list for the frontend to render."""
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
            step = report.get("pending_step") or {}
            tool = (step.get("description") or step.get("tool") or "")
            # action "confirm" tells the frontend to render inline
            # Approve/Reject buttons right under this bubble — resolved via
            # resume(), never by sending the user off to a separate tab.
            return {"reply": (f"⚠️ Approval lagbe: **{tool}**\n" + body),
                    "action": "confirm", "data": report, "ok": False}
        if real:
            status = "COMPLETED" if report.get("status") == "COMPLETED" else \
                ("FAILED" if report.get("status") == "FAILED" else report.get("status"))
            head = f"📋 Plan complete ({status}) — {report.get('steps', 0)} step:\n"
            reply = head + "\n".join(lines)
            artifacts = self._extract_response_artifacts(message, results)
            return {"reply": reply, "action": "live",
                    "data": report, "ok": status == "COMPLETED",
                    "artifacts": artifacts}
        for sid, out in results.items():
            if out.get("tool") == "answer":
                text = (out.get("output") or {}).get("text", "") or UNHANDLED_OFFLINE
                artifacts = self._extract_response_artifacts(message, results)
                return {"reply": text, "action": "none", "data": report,
                        "ok": False, "artifacts": artifacts}
        return {"reply": UNHANDLED_OFFLINE, "action": "none", "data": report,
                "ok": False, "artifacts": []}

    def _extract_response_artifacts(self, message: str,
                                     results: dict) -> list[dict]:
        """Extract artifacts from provider responses when applicable."""
        try:
            from astra.ai.artifact_extraction import (
                extract_artifacts, detect_output_type)
            from astra.core.artifacts import make_artifact_dir
            import tempfile
            artifacts = []
            for sid, out in results.items():
                output = out.get("output") or {}
                if isinstance(output, dict) and output.get("artifact"):
                    artifacts.append(output["artifact"])
            requested = detect_output_type(message)
            if requested:
                artifact_dir = make_artifact_dir(
                    tempfile.gettempdir() + "/astra")
                for sid, out in results.items():
                    text = ""
                    output = out.get("output") or {}
                    if isinstance(output, dict):
                        text = output.get("text", "")
                    elif isinstance(output, str):
                        text = output
                    if text:
                        found = extract_artifacts(text, artifact_dir,
                                                   requested)
                        artifacts.extend(found)
            return artifacts
        except Exception:
            return []

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