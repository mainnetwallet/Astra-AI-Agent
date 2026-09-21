"""Astra chat pipeline — the single path every chat message takes.

    User message
      -> Gateway call #1  UNDERSTAND + ASSIGN
           - message incomplete?  -> Gateway completes it
           - message complete?    -> passed through unchanged (no "improving")
           - Gateway sees every usable provider/model and picks the best one
             for this job, and writes down what a 100%-complete answer needs
      -> Provider executes the work (its output is NOT shown to the user yet)
      -> Gateway call #2  VERIFY
           - the Gateway is handed everything call #1 decided (the request it
             assigned, the completion criteria, which provider/model got it)
             plus the provider's output, and judges: complete or not?
           - complete     -> the output goes to the user
           - not complete -> the Gateway tells the provider exactly what is
                             missing/wrong and whether to FIX it or REDO it
                             from scratch; the provider retries and the
                             Gateway verifies again
      -> User

The verify/fix loop is bounded (`astra.core.correction.MAX_CORRECTION_ATTEMPTS`)
so a stubborn provider can never spin forever. If the loop ends without the
Gateway confirming completion, the user still gets the best answer, together
with an honest note saying what is still missing — this pipeline never claims
100% unless the Gateway's last verification said so.

Building blocks reused (not re-implemented):
  - `AstraAIGateway.chat`            the Gateway's own AI connections
  - `AstraRouter.route_request`      provider execution + provider fallback
  - `AstraAIGateway.supervise_task`  bounded verify -> correct -> re-verify
                                     loop (astra.ai.gateway_task_completion)

Fail-open rules:
  - Gateway absent/unusable  -> the message goes straight to the router and the
                                answer is returned unverified (`data.gateway`
                                says so).
  - Call #1 fails            -> the raw message is used, routing is automatic.
  - Call #2 unusable         -> the provider's answer is returned with a note
                                that it could not be verified.
"""
from __future__ import annotations

import os
import tempfile

from astra.ai.artifact_extraction import detect_output_type, extract_artifacts
from astra.ai.gateway_contract import (ProviderExecutionPort,
                                       ProviderExecutionResult,
                                       ProviderExecutionTarget)
from astra.ai.gateway_task_completion import (COMPLETE, FAILED, INCOMPLETE,
                                              build_task_completion_contract)
from astra.ai.json_extract import loads_lenient
from astra.ai.multimodal_messages import build_multimodal_content
from astra.ai.router import RoutingRequest, classify
from astra.core.exceptions import ProviderError
from astra.core.events import new_op_id

# Task types that are safe to hand the router for a plain chat turn.
# Everything else `classify()` can return (image/audio/video generation,
# structured_output -> forced JSON mode, browser/web3 -> tool territory)
# is served as ordinary chat here.
_CHAT_TASK_TYPES = frozenset({"simple_chat", "coding", "translation",
                              "summarization", "research", "planning"})

MAX_TARGETS_IN_PROMPT = 60
MAX_OUTPUT_CHARS_IN_VERIFY = 12000
# The Gateway picks ITS OWN model by keyword-classifying user-role text. These
# control prompts embed the provider catalogue ("...vision...") and provider
# output, which would trip the hard vision/json filters, so the pipeline
# states the category explicitly instead of letting it be guessed.
UNDERSTAND_MAX_TOKENS = 700
VERIFY_MAX_TOKENS = 600

PROVIDER_SYSTEM_PROMPT = (
    "You are Astra, a helpful AI assistant. Do the user's request fully and "
    "directly. Reply in the same language the user wrote in (Bengali, "
    "Banglish or English). Give the actual answer/output, not a description "
    "of what you would do. Never mention internal routing, gateways or "
    "verification."
)

UNDERSTAND_SYSTEM_PROMPT = (
    "You are the Astra AI Gateway. A user message arrives (it may be short, "
    "incomplete, or written in Bengali/Banglish/English). You do NOT answer "
    "it and you do NOT do the task — a Provider AI does that after you. You "
    "do three things and reply with ONE JSON object and nothing else.\n\n"
    "1) UNDERSTAND. Decide whether the message is complete.\n"
    "   - If it is incomplete (missing subject, vague reference like "
    "\"eita\"/\"that one\", cut-off sentence), rewrite it as a complete "
    "request. Use only what is in the message or the prior-conversation "
    "block; NEVER invent facts, names, numbers or intentions. If something "
    "essential truly cannot be inferred, keep the request as-is and add "
    "\"state clearly what information is missing instead of guessing\" to "
    "the criteria.\n"
    "   - If it is already complete, copy it EXACTLY into final_request. "
    "Do not improve, translate, restructure or expand a complete message.\n"
    "   - final_request is written in the user's voice, never as a reply.\n\n"
    "2) ASSIGN. From the provider/model list you are given, pick the single "
    "best provider+model for this job (coding -> a coding-capable model, "
    "hard reasoning -> a high-quality model, simple chat -> a fast one, "
    "images -> a vision model). Copy provider and model EXACTLY from the "
    "list. If nothing in the list is a clear fit, use \"\" for both.\n\n"
    "3) DEFINE DONE. List 1-5 short, checkable criteria a 100%-complete "
    "answer must satisfy.\n\n"
    "Reply with exactly this JSON shape:\n"
    "{\"final_request\": \"...\", \"was_incomplete\": true|false, "
    "\"provider\": \"...\", \"model\": \"...\", "
    "\"criteria\": [\"...\"], \"reason\": \"<one short line>\"}"
)

VERIFY_SYSTEM_PROMPT = (
    "You are the Astra AI Gateway's verifier. Earlier you understood a user "
    "request, assigned it to a Provider AI and defined what a complete "
    "answer needs. The Provider has now produced an output that the user "
    "has NOT seen yet. Judge it against the user's request and the "
    "criteria. Do not answer the request yourself.\n\n"
    "Mark it \"complete\" only if it fully and correctly satisfies the "
    "request and every criterion, is in the user's language, contains the "
    "real output (not a promise, placeholder or description of what it "
    "would do), and does not leak internal machinery. If anything is "
    "missing, wrong, cut off or off-topic, mark it \"incomplete\".\n\n"
    "For an incomplete output choose the action:\n"
    "  - \"fix\":  most of it is usable; the provider must patch what is "
    "missing/wrong.\n"
    "  - \"redo\": it is mostly unusable; the provider must start again "
    "from scratch.\n"
    "Then write `instructions`: clear, specific, imperative text addressed "
    "to the provider saying exactly what to fix or why to redo it.\n\n"
    "Reply with ONE JSON object and nothing else:\n"
    "{\"verdict\": \"complete\"|\"incomplete\", \"missing\": [\"...\"], "
    "\"action\": \"fix\"|\"redo\", \"instructions\": \"...\"}"
)


# ── helpers ─────────────────────────────────────────────────────────────────
def _parse_json_object(raw: str) -> dict | None:
    try:
        data = loads_lenient((raw or "").strip())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _clean(text) -> str:
    return " ".join(str(text or "").split()).strip()


def _describe_attachments(attachments) -> str:
    names = []
    for a in attachments or []:
        if isinstance(a, dict):
            name = a.get("original_filename") or a.get("filename") or "file"
            fam = a.get("family") or ""
        else:
            name = getattr(a, "original_filename", "") or "file"
            fam = getattr(a, "family", "")
        names.append(f"{name} ({fam})" if fam else str(name))
    return ", ".join(names)


def _has_image(attachments) -> bool:
    for a in attachments or []:
        fam = a.get("family") if isinstance(a, dict) else getattr(a, "family", "")
        if fam == "image":
            return True
    return False


class _ChatPort(ProviderExecutionPort):
    """Sends a correction back to the SAME provider/model that produced the
    output being corrected (never a different one — switching providers is
    the router's failover job, not the fix loop's). Remembers the last text
    it got back so the pipeline still has an answer if a later correction
    round fails."""

    def __init__(self, router, task_type: str, vision: bool, trace: str = ""):
        self._router = router
        self._task_type = task_type
        self._vision = vision
        self._trace = trace
        self.last_text = ""

    def execute(self, target, messages: list, max_tokens: int = 500,
                **kwargs) -> str:
        rr = self._router.route_request(RoutingRequest(
            task_type=self._task_type, messages=messages,
            preferred_provider=target.provider_id,
            preferred_model=target.model_id,
            vision=self._vision, max_tokens=max_tokens, no_fallback=True,
            trace=self._trace))
        if rr is None or not rr.ok:
            raise ProviderError(
                (rr.error if rr is not None else "") or "correction failed")
        self.last_text = rr.text
        return rr.text


# ── the pipeline ────────────────────────────────────────────────────────────
class ChatPipeline:
    def __init__(self, gateway, router, events=None, *,
                 max_tokens: int = 1500):
        self.gateway = gateway
        self.router = router
        self.events = events
        self.max_tokens = int(max_tokens or 1500)

    # -- plumbing ------------------------------------------------------------
    def _emit(self, kind: str, **data) -> None:
        if self.events:
            try:
                self.events.emit(kind, agent="chat.pipeline", **data)
            except Exception:
                pass

    def _gateway_usable(self) -> bool:
        try:
            return self.gateway is not None and bool(self.gateway.is_usable())
        except Exception:
            return False

    # -- step 1: Gateway call #1 (understand + assign) ------------------------
    def _understand(self, message: str, context: str, attachments,
                    req: str = "") -> dict:
        """Returns {"final_request", "was_incomplete", "provider", "model",
        "criteria", "reason", "ok"}. `ok` False means the call failed and
        the raw message is being used as-is."""
        fallback = {"final_request": message, "was_incomplete": False,
                    "provider": "", "model": "", "criteria": [],
                    "reason": "", "ok": False}
        targets = []
        try:
            targets = self.router.available_targets()
        except Exception:
            pass
        catalogue = "\n".join(
            f"- provider={t['provider']} model={t['model']} "
            f"caps={','.join(t.get('capabilities') or []) or 'chat'} "
            f"quality={t.get('quality') or '?'} ctx={t.get('context_window') or '?'}"
            for t in targets[:MAX_TARGETS_IN_PROMPT]) or "(none listed)"
        parts = ["Available providers/models:\n" + catalogue]
        ctx = _clean(context)
        if ctx:
            parts.append("Prior conversation (only to resolve references in the "
                         "message; not a new request):\n" + ctx)
        att = _describe_attachments(attachments)
        if att:
            parts.append("Attachments sent with the message: " + att)
        parts.append("User message:\n" + message)
        try:
            raw = self.gateway.chat(
                [{"role": "system", "content": UNDERSTAND_SYSTEM_PROMPT},
                 {"role": "user", "content": "\n\n".join(parts)}],
                max_tokens=UNDERSTAND_MAX_TOKENS, category="general",
                trace=req)
        except Exception as e:
            self._emit("chat.pipeline.understand_failed", error=str(e),
                       request=req, trace=req)
            return fallback
        data = _parse_json_object(raw)
        rewrote = bool((data or {}).get("was_incomplete"))
        if not data or (rewrote and not _clean(data.get("final_request"))):
            self._emit("chat.pipeline.understand_failed",
                       error="unparsable Gateway reply", request=req, trace=req)
            return fallback

        provider, model = _clean(data.get("provider")), _clean(data.get("model"))
        valid = {(t["provider"], t["model"]) for t in targets}
        if (provider, model) not in valid:
            # Never trust an invented target. Keep a provider-only pick when
            # the provider itself is real; otherwise leave routing automatic.
            provider = provider if any(p == provider for p, _ in valid) else ""
            model = ""
        criteria = [_clean(c) for c in (data.get("criteria") or [])
                    if _clean(c)][:5]
        return {"final_request": _clean(data["final_request"]) if rewrote
                else message,
                "was_incomplete": rewrote,
                "provider": provider, "model": model, "criteria": criteria,
                "reason": _clean(data.get("reason")), "ok": True}

    # -- step 3: Gateway call #2 (verify) -------------------------------------
    def _make_verifier(self, brief: dict, state: dict, req: str = ""):
        """Semantic verifier for `supervise_task`. It closes over `brief`, so
        verification knows everything call #1 decided."""
        def verifier(contract, result, evidence):
            state["verifications"] += 1
            output = (result.text or "")[:MAX_OUTPUT_CHARS_IN_VERIFY]
            crit = "\n".join(f"- {c}" for c in brief["criteria"]) or "- (none)"
            prompt = (
                f"Original user message:\n{brief['raw']}\n\n"
                f"Request you assigned (call #1):\n{brief['final_request']}\n\n"
                f"Completion criteria you defined:\n{crit}\n\n"
                f"Assigned to: {brief['assigned'] or 'automatic routing'}\n\n"
                f"Provider output to verify:\n{output}")
            try:
                raw = self.gateway.chat(
                    [{"role": "system", "content": VERIFY_SYSTEM_PROMPT},
                     {"role": "user", "content": prompt}],
                    max_tokens=VERIFY_MAX_TOKENS, category="reasoning",
                    trace=req)
            except Exception as e:
                state["unavailable"] = str(e)
                return (FAILED, f"verification unavailable: {e}")
            data = _parse_json_object(raw)
            verdict = str((data or {}).get("verdict") or "").lower()
            if verdict not in ("complete", "incomplete"):
                state["unavailable"] = "unparsable verifier reply"
                return (FAILED, "verification unavailable: unparsable reply")
            self._emit("chat.pipeline.verified", verdict=verdict,
                       n=state["verifications"], request=req, trace=req)
            if verdict == "complete":
                return (COMPLETE, "")
            missing = [_clean(m) for m in (data.get("missing") or [])
                       if _clean(m)]
            instructions = _clean(data.get("instructions"))
            state["last_missing"] = missing
            return (INCOMPLETE, instructions or "; ".join(missing) or
                    "output is not complete",
                    {"missing": missing, "action": data.get("action"),
                     "instructions": instructions})
        return verifier

    # -- helpers -----------------------------------------------------------------
    @staticmethod
    def _task_type(text: str, attachments) -> str:
        if _has_image(attachments):
            return "vision"
        t = classify(text)
        return t if t in _CHAT_TASK_TYPES else "simple_chat"

    def _route(self, task_type, messages, provider, model, vision, req=""):
        rr = self.router.route_request(RoutingRequest(
            task_type=task_type, messages=messages,
            preferred_provider=provider or None,
            preferred_model=model or None,
            vision=vision, max_tokens=self.max_tokens, trace=req))
        if (rr is None or not rr.ok) and task_type not in ("simple_chat", "vision") \
                and "no eligible" in (getattr(rr, "error", "") or ""):
            # A hard capability filter (e.g. "coding") left nothing to run
            # on — a plain chat turn can still be served by any model.
            rr = self.router.route_request(RoutingRequest(
                task_type="simple_chat", messages=messages,
                preferred_provider=provider or None,
                preferred_model=model or None, max_tokens=self.max_tokens,
                trace=req))
        return rr

    @staticmethod
    def _artifacts(text: str, message: str) -> list:
        try:
            d = os.path.join(tempfile.gettempdir(), "astra", "artifacts")
            return extract_artifacts(text, d, detect_output_type(message))
        except Exception:
            return []

    @staticmethod
    def _reply(text, ok, data, artifacts=None, note=""):
        out = {"reply": (text + note) if note else text, "action": "none",
               "ok": ok, "data": data}
        if artifacts:
            out["artifacts"] = artifacts
        return out

    # -- public entry point ---------------------------------------------------------
    def run(self, message: str, *, context: str = "", attachments=None) -> dict:
        raw = _clean(message)
        # Correlation id for this chat turn: every event, Gateway call and
        # Router request made below carries it, so the Activity Log can pair
        # each start with its terminal event and resolve any child operation
        # still "running" when the turn ends.
        req = new_op_id()
        if not raw and not attachments:
            return self._reply("Kichu likhun — ami help korte ready.", False,
                               {"stage": "input"})
        raw = raw or "(attachment only)"
        gateway_ok = self._gateway_usable()
        trace = {"gateway": "used" if gateway_ok else "unavailable",
                 "raw": raw, "request": req}
        self._emit("chat.pipeline.started", gateway=trace["gateway"],
                   op=f"chat:{req}", request=req, trace=req)

        # Any unexpected error must still close this turn's root operation:
        # without a terminal event the "Request received" row would stay
        # "… running" in the Activity Log forever (reconciliation keys off
        # the correlation ids, it never filters running rows away).
        try:
            return self._run_turn(raw, context, attachments, req, gateway_ok,
                                  trace)
        except Exception as e:
            self._emit("chat.pipeline.failed",
                       error=f"{type(e).__name__}: {e}",
                       op=f"chat:{req}", request=req, trace=req,
                       terminal=True)
            raise

    def _run_turn(self, raw, context, attachments, req, gateway_ok,
                  trace) -> dict:
        """The rest of one chat turn, split out of `run()` so it can
        guarantee a terminal event even when a step raises unexpectedly."""

        # 1) Gateway understands + assigns
        if gateway_ok:
            brief = self._understand(raw, context, attachments, req=req)
        else:
            brief = {"final_request": raw, "was_incomplete": False,
                     "provider": "", "model": "", "criteria": [],
                     "reason": "", "ok": False}
        assigned = (f"{brief['provider']}/{brief['model']}" if brief["model"]
                    else brief["provider"])
        trace.update({"understood": brief["final_request"],
                      "was_incomplete": brief["was_incomplete"],
                      "assigned": assigned, "criteria": brief["criteria"],
                      "assign_reason": brief["reason"]})
        self._emit("chat.pipeline.assigned", provider=brief["provider"],
                   model=brief["model"], was_incomplete=brief["was_incomplete"],
                   request=req, trace=req)

        # 2) Provider executes (output stays internal until verified)
        ctx = _clean(context)
        user_text = brief["final_request"]
        if ctx:
            user_text = ("Recent conversation (for reference):\n" + ctx +
                         "\n\nCurrent request:\n" + user_text)
        vision = _has_image(attachments)
        content = build_multimodal_content(user_text, attachments)
        messages = [{"role": "system", "content": PROVIDER_SYSTEM_PROMPT},
                    {"role": "user", "content": content}]
        task_type = self._task_type(brief["final_request"], attachments)
        rr = self._route(task_type, messages, brief["provider"],
                         brief["model"], vision, req=req)
        if rr is None or not rr.ok:
            err = (rr.error if rr is not None else "") or "unknown error"
            trace["error"] = err
            self._emit("chat.pipeline.failed", error=err, op=f"chat:{req}",
                       request=req, trace=req, terminal=True)
            return self._reply(
                "Provider theke kono uttor pawa jayni. Kichukkhon pore abar "
                f"try korun. (`{err}`)", False, trace)
        trace["served_by"] = f"{rr.provider}/{rr.model}"

        # gateway can't verify -> honest pass-through
        if not gateway_ok:
            trace["verification"] = {"status": "skipped"}
            # terminal event: the request is done even though verification was
            # skipped, so its started row never stays "running" in the log.
            self._emit("chat.pipeline.finished", status="skipped",
                       op=f"chat:{req}", request=req, trace=req, terminal=True)
            return self._reply(rr.text, True, trace,
                               self._artifacts(rr.text, raw))

        # 3) Gateway verifies; fix/redo loop until complete or bound reached
        state = {"verifications": 0, "unavailable": "", "last_missing": []}
        verify_brief = {"raw": raw, "final_request": brief["final_request"],
                        "criteria": brief["criteria"],
                        "assigned": f"{rr.provider}/{rr.model}"}
        contract = build_task_completion_contract(
            user_request=raw, goal=brief["final_request"],
            completion_criteria=brief["criteria"], require_semantic=True)
        port = _ChatPort(self.router, task_type, vision, trace=req)
        port.last_text = rr.text
        target = ProviderExecutionTarget(rr.provider, rr.model)
        try:
            final, outcome, attempts = self.gateway.supervise_task(
                port, target, messages,
                ProviderExecutionResult(ok=True, text=rr.text), contract,
                semantic_verifier=self._make_verifier(verify_brief, state, req=req),
                max_tokens=self.max_tokens)
        except Exception as e:      # a Gateway bug must not lose a good answer
            self._emit("chat.pipeline.verify_error", error=str(e),
                       op=f"chat:{req}", request=req, trace=req, terminal=True)
            trace["verification"] = {"status": "error", "reason": str(e)}
            return self._reply(
                rr.text, True, trace, self._artifacts(rr.text, raw),
                note="\n\n⚠️ Gateway verification kaj korenni — uttor ta "
                     "verify kora hoyni.")

        text = (final.text if final.ok and (final.text or "").strip()
                else port.last_text) or rr.text
        trace["verification"] = {"status": outcome.status, "attempts": attempts,
                                 "checks": state["verifications"],
                                 "reason": outcome.reason,
                                 "missing": outcome.missing or state["last_missing"]}
        self._emit("chat.pipeline.finished", status=outcome.status,
                   attempts=attempts, op=f"chat:{req}", request=req,
                   trace=req, terminal=True)
        arts = self._artifacts(text, raw)

        if outcome.status == COMPLETE:
            return self._reply(text, True, trace, arts)
        if state["unavailable"]:
            return self._reply(
                text, True, trace, arts,
                note="\n\n⚠️ Gateway ei uttor ta verify korte parenni, tai "
                     "100% confirm kora jayni.")
        missing = outcome.missing or state["last_missing"]
        detail = ("; ".join(missing) if missing else outcome.reason or
                  "kichu ongsho ekhono bakhi")
        tried = (f" ({attempts} bar fix korar chesta kora hoyeche)"
                 if attempts else "")
        return self._reply(
            text, True, trace, arts,
            note=f"\n\n⚠️ Gateway verification e ekhono 100% confirm hoyni"
                 f"{tried}. Missing: {detail}")
