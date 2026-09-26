/* Astra AI Agent — core frontend bootstrap.
 *
 * Loads /api/manifest, builds the tab bar, creates a tabview per plugin tab,
 * loads each plugin's JS (which self-registers via Astra.register), renders
 * the aggregated dashboard, and provides shared chat + backup.
 *
 * Plugin authors write:  Astra.register(slug, { title, render(el), dashboard() })
 */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
  } catch (e) {                       // server restarting / connection dropped
    return { ok: false, error: "network: " + (e && e.message || e) };
  }
  let body = {};
  try { body = await res.json(); } catch (_) { /* empty */ }
  return body;
}
const post = (p, b) => api(p, { method: "POST", body: b });
const patch = (p, b) => api(p, { method: "PATCH", body: b });
const del = (p) => api(p, { method: "DELETE" });

/* ------------------------------ plugin registry ---------------------------- */
window.Astra = {
  plugins: {},
  register(slug, def) {
    Astra.plugins[slug] = { rendered: false, ...def };
  },
};

/* -------------------------------- tabs -------------------------------------- */
let MANIFEST = null;
const loaders = {};
const TAB_LABELS = {};
/* Core tab loaders register here too (e.g. the Agent Workflow tab in
 * static/js/workflow.js) — one registry, one showTab path. */
window.Astra.loaders = loaders;
/* Current chat conversation id, for the Astra Agent Terminal (static/js/
 * terminal.js). The terminal derives `conv-<id>` from it so the PTY it
 * attaches to is the SAME session the chat agent's runtime tools use —
 * that is what makes "clone in chat, `ls` in the terminal" work. */
window.Astra.currentConversation = () =>
  (typeof CHAT !== "undefined" && CHAT ? (CHAT.conversationId || null) : null);

function showTab(name) {
  $$("#nav .tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".tabview").forEach((v) => v.classList.toggle("active", v.id === `tab-${name}`));
  closeNavMenu();
  try { localStorage.setItem("astra:active-tab", name); } catch (_) { /* ignore */ }
  const loader = loaders[name];
  if (loader) loader();
}

/* left-side 3-dot menu that holds the tab list */
function openNavMenu() {
  $("#nav").classList.add("open");
  $("#menu-btn").setAttribute("aria-expanded", "true");
}
function closeNavMenu() {
  $("#nav").classList.remove("open");
  $("#menu-btn").setAttribute("aria-expanded", "false");
}
$("#menu-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  const open = $("#nav").classList.contains("open");
  if (open) closeNavMenu(); else openNavMenu();
});
document.addEventListener("click", (e) => {
  if (!$("#nav-menu").contains(e.target)) closeNavMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeNavMenu();
});

function deadlineText(d, today = new Date()) {
  if (!d) return "";
  const diff = Math.round((new Date(d + "T00:00:00") - new Date(today.toDateString())) / 86400000);
  if (diff === 0) return ' class="hl-today"';
  if (diff < 0) return ' class="hl-overdue"';
  if (diff <= 7) return ' class="hl-soon"';
  return "";
}
function deadlineTitle(d) {
  if (!d) return "";
  const diff = Math.round((new Date(d + "T00:00:00") - new Date(new Date().toDateString())) / 86400000);
  if (diff === 0) return "TODAY!";
  if (diff < 0) return "overdue " + -diff + "d";
  return "in " + diff + "d";
}

/* ------------------------------ dashboard ---------------------------------- */
loaders.dashboard = async function () {
  const r = await api("/api/dashboard");
  if (!r.ok) return;
  const blocks = r.data || [];
  $("#dash-blocks").innerHTML = blocks.length
    ? blocks.map((b) => `
        <div class="panel dash-block">
          <div class="panel-head">
            <h3>${b.icon} ${esc(b.title)}</h3>
            <button class="btn mini" data-goto="${b.slug}">Open ➜</button>
          </div>
          <div class="dash-body" id="dash-${b.slug}"></div>
          <div class="cards" id="cards-${b.slug}"></div>
          <div class="grid2" id="grid-${b.slug}"></div>
        </div>`).join("")
    : `<div class="empty">Kono plugin nai — ekhoni ekta add korun 🛠️</div>`;
  blocks.forEach((b) => {
    const cards = b.data.cards || [];
    $("#cards-" + b.slug).innerHTML = cards.map((c) => `
      <div class="card"><div class="card-v">${esc(c.v)}</div>
        <div class="card-k">${esc(c.k)}</div><div class="card-s">${esc(c.s)}</div></div>`).join("");
    // plugin-specific extended dashboard (deadlines etc.)
    const def = Astra.plugins[b.slug];
    if (def && def.dashboard) {
      const el = $("#dash-" + b.slug);
      el.innerHTML = def.dashboard(b.data);
    }
  });
};
$("#dash-blocks").addEventListener("click", (e) => {
  const g = e.target.closest("[data-goto]");
  if (g) showTab(g.dataset.goto);
});

/* ----------------------------- assistant chat ------------------------------- */
loaders.assistant = function () {
  if (Astra.plugins._chatWired) return;
  Astra.plugins._chatWired = true;
  wireChatComposer();
  chatRestore();
};

/* The transcript lives on the server (astra/chat_log.py), so a page refresh
 * brings the conversation back instead of showing a blank "first open"
 * screen. If a reply was still being worked on when the page reloaded, the
 * typing indicator comes back too and the reply is picked up when it lands.
 *
 * `viewGen` guards every async chat operation (an in-flight send, a
 * background poll for a pending reply). It's bumped every time a different
 * chat is shown (page load, switching chats, opening a new one). Anything
 * started for an earlier view checks its own snapshot of `viewGen` against
 * the live value before touching the DOM — so a reply that finishes after
 * the user has already moved to another chat can never get painted into
 * the wrong one, and a background poll for chat A can never start showing
 * chat B's messages just because B became the server's "current" chat. */
const CHAT = { lastId: 0, conversationId: 0, viewGen: 0 };
const CHAT_EMPTY_HTML = ($("#chat-empty") || {}).outerHTML || "";
const CHAT_WAIT_MAX_MS = 15 * 60 * 1000;

// Call this whenever a (possibly different) chat is about to be shown.
// Resets the per-view state and the Send button's busy/disabled state to
// match the new view — any older poll loop that later notices its gen is
// stale just abandons quietly instead of touching this state itself.
function chatEnterView(conversationId) {
  CHAT.conversationId = conversationId;
  CHAT.lastId = 0;
  CHAT.viewGen++;
  chatStatusReset();
  const send = $("#chat-send");
  const input = $("#chat-input");
  if (send) {
    delete send.dataset.busy;
    send.disabled = !(input && input.value.trim());
  }
  return CHAT.viewGen;
}

function chatRenderMessages(msgs, markLast) {
  msgs.forEach((m, i) => {
    const isUser = m.role === "user";
    // Approve/Reject only makes sense on the newest message; anything older
    // was already answered (or superseded) before the refresh.
    const stale = m.action === "confirm" && !(markLast && i === msgs.length - 1) && markLast;
    chatBubble(isUser ? "me" : "ai", m.text, isUser ? null : (stale ? "none" : m.action),
               (m.files || []).map((n) => ({ name: n })), m.artifacts, m.data);
    CHAT.lastId = Math.max(CHAT.lastId, m.id || 0);
  });
}
async function chatRestore() {
  let r;
  try { r = await api("/api/chat/history"); } catch (_e) { return; }
  if (!r || !r.ok || !r.data) return;
  const gen = chatEnterView(r.data.conversation_id || 1);
  const msgs = r.data.messages || [];
  if (msgs.length) chatRenderMessages(msgs, true);
  CHAT.lastId = Math.max(CHAT.lastId, r.data.last_id || 0);
  if (r.data.pending) chatWaitForReply(gen, CHAT.conversationId);
}
// A turn was still running when the page (re)loaded, or the chat the user
// just switched to still has one in flight: show the typing indicator,
// lock Send, and poll that SPECIFIC chat's transcript until the reply
// arrives — never whatever chat happens to be "current" on the server by
// the time each poll fires.
function chatWaitForReply(gen, conversationId) {
  const send = $("#chat-send");
  const input = $("#chat-input");
  send.dataset.busy = "1";
  send.disabled = true;
  chatTyping();
  const started = Date.now();
  const finish = () => {
    if (gen !== CHAT.viewGen) return;   // a later view already owns this state
    chatStatusFinish();
    delete send.dataset.busy;
    send.disabled = !input.value.trim();
  };
  const tick = async () => {
    if (gen !== CHAT.viewGen) return;   // user moved to a different chat — abandon
    let r = null;
    try {
      r = await api("/api/chat/history?after_id=" + CHAT.lastId
        + "&conversation_id=" + conversationId);
    } catch (_e) { /* retry */ }
    if (gen !== CHAT.viewGen) return;   // moved on while that request was in flight
    if (r && r.ok && r.data) {
      const fresh = r.data.messages || [];
      if (fresh.length) {
        chatRenderMessages(fresh, false);
        if (r.data.pending) chatTyping();   // same row, back at the bottom
      }
      if (!r.data.pending) return finish();
    }
    if (Date.now() - started > CHAT_WAIT_MAX_MS) return finish();
    setTimeout(tick, 1500);
  };
  setTimeout(tick, 1500);
}
// "New chat" no longer wipes anything — it opens a fresh thread and the old
// one stays reachable from the ⋮ history menu (see chatSwitchTo below).
// Allowed even while the current chat still has a reply in flight — that
// chat keeps working in the background and is reachable again from the ⋮
// history menu. If the current chat is already a fresh, untouched "new
// chat" (nothing rendered, nothing in flight), clicking it again is a
// no-op: no API call, no re-render, nothing changes.
$("#chat-clear")?.addEventListener("click", async () => {
  if ($("#chat-empty")) return;   // already an empty new chat — nothing to do
  const r = await post("/api/chat/conversations");
  if (!r || !r.ok || !r.data) return;
  $("#chat-log").innerHTML = CHAT_EMPTY_HTML;
  chatEnterView(r.data.id);
  closeChatHistoryMenu();
});

/* -------------------- chat history (⋮ menu, multiple saved chats) --------- */
function closeChatHistoryMenu() {
  $("#chat-history-panel")?.classList.remove("open");
  $("#chat-history-btn")?.setAttribute("aria-expanded", "false");
}
function openChatHistoryMenu() {
  $("#chat-history-panel")?.classList.add("open");
  $("#chat-history-btn")?.setAttribute("aria-expanded", "true");
  chatLoadHistoryList();
}
$("#chat-history-btn")?.addEventListener("click", (e) => {
  e.stopPropagation();
  const open = $("#chat-history-panel")?.classList.contains("open");
  if (open) closeChatHistoryMenu(); else openChatHistoryMenu();
});
document.addEventListener("click", (e) => {
  if (!$("#chat-history-menu")?.contains(e.target)) closeChatHistoryMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeChatHistoryMenu();
});

// Server timestamps are naive "YYYY-MM-DD HH:MM:SS" local time — show just
// a clock time for today, else a short date.
function chatHistoryTimeLabel(ts) {
  if (!ts) return "";
  const d = new Date(String(ts).replace(" ", "T"));
  if (isNaN(d.getTime())) return "";
  const now = new Date();
  return d.toDateString() === now.toDateString()
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { day: "2-digit", month: "short" });
}

async function chatLoadHistoryList() {
  const list = $("#chat-history-list");
  if (!list) return;
  list.innerHTML = `<div class="empty">Loading…</div>`;
  let r;
  try { r = await api("/api/chat/conversations"); } catch (_e) { r = null; }
  const convos = (r && r.ok && r.data) || [];
  if (!convos.length) {
    list.innerHTML = `<div class="empty">Kono purono chat nei</div>`;
    return;
  }
  list.innerHTML = convos.map((c) => `
    <div class="chat-history-item${c.current ? " active" : ""}" data-id="${c.id}">
      <span class="chat-history-title">${esc(c.title || "New chat")}</span>
      <span class="chat-history-time muted">${esc(chatHistoryTimeLabel(c.updated_at))}</span>
      <button type="button" class="chat-history-del" data-del="${c.id}"
        title="Ei chat ta muche felun" aria-label="Delete chat">🗑</button>
    </div>`).join("");
}

// Swap the visible chat log for a saved chat's messages and make it the
// server's "current" thread, so new messages and a later page reload land
// back in the same conversation. Allowed even if the chat being left still
// has a reply in flight — that turn keeps running server-side regardless.
async function chatSwitchTo(id) {
  let r;
  try { r = await api("/api/chat/conversations/" + id); } catch (_e) { return; }
  if (!r || !r.ok || !r.data) return;
  chatShowLoadedHistory(r.data);
}
async function chatShowCurrent() {
  let r;
  try { r = await api("/api/chat/history"); } catch (_e) { return; }
  if (!r || !r.ok || !r.data) return;
  chatShowLoadedHistory(r.data);
}
function chatShowLoadedHistory(data) {
  $("#chat-log").innerHTML = CHAT_EMPTY_HTML;
  const gen = chatEnterView(data.conversation_id);
  const msgs = data.messages || [];
  if (msgs.length) chatRenderMessages(msgs, true);
  CHAT.lastId = Math.max(CHAT.lastId, data.last_id || 0);
  if (data.pending) chatWaitForReply(gen, data.conversation_id);
}

$("#chat-history-list")?.addEventListener("click", async (e) => {
  const delBtn = e.target.closest("[data-del]");
  if (delBtn) {
    e.stopPropagation();
    if (!confirm("Ei chat ta muche felben? Eta ar fire pawa jabe na.")) return;
    const r = await del("/api/chat/conversations/" + delBtn.dataset.del);
    if (r && r.ok) {
      await chatShowCurrent();     // deleting the open chat may switch us elsewhere
      chatLoadHistoryList();
    }
    return;
  }
  const item = e.target.closest(".chat-history-item");
  if (!item || item.classList.contains("active")) return closeChatHistoryMenu();
  await chatSwitchTo(item.dataset.id);
  closeChatHistoryMenu();
});

/* Host-terminal fallback approval card (Assistant Chat only).
 * Astra Agent Runtime is the PRIMARY execution environment and needs no
 * permission. A host command is a FALLBACK and is only ever run after the
 * user selects Allow here. This card is intentionally chat-only — the Astra
 * Agent Terminal never shows Allow/Deny. */
function _hoaRow(label, value, mono) {
  const row = document.createElement("div");
  row.className = "hoa-row";
  const l = document.createElement("div");
  l.className = "hoa-label";
  l.textContent = label;
  const v = document.createElement("div");
  v.className = "hoa-value" + (mono ? " mono" : "");
  v.textContent = (value === undefined || value === null || value === "")
    ? "—" : String(value);
  row.appendChild(l);
  row.appendChild(v);
  return row;
}

function _hoaStatus(ap) {
  const code = ap && ap.result ? ap.result.exit_code : undefined;
  switch ((ap && ap.status) || "pending") {
    case "denied":
      return ["✕ Denied", "Host command was not executed."];
    case "expired":
      return ["⌛ Expired", "Host command was not executed."];
    case "cancelled":
      return ["✕ Cancelled", "Host command was not executed."];
    case "approved":
    case "completed":
      return ["✓ Approved by you",
              "Host command executed" +
              ((code === undefined || code === null) ? "." : " (exit code " + code + ").")];
    case "failed":
      return ["⚠ Approved, but it failed",
              (ap && ap.error) ? String(ap.error) : "Host command was attempted once."];
    default:
      return null;
  }
}

function renderHostApproval(content, approval) {
  let ap = approval || {};
  const wrap = document.createElement("div");
  wrap.className = "msg-host-approval";
  const head = document.createElement("div");
  head.className = "hoa-head";
  head.textContent = "⚠ Host Terminal Access Required";
  wrap.appendChild(head);
  const sub = document.createElement("div");
  sub.className = "hoa-sub";
  sub.textContent = ap.reason
    ? (ap.reason + " — Astra Agent Runtime cannot perform this operation on its own.")
    : "Astra Agent Runtime cannot perform this operation.";
  wrap.appendChild(sub);
  wrap.appendChild(_hoaRow("Command", "$ " + (ap.command || ""), true));
  wrap.appendChild(_hoaRow("Working directory", ap.cwd, true));
  if (ap.reason) wrap.appendChild(_hoaRow("Reason", ap.reason, false));
  const danger = document.createElement("div");
  danger.className = "hoa-danger";
  danger.textContent = "This command will execute on the HOST system, " +
    "outside Astra Agent Runtime.";
  wrap.appendChild(danger);
  const actions = document.createElement("div");
  actions.className = "hoa-actions";
  const denyBtn = document.createElement("button");
  denyBtn.type = "button";
  denyBtn.className = "hoa-btn hoa-deny";
  denyBtn.textContent = "Deny";
  const allowBtn = document.createElement("button");
  allowBtn.type = "button";
  allowBtn.className = "hoa-btn hoa-allow";
  allowBtn.textContent = "Allow";
  actions.appendChild(denyBtn);
  actions.appendChild(allowBtn);
  wrap.appendChild(actions);
  const statusEl = document.createElement("div");
  statusEl.className = "hoa-status";
  wrap.appendChild(statusEl);

  const paint = (next) => {
    const info = _hoaStatus(next);
    if (!info) return;
    wrap.classList.add("resolved");
    if (actions.remove) actions.remove();
    else if (actions.parentElement) actions.parentElement.removeChild(actions);
    statusEl.textContent = "";
    const t = document.createElement("div");
    t.className = "hoa-status-title";
    t.textContent = info[0];
    statusEl.appendChild(t);
    const d = document.createElement("div");
    d.className = "hoa-status-detail";
    d.textContent = info[1];
    statusEl.appendChild(d);
    if (next && next.approval_id) {
      const idl = document.createElement("div");
      idl.className = "hoa-id";
      idl.textContent = "approval_id: " + next.approval_id;
      statusEl.appendChild(idl);
    }
  };
  paint(ap);

  const decide = async (allow) => {
    if (!ap || !ap.approval_id) return;
    if (ap.status && ap.status !== "pending") return;   // already resolved
    denyBtn.disabled = true;
    allowBtn.disabled = true;
    chatTyping();
    try {
      const r = await post("/api/terminal/approval/" + ap.approval_id,
                           { decision: allow ? "allow" : "deny" });
      if (!r.ok || !r.data) {
        chatStatusFail(r.error || "the approval could not be applied");
        chatBubble("ai", "Server e problem — `" + (r.error || "unknown error") + "`");
        return;
      }
      chatStatusFinish();
      ap = (r.data.data && r.data.data.approval) || ap;
      paint(ap);
      chatBubble("ai", r.data.reply, r.data.action, null, r.data.artifacts,
                 r.data.data);
    } catch (err) {
      chatStatusFail(String(err));
      chatBubble("ai", "Server e problem — `" + err + "`");
    }
  };
  denyBtn.addEventListener("click", () => decide(false));
  allowBtn.addEventListener("click", () => decide(true));
  content.appendChild(wrap);
}

function chatBubble(who, text, action, attachedFiles, artifacts, meta) {
  hideChatEmpty();
  const row = document.createElement("div");
  row.className = "msg " + (who === "me" ? "user" : "assistant");
  const content = document.createElement("div");
  content.className = "msg-content";
  if (who !== "me") {
    const avatar = document.createElement("div");
    avatar.className = "msg-avatar";
    const avatarImg = document.createElement("img");
    avatarImg.src = "/static/img/logo.png";
    avatarImg.alt = "Astra";
    avatar.appendChild(avatarImg);
    row.appendChild(avatar);
  }
  if (attachedFiles && attachedFiles.length) {
    const strip = document.createElement("div");
    strip.className = "msg-attachments";
    attachedFiles.forEach((f) => {
      const chip = document.createElement("span");
      chip.className = "msg-attach-chip";
      chip.textContent = _fileIcon(f.name) + " " + f.name;
      strip.appendChild(chip);
    });
    content.appendChild(strip);
  }
  const textEl = document.createElement("div");
  textEl.className = "msg-text";
  // Safe formatting (static/js/chat_format.js): text is HTML-escaped first,
  // ``` fences become a code block with a Copy button.
  textEl.innerHTML = (typeof AstraChatFormat !== "undefined")
    ? AstraChatFormat.toHtml(text)
    : esc(text).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
        .replace(/`(.+?)`/g, "<code>$1</code>").replace(/\n/g, "<br>");
  content.appendChild(textEl);
  if (artifacts && artifacts.length) {
    const artWrap = document.createElement("div");
    artWrap.className = "msg-artifacts";
    artifacts.forEach((a) => { artWrap.appendChild(renderArtifact(a)); });
    content.appendChild(artWrap);
  }
  if (action === "host_approval") {
    renderHostApproval(content, meta && meta.approval);
  } else if (action === "confirm") {
    // inline Approve/Reject — resolved right here in chat, never by
    // sending the user off to a separate tab.
    const eid = meta && meta.execution_id;
    const wrap = document.createElement("div");
    wrap.className = "msg-confirm-actions";
    const approveBtn = document.createElement("button");
    approveBtn.type = "button";
    approveBtn.className = "msg-confirm-btn msg-approve-btn";
    approveBtn.textContent = "✅ Approve";
    const rejectBtn = document.createElement("button");
    rejectBtn.type = "button";
    rejectBtn.className = "msg-confirm-btn msg-reject-btn";
    rejectBtn.textContent = "❌ Reject";
    const decide = async (allow) => {
      approveBtn.disabled = true;
      rejectBtn.disabled = true;
      wrap.classList.add("resolved");
      chatTyping();
      try {
        const r = await post("/api/chat/resume", { execution_id: eid, allow });
        if (!r.ok || !r.data) {
          chatStatusFail(r.error || "the approval could not be applied");
          chatBubble("ai", "Server e problem — `" + (r.error || "unknown error") + "`");
          return;
        }
        chatStatusFinish();
        chatBubble("ai", r.data.reply, r.data.action, null,
                   r.data.artifacts, r.data.data);
      } catch (err) {
        chatStatusFail(String(err));
        chatBubble("ai", "Server e problem — `" + err + "`");
      }
    };
    approveBtn.addEventListener("click", () => decide(true));
    rejectBtn.addEventListener("click", () => decide(false));
    wrap.appendChild(approveBtn);
    wrap.appendChild(rejectBtn);
    content.appendChild(wrap);
  } else if (action && action !== "none") {
    const link = document.createElement("button");
    link.type = "button";
    link.className = "msg-action-link";
    link.textContent = "→ " + (TAB_LABELS[action] || action) + " e dekhun";
    link.addEventListener("click", () => showTab(action));
    content.appendChild(link);
  }
  row.appendChild(content);
  $("#chat-log").appendChild(row);
  $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
  return row;
}
/* --------------------- assistant execution status --------------------------
 * The compact live status under the Astra avatar ("🚀 Working… • • •" + the
 * operation Astra is really running right now). It is driven by the SAME
 * lifecycle events the Activity Log consumes: AstraChatStatus
 * (static/js/chat_status.js) maps one event to a human-readable line plus a
 * step timeline, and this code only paints it. No timers, no simulated
 * progress, no second event system — an event that never arrives never
 * changes the line.
 *
 * Tapping the status expands the step timeline. It NEVER re-runs a terminal,
 * browser, file or web3 operation: the click handler only toggles a class.
 */
const CHAT_STATUS = { tracker: null, row: null, btn: null, summary: null,
                      line: null, steps: null };
const CHAT_STEP_MARK = { done: "✓", active: "●", failed: "✕", pending: "○" };

// A different chat view is taking over: drop the per-turn status state (the
// DOM row goes away with the re-rendered transcript).
function chatStatusReset() {
  CHAT_STATUS.tracker = null;
  CHAT_STATUS.row = null;
  CHAT_STATUS.btn = null;
  CHAT_STATUS.summary = null;
  CHAT_STATUS.line = null;
  CHAT_STATUS.steps = null;
  CHAT_STATUS.cards = null;
  chatCardsReset();
}

function chatStatusStepEl(step) {
  const wrap = document.createElement("div");
  wrap.className = "chat-step " + (step.state || "pending");
  const mark = document.createElement("span");
  mark.className = "chat-step-mark";
  mark.setAttribute("aria-hidden", "true");
  mark.textContent = CHAT_STEP_MARK[step.state] || "○";
  const label = document.createElement("span");
  label.className = "chat-step-label";
  label.textContent = step.label || "";   // textContent: an event cannot inject markup
  wrap.append(mark, label);
  if (step.duration) {
    const dur = document.createElement("span");
    dur.className = "chat-step-dur";
    dur.textContent = step.duration;
    wrap.appendChild(dur);
  }
  return wrap;
}

// Build (once per turn) the status row: compact line, step timeline.
// No avatar here (by design) — the status card is a system-style progress
// indicator, not a chat message from Astra, so it doesn't carry the brand
// avatar the way actual assistant replies do.
function chatStatusBuild() {
  const row = document.createElement("div");
  row.className = "msg assistant chat-status-row";
  const content = document.createElement("div");
  content.className = "msg-content";

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "chat-status working";
  btn.setAttribute("aria-expanded", "false");
  btn.setAttribute("aria-label", "Execution status — tap to see the steps");

  const head = document.createElement("span");
  head.className = "chat-status-head";
  const summary = document.createElement("span");
  summary.className = "chat-status-summary";
  summary.setAttribute("role", "status");
  summary.setAttribute("aria-live", "polite");
  const dots = document.createElement("span");
  dots.className = "chat-status-dots";
  dots.setAttribute("aria-hidden", "true");
  for (let i = 0; i < 3; i++) dots.appendChild(document.createElement("i"));
  const caret = document.createElement("span");
  caret.className = "chat-status-caret";
  caret.setAttribute("aria-hidden", "true");
  caret.textContent = "▾";
  head.append(summary, dots, caret);

  const line = document.createElement("span");
  line.className = "chat-status-action";

  btn.append(head, line);

  const steps = document.createElement("div");
  steps.className = "chat-steps";
  steps.hidden = true;

  // The execution cards live BELOW the compact status (and below the step
  // timeline): one card per real tool execution, in order.
  const cards = document.createElement("div");
  cards.className = "chat-cards";

  btn.addEventListener("click", () => {
    // The ONLY thing this click does: reveal the timeline. No tool is ever
    // re-invoked from here (nothing but classList/hidden/aria is touched).
    const open = btn.classList.toggle("open");
    steps.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  });

  content.append(btn, steps, cards);
  row.append(content);
  return { row: row, btn: btn, summary: summary, line: line, steps: steps,
           cards: cards };
}

function chatStatusPaint(snap) {
  if (!CHAT_STATUS.btn || !snap) return;
  CHAT_STATUS.btn.classList.remove("working", "completed", "failed");
  CHAT_STATUS.btn.classList.add(snap.state || "working");
  CHAT_STATUS.summary.textContent = snap.summary || "Working…";
  CHAT_STATUS.line.textContent = snap.current || "";

  const steps = CHAT_STATUS.steps;
  while (steps.firstChild) steps.removeChild(steps.firstChild);
  const head = document.createElement("div");
  head.className = "chat-steps-head";
  head.textContent = (snap.state === "working" ? "🚀 " : "") +
                     (snap.panelTitle || "Execution steps");
  steps.appendChild(head);
  (snap.steps || []).forEach((st) => steps.appendChild(chatStatusStepEl(st)));

  const log = $("#chat-log");
  if (log && CHAT_STATUS.row && CHAT_STATUS.row.parentElement === log) {
    log.scrollTop = log.scrollHeight;
  }
}

// One live event (the same object the Activity Log just received).
function chatStatusTrack(event) {
  if (!CHAT_STATUS.tracker) return;     // nothing on screen — ignore it
  chatStatusPaint(CHAT_STATUS.tracker.apply(event));
  chatCardsTrack(event);                // and the per-execution cards
}

/* --------------------- assistant execution cards ---------------------------
 * Claude-style EXECUTION CARDS rendered under the compact live status: ONE
 * card per REAL tool execution — a terminal command is exactly one card, and
 * three terminal_exec calls are three cards, never merged into one timeline
 * row. AstraChatStatus.createCardTracker() owns the correlation/order/stale
 * rules; this code only paints them and toggles their detail.
 *
 * Tapping a card ONLY expands a read-only detail built from the values the
 * lifecycle events already carried — it never calls an API and never re-runs
 * a tool. "Load full output" is a separate, explicit button that pages the
 * existing read-only terminal retrieval endpoint; nothing is dumped into the
 * chat itself.
 */
const CHAT_CARDS = { tracker: null, box: null, els: {} };
const CHAT_CARD_MARK = { running: "●", completed: "✓", failed: "✕",
                         stopped: "■" };
const CHAT_CARD_OUT_CHUNK = 6000;

function chatCardsReset() {
  CHAT_CARDS.tracker = null;
  CHAT_CARDS.box = null;
  CHAT_CARDS.els = {};
}

function chatCardsClear() {
  CHAT_CARDS.els = {};
  const box = CHAT_CARDS.box;
  if (box) while (box.firstChild) box.removeChild(box.firstChild);
}

function chatCardField(label, value, mono) {
  const row = document.createElement("div");
  row.className = "chat-exec-row";
  const k = document.createElement("span");
  k.className = "chat-exec-k";
  k.textContent = label;
  const v = document.createElement("span");
  v.className = "chat-exec-v" + (mono ? " mono" : "");
  v.textContent = value;                 // textContent: an event cannot inject markup
  row.append(k, v);
  return row;
}

// Lazily page the full stdout/stderr through the EXISTING read-only retrieval
// endpoint. Exposed as its own function so tests can assert the card toggle
// itself never fetches while this explicit action does exactly one GET.
async function chatCardLoadOutput(el, stream) {
  const snap = el.snap || {};
  const o = el.out || (el.out = { stream: "stdout", text: "", offset: 0,
                                 done: false, loading: false, loaded: false,
                                 error: "" });
  if (stream && stream !== o.stream) {
    o.stream = stream;
    o.text = ""; o.offset = 0; o.done = false; o.loaded = false; o.error = "";
  }
  if (o.loading || o.done) return o;
  o.loading = true;
  const blob = o.stream === "stderr" ? snap.blobErr : snap.blobId;
  let url = "/api/terminal/output?stream=" + o.stream +
            "&offset=" + o.offset + "&length=" + CHAT_CARD_OUT_CHUNK;
  if (blob) url += "&blob_id=" + encodeURIComponent(blob);
  else if (snap.processId) url += "&process_id=" + encodeURIComponent(snap.processId);
  try {
    const r = await api(url);
    if (!r || !r.ok || !r.data) {
      o.error = (r && r.error) || "Full output is no longer available.";
    } else {
      const d = r.data;
      o.text = (o.loaded ? o.text : "") + (d.text || "");
      o.loaded = true;
      const next = d.next_offset;
      o.offset = (next === null || next === undefined)
        ? o.offset + (d.chars_returned || 0) : next;
      o.done = !!d.done;
      o.error = "";
    }
  } catch (err) {
    o.error = "Full output could not be loaded.";
  }
  o.loading = false;
  chatCardDetail(el, el.snap);
  return o;
}

function chatCardOutput(el, snap) {
  const box = document.createElement("div");
  box.className = "chat-exec-outbox";
  const o = el.out || (el.out = { stream: "stdout", text: "", offset: 0,
                                  done: false, loading: false, loaded: false,
                                  error: "" });
  if (snap.blobErr) {
    const tabs = document.createElement("div");
    tabs.className = "chat-exec-tabs";
    ["stdout", "stderr"].forEach((name) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chat-exec-tab" + (o.stream === name ? " active" : "");
      b.textContent = name;
      b.addEventListener("click", () => chatCardLoadOutput(el, name));
      tabs.appendChild(b);
    });
    box.appendChild(tabs);
  }
  const pre = document.createElement("pre");
  pre.className = "chat-exec-pre";
  pre.textContent = o.loaded ? o.text
                             : (snap.preview || "No output captured yet.");
  box.appendChild(pre);
  const note = document.createElement("div");
  note.className = "chat-exec-note";
  if (o.error) note.textContent = o.error;
  else if (o.loading) note.textContent = "Loading…";
  else if (o.done) note.textContent = "End of output";
  box.appendChild(note);
  const more = document.createElement("button");
  more.type = "button";
  more.className = "chat-exec-more";
  more.textContent = o.loaded ? "Load more" : "Load full output";
  more.hidden = o.done;
  more.addEventListener("click", () => chatCardLoadOutput(el, o.stream));
  box.appendChild(more);
  return box;
}

// The read-only detail panel. Built from lifecycle values ONLY — nothing here
// re-runs the tool, and only the terminal "load full output" button touches
// the network.
function chatCardDetail(el, snap) {
  const d = el.detail;
  if (!d) return;
  snap = snap || el.snap || {};
  while (d.firstChild) d.removeChild(d.firstChild);
  d.appendChild(chatCardField("Action", snap.title || "Tool"));
  if (snap.command) d.appendChild(chatCardField("Command", snap.command, true));
  if (snap.cwd) d.appendChild(chatCardField("Directory", snap.cwd, true));
  d.appendChild(chatCardField("Status", snap.stateText || ""));
  if (snap.exitCode !== null && snap.exitCode !== undefined) {
    d.appendChild(chatCardField("Exit code", String(snap.exitCode)));
  }
  d.appendChild(chatCardField("Duration",
    snap.duration || (snap.state === "running" ? "running" : "")));
  if (snap.kind === "terminal") d.appendChild(chatCardOutput(el, snap));
  else if (snap.preview) {
    const pre = document.createElement("pre");
    pre.className = "chat-exec-pre";
    pre.textContent = snap.preview;
    d.appendChild(pre);
  }
}

// One card = one real execution. Elements are reconciled by card id so an
// update never disturbs an open detail panel or the order on screen.
function chatCardEl(snap) {
  let el = CHAT_CARDS.els[snap.id];
  if (el) return el;
  const wrap = document.createElement("div");
  wrap.className = "chat-exec " + snap.kind;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "chat-exec-card";
  btn.setAttribute("aria-expanded", "false");
  const mark = document.createElement("span");
  mark.className = "chat-exec-mark";
  mark.setAttribute("aria-hidden", "true");
  const main = document.createElement("span");
  main.className = "chat-exec-main";
  const title = document.createElement("span");
  title.className = "chat-exec-title";
  const sub = document.createElement("span");
  sub.className = "chat-exec-sub";
  main.append(title, sub);
  const dur = document.createElement("span");
  dur.className = "chat-exec-dur";
  const chev = document.createElement("span");
  chev.className = "chat-exec-chev";
  chev.setAttribute("aria-hidden", "true");
  chev.textContent = "›";
  btn.append(mark, main, dur, chev);
  const detail = document.createElement("div");
  detail.className = "chat-exec-detail";
  detail.hidden = true;
  el = { wrap, btn, mark, title, sub, dur, detail, snap: snap, out: null };
  btn.addEventListener("click", () => {
    // The ONLY thing this click does: reveal/hide the read-only detail.
    const open = wrap.classList.toggle("open");
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    detail.hidden = !open;
    if (open) chatCardDetail(el, el.snap);
  });
  wrap.append(btn, detail);
  CHAT_CARDS.els[snap.id] = el;
  return el;
}

function chatCardPaint(snap) {
  const el = chatCardEl(snap);
  el.snap = snap;
  el.wrap.className = "chat-exec " + snap.kind;
  el.btn.className = "chat-exec-card " + snap.state;
  el.mark.textContent = CHAT_CARD_MARK[snap.state] || "•";
  el.title.textContent = snap.title || snap.tool || "Tool";
  el.sub.textContent = snap.command || "";
  el.sub.hidden = !snap.command;
  el.dur.textContent = snap.duration || "";
  el.dur.hidden = !snap.duration;
  if (!el.detail.hidden) chatCardDetail(el, snap);
  return el;
}

function chatCardsPaint(snaps) {
  const box = CHAT_CARDS.box;
  if (!box) return;
  const seen = {};
  (snaps || []).forEach((snap) => {
    seen[snap.id] = true;
    const el = chatCardPaint(snap);
    if (el.wrap.parentElement !== box) box.appendChild(el.wrap);
  });
  Object.keys(CHAT_CARDS.els).forEach((id) => {
    if (seen[id]) return;
    const el = CHAT_CARDS.els[id];
    if (el && el.wrap.parentElement === box) box.removeChild(el.wrap);
    delete CHAT_CARDS.els[id];
  });
  const log = $("#chat-log");
  if (log && box.parentElement) log.scrollTop = log.scrollHeight;
}

// One live event (the same object the status tracker just consumed).
function chatCardsTrack(event) {
  if (!CHAT_CARDS.tracker) return;
  chatCardsPaint(CHAT_CARDS.tracker.apply(event));
}

// The turn is over (reply landed, or failed): no card may still say Running.
function chatCardsEnd() {
  if (!CHAT_CARDS.tracker) return;
  CHAT_CARDS.tracker.endTurn();
  chatCardsPaint(CHAT_CARDS.tracker.list());
}


// The reply landed (or the transport failed): close the status out with the
// steps that were really observed.
function chatStatusFinish() {
  if (!CHAT_STATUS.tracker) return null;
  const snap = CHAT_STATUS.tracker.complete();
  chatStatusPaint(snap);
  chatCardsEnd();
  return snap;
}
function chatStatusFail(reason) {
  if (!CHAT_STATUS.tracker) return null;
  const snap = CHAT_STATUS.tracker.fail(reason);
  chatStatusPaint(snap);
  chatCardsEnd();
  return snap;
}

function chatTyping() {
  hideChatEmpty();
  if (typeof AstraChatStatus === "undefined") return null;
  const log = $("#chat-log");
  if (CHAT_STATUS.row && CHAT_STATUS.row.parentElement === log) {
    // A restored/pending turn re-opens the indicator: keep the SAME row and
    // move it back to the bottom, never a second one.
    log.appendChild(CHAT_STATUS.row);
  } else {
    const built = chatStatusBuild();
    CHAT_STATUS.row = built.row;
    CHAT_STATUS.btn = built.btn;
    CHAT_STATUS.summary = built.summary;
    CHAT_STATUS.line = built.line;
    CHAT_STATUS.steps = built.steps;
    CHAT_STATUS.cards = built.cards;
    log.appendChild(built.row);
  }
  CHAT_STATUS.tracker = AstraChatStatus.createTracker();
  // A new turn owns a fresh card list: the previous turn's cards are history
  // that the transcript already shows, and must not be extended by this one.
  CHAT_CARDS.box = CHAT_STATUS.cards;
  CHAT_CARDS.tracker = AstraChatStatus.createCardTracker();
  chatCardsClear();
  chatStatusPaint(CHAT_STATUS.tracker.begin());
  // The chat needs the live feed whether or not the Logs tab was ever opened.
  ensureEventStream();
  log.scrollTop = log.scrollHeight;
  return CHAT_STATUS.row;
}
function hideChatEmpty() {
  const empty = $("#chat-empty");
  if (empty) empty.remove();
}
function wireChatComposer() {
  const input = $("#chat-input");
  const sendBtn = $("#chat-send");
  const autosize = () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
    sendBtn.disabled = !input.value.trim() || !!sendBtn.dataset.busy;
  };
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      $("#chat-form").requestSubmit();
    }
  });
  $("#chat-suggestions")?.addEventListener("click", (e) => {
    const btn = e.target.closest(".suggestion");
    if (!btn) return;
    input.value = btn.dataset.text || "";
    autosize();
    input.focus();
  });
  autosize();
}
$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#chat-input");
  const msg = input.value.trim();
  const hasAttachments = _pendingAttachments.length > 0;
  if (!msg && !hasAttachments) return;
  if ($("#chat-send").dataset.busy) return;   // a restored reply is still running
  // Snapshot which chat this is being sent to. The user is free to switch
  // chats (or open a new one) before the reply comes back — it's already
  // saved server-side under this chat regardless — so only paint the
  // bubble here if this chat is still the one on screen when it lands.
  const sentGen = CHAT.viewGen;
  chatBubble("me", msg, null, _pendingAttachments.map((a) => a.file));
  input.value = "";
  input.style.height = "auto";
  $("#chat-send").disabled = true;
  chatTyping();
  showUploadIndicator(hasAttachments);
  try {
    let r;
    if (hasAttachments) {
      const fd = new FormData();
      fd.append("message", msg);
      _pendingAttachments.forEach((a) => fd.append("files", a.file));
      clearAttachments();
      const res = await fetch("/api/chat", { method: "POST", body: fd });
      r = await res.json();
    } else {
      r = await post("/api/chat", { message: msg });
    }
    hideUploadIndicator();
    if (sentGen !== CHAT.viewGen) return;   // moved to a different chat — leave it be
    if (!r.ok || !r.data) {
      chatStatusFail(r.error || "the server returned no reply");
      chatBubble("ai", "Server e problem — `" + (r.error || "unknown error") + "`");
      return;
    }
    chatStatusFinish();
    if (r.data.action === "dashboard") loaders.dashboard();
    chatBubble("ai", r.data.reply, r.data.action, null, r.data.artifacts, r.data.data);
  } catch (err) {
    hideUploadIndicator();
    if (sentGen !== CHAT.viewGen) return;   // moved to a different chat — leave it be
    chatStatusFail(String(err));
    chatBubble("ai", "Server e problem — `" + err + "`");
  }
});


/* -------------------------------- backup ------------------------------------ */
$("#btn-export").addEventListener("click", async () => {
  const r = await api("/api/export");
  if (!r.ok) return;
  const blob = new Blob([JSON.stringify(r.data, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `astra-backup-${new Date().toISOString().slice(0, 10)}.json`;
  a.click();
  $("#backup-info").textContent = "✅ Export done!";
});
$("#btn-import").addEventListener("click", () => $("#import-file").click());
$("#import-file").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const data = JSON.parse(await f.text());
  const r = await post("/api/import", { data });
  const parts = Object.entries(r.data || {})
    .map(([k, v]) => `${esc(k)}: ${esc(v)}`).join(", ");
  $("#backup-info").textContent = "✅ Imported — " + (parts || "done");
  loaders.dashboard();
});

/* ------------------------------ ACTIVITY LOG (core tab) ---------------------- */
loaders.logs = async function () {
  if (!Astra.plugins._logsLoaded) {
    Astra.plugins._logsLoaded = true;
    initLogsToolbar();
    const lastId = await loadLogsHistory();
    if (window.EventSource) openSse(lastId);
    else setInterval(eventsPoll, 3000);   // fallback for older browsers
  } else if (LOGS.follow) {
    // Returning to the tab: snap back to the newest entry so live-follow
    // resumes at the bottom instead of the top of the scroll area.
    jumpToLatest();
  }
};

// The timeline shows real application activity, not every internal step.
// AstraLog.isMeaningful() drops pure heartbeats and duplicate call events
// (scheduler.tick, streamed ai.token, the Gateway's own ai.* mirror of an
// astra_gateway.* call) while keeping chat/agent/ai/tool/browser/web3 work.
const isImportantEvent = (e) => AstraLog.isMeaningful(e);

// Everything that already happened before the Logs tab/SSE connection
// opened lives in the events table — load it once up front so the panel
// shows the full picture, not just events from this moment forward. Rows
// render oldest -> newest (newest at the bottom); the newest id is returned
// so the live SSE stream resumes exactly there — no gap, no duplicates.
async function loadLogsHistory() {
  const feed = $("#live-feed");
  try {
    // fetch more than the display buffer needs since some rows get filtered.
    const r = await api("/api/events?limit=500");
    const rows = (r && r.ok && r.data) ? r.data : [];
    if (!rows.length) return 0;
    if (feed && feed.firstElementChild && feed.firstElementChild.classList.contains("empty"))
      feed.innerHTML = "";
    // /api/events returns newest-first; the timeline is chronological, so
    // order by id and append. Then snap to the bottom in follow mode.
    AstraLog.orderHistory(rows).forEach((e) => upsertEvent(e, true));
    jumpToLatest();
    return rows[0].id || 0;
  } catch (_) {
    return 0;
  }
}

/* ------------------------------ logs toolbar --------------------------------
 * Category filters, search, pause/resume and clear for the Live activity
 * feed. Purely client-side: every rendered feed-line carries the event kind
 * + a resolved category as data attributes, and the toolbar just toggles
 * visibility / appends nothing while paused. */
const LOGS = AstraLog.createState();
const LOGS_MAX_BUFFER = 300;   // bounded DOM: oldest rows drop off the top

// Compact breakdown shown above the timeline (matches the filter chips).
const LOG_STAT_DEFS = [
  { key: "total", label: "Total" },
  { key: "agents", label: "Agents" },
  { key: "ai", label: "AI" },
  { key: "tools", label: "Tools" },
  { key: "errors", label: "Errors" },
];

function renderLogStats() {
  const el = $("#logs-stats");
  if (el) el.innerHTML = LOG_STAT_DEFS.map((d) =>
    `<div class="card"><div class="card-v">${LOGS.counts[d.key] || 0}</div>` +
    `<div class="card-k">${esc(d.label)}</div></div>`).join("");
  const count = $("#logs-count");
  if (count) count.textContent = `${LOGS.counts.total} events`;
}

// header live state: ● LIVE | ○ RECONNECTING | Ⅱ PAUSED
function setLiveState(state) {
  const el = $("#logs-live");
  if (!el) return;
  el.textContent = state === "live" ? "● LIVE"
                 : state === "paused" ? "Ⅱ PAUSED"
                 : "○ RECONNECTING";
  el.dataset.state = state;
}
function refreshLiveState() {
  setLiveState(LOGS.paused ? "paused" : SSE_STATE);
}

function feedMetrics() {
  const feed = $("#live-feed");
  if (!feed) return {};
  return { scrollTop: feed.scrollTop, scrollHeight: feed.scrollHeight,
           clientHeight: feed.clientHeight };
}

// Scroll to the newest row and resume live-follow.
function jumpToLatest() {
  const feed = $("#live-feed");
  AstraLog.onJumpToLatest(LOGS);
  if (feed) feed.scrollTop = feed.scrollHeight;
  const jump = $("#logs-jump");
  if (jump) jump.hidden = true;
}

function showJump(unread) {
  const jump = $("#logs-jump");
  if (!jump) return;
  jump.textContent = unread > 0 ? `↓ New logs · ${unread}` : "↓ New logs";
  jump.hidden = false;
}

function metaText(m) {
  const dur = m.duration ? " · " + m.duration : "";
  if (m.status === "err") {
    return m.endTime ? "✖ FAILED" + dur : "✖ " + (m.detail || "error");
  }
  if (m.status === "warn") return "⚠ " + (m.detail || "warning");
  if (m.status === "running") return "… RUNNING";
  if (m.status === "ok") {
    return m.endTime ? "✓ COMPLETE" + dur : "✓ " + (m.detail || "done");
  }
  // statusOf() only ever returns running/ok/warn/err, so this is a purely
  // defensive fallback for an unknown status — it still names a state
  // instead of leaving a primary row with a bare, meaningless bullet.
  return "• " + (m.detail || "event");
}

// Copy text to the clipboard. Uses the async Clipboard API when available and
// falls back to a hidden textarea + execCommand (older browsers / non-secure
// origins). Resolves on success, rejects on failure.
function copyText(text) {
  const fallback = () => new Promise((resolve, reject) => {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;top:0;left:0;opacity:0;";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      if (ok) resolve(); else reject(new Error("copy failed"));
    } catch (e) { reject(e); }
  });
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text).catch(fallback);
  }
  return fallback();
}

// Copy button on a chat code block (blocks are rendered by chat_format.js).
// One delegated listener: message HTML is rebuilt on every restore/switch.
document.addEventListener("click", (ev) => {
  const btn = ev.target && ev.target.closest && ev.target.closest(".code-copy");
  if (!btn) return;
  const block = btn.closest(".code-block");
  const code = block && block.querySelector("pre");
  if (!code) return;
  copyText(code.textContent || "").then(() => "Copied ✓", () => "Copy failed")
    .then((label) => {
      btn.textContent = label;
      setTimeout(() => { btn.textContent = "Copy"; }, 1500);
    });
});

function buildBlock(title, text) {
  const wrap = document.createElement("div");
  wrap.className = "tl-block";
  const head = document.createElement("div");
  head.className = "tl-block-head";
  const h = document.createElement("h5");
  h.textContent = title;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "tl-copy";
  btn.textContent = "Copy";
  btn.title = "Copy " + title.toLowerCase();
  btn.addEventListener("click", (ev) => {
    ev.stopPropagation();          // never toggles the row
    copyText(text).then(() => "Copied ✓", () => "Copy failed").then((label) => {
      btn.textContent = label;
      setTimeout(() => { btn.textContent = "Copy"; }, 1500);
    });
  });
  head.append(h, btn);
  const pre = document.createElement("pre");
  pre.textContent = text;         // textContent => no HTML injection
  wrap.append(head, pre);
  return wrap;
}

function buildDetails(m) {
  const frag = document.createDocumentFragment();
  const dl = document.createElement("dl");
  dl.className = "tl-fields";
  // Started/Completed come straight from the backend event timestamps (never
  // DOM insertion time); the sort position stays the start.
  const fields = [];
  if (m.time) fields.push(["Started", m.time]);
  if (m.endTime && m.endTime !== m.time) fields.push(["Completed", m.endTime]);
  if (m.duration) fields.push(["Duration", m.duration]);
  fields.push(["Status", AstraLog.statusLabel(m)]);
  for (const f of m.fields) fields.push(f);
  fields.forEach(([label, value]) => {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    dl.append(dt, dd);
  });
  frag.appendChild(dl);
  if (m.input) frag.appendChild(buildBlock("Input", m.input));
  if (m.output) frag.appendChild(buildBlock("Output", m.output));
  return frag;
}

// One collapsed timeline row + its expandable details. Built from DOM nodes
// (never innerHTML for event data) so a crafted event cannot inject markup.
// (Re)build a row's contents from a normalized model. Safe: every dynamic
// value goes through textContent; only static structure uses innerHTML.
function fillRow(row, m) {
  row.dataset.category = m.category;
  row.dataset.status = m.status;
  row.dataset.text = m.search;
  row.dataset.cats = m.category +
    ((m.status === "err" || m.kind === "operation.interrupted")
      ? ",errors" : "");
  row.innerHTML = "";

  const ind = document.createElement("span");
  ind.className = "tl-ind";
  const dot = document.createElement("span");
  dot.className = "tl-dot " + m.status;
  ind.appendChild(dot);

  const time = document.createElement("span");
  time.className = "tl-time";
  const span = AstraLog.timeRangeOf(m);
  time.appendChild(document.createTextNode(span.start));
  if (span.end) {
    const sep = document.createElement("span");
    sep.className = "tl-time-sep";
    sep.textContent = " → ";
    const end = document.createElement("span");
    end.className = "tl-time-end";
    end.textContent = span.end;
    time.append(sep, end);
  }

  const main = document.createElement("span");
  main.className = "tl-main";
  const title = document.createElement("span");
  title.className = "tl-title";
  const ico = document.createElement("span");
  ico.className = "tl-ico";
  ico.textContent = m.icon;
  title.append(ico, document.createTextNode(m.title));
  main.appendChild(title);
  if (m.subject) {
    const sub = document.createElement("span");
    sub.className = "tl-sub";
    sub.textContent = m.subject;
    main.appendChild(sub);
  }

  const meta = document.createElement("span");
  meta.className = "tl-meta";
  meta.textContent = metaText(m);

  row.append(ind, time, main, meta);

  const detail = document.createElement("div");
  detail.className = "tl-detail";
  detail.hidden = true;
  detail.appendChild(buildDetails(m));
  row.appendChild(detail);

  row._astraModel = m;
  row.classList.toggle("hidden",
    !AstraLog.matchesRow(row.dataset.cats, row.dataset.text,
                         LOGS.filter, LOGS.query));
}

function buildRow(m) {
  const row = document.createElement("div");
  row.className = "tl-row";
  row.dataset.id = m.id;
  row.setAttribute("role", "button");
  row.tabIndex = 0;
  row.setAttribute("aria-expanded", "false");

  const toggle = () => {
    const open = row.classList.toggle("open");
    const detail = $(".tl-detail", row);
    if (detail) detail.hidden = !open;
    row.setAttribute("aria-expanded", open ? "true" : "false");
  };
  // Clicks inside the expanded details (Input/Output text, Copy buttons)
  // must never collapse the row — otherwise text can't be selected or
  // copied. Only the header line toggles, and a click that ends a text
  // selection is not a toggle either.
  row.addEventListener("click", (ev) => {
    if (ev.target.closest && ev.target.closest(".tl-detail")) return;
    const sel = window.getSelection && window.getSelection();
    if (sel && !sel.isCollapsed && row.contains(sel.anchorNode)) return;
    toggle();
  });
  row.addEventListener("keydown", (ev) => {
    if (ev.target !== row) return;   // Enter/Space on an inner button is its own
    if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); }
  });
  fillRow(row, m);
  return row;
}

// Update an existing row in place (start -> completion), preserving whether
// the user had it expanded. The row keeps its original timeline identity
// (start time/position) — only its lifecycle state changes.
function updateRow(row, m) {
  const wasOpen = row.classList.contains("open");
  fillRow(row, AstraLog.mergeLifecycle(row._astraModel, m));
  if (wasOpen) {
    row.classList.add("open");
    row.setAttribute("aria-expanded", "true");
    const detail = $(".tl-detail", row);
    if (detail) detail.hidden = false;
  }
}

// A child operation that was still "running" when its request/run ended.
// The decision is `AstraLog.interruptedModel()` (pure + unit-tested); this
// only applies it to the live row.
function resolveRow(row, reason) {
  const next = AstraLog.interruptedModel(row._astraModel, reason);
  if (next === row._astraModel) return;   // already terminal, nothing to do
  updateRow(row, next);
}

function trimBuffer(feed) {
  while (feed.children.length > LOGS_MAX_BUFFER) {
    const before = feed.scrollTop;
    const removed = feed.firstElementChild;
    const removedH = removed.offsetHeight || 0;
    const lk = removed.dataset.lifecycleKey;
    feed.removeChild(removed);
    if (lk && LOGS.active.get(lk) && LOGS.active.get(lk).el === removed)
      LOGS.active.delete(lk);
    if (!LOGS.follow) feed.scrollTop = AstraLog.compensateTrim(before, removedH);
  }
}

// Insert a new row at its chronological position. Normally that is the bottom
// (events arrive in order), but an out-of-order event — SSE replay, buffering,
// a reconnect, or a slow async operation — must land where its backend
// timestamp puts it, not where it happened to arrive. Returns true when the
// row went in above the viewport so the caller can keep the reader's place.
function placeRow(feed, rowEl) {
  const m = rowEl._astraModel;
  let ref = null;
  for (let n = feed.lastElementChild; n; n = n.previousElementSibling) {
    const nm = n._astraModel;
    if (!nm) continue;                          // placeholder / non-row node
    if (AstraLog.compareChron(nm, m) <= 0) break;
    ref = n;
  }
  if (ref) feed.insertBefore(rowEl, ref); else feed.appendChild(rowEl);
  if (!ref) return false;
  return rowEl.getBoundingClientRect().top < feed.getBoundingClientRect().top;
}

// Render one arriving event: resolve the row for its operation if it is a
// lifecycle continuation, otherwise append a new row. `quiet` (history load /
// pause flush) skips the per-row scroll bookkeeping so a large batch is one
// layout pass, not hundreds.
function upsertEvent(event, quiet) {
  if (!event || event.id == null) return;
  if (!isImportantEvent(event)) return;
  const key = String(event.id);
  if (LOGS.rendered.has(key)) return;   // SSE replay / poll overlap dedupe
  const feed = $("#live-feed");
  if (!feed) return;

  const m = AstraLog.normalize(event);
  AstraLog.markRendered(LOGS, event);
  const plan = AstraLog.planRender(LOGS, event, m);
  if (feed.firstElementChild &&
      feed.firstElementChild.classList.contains("empty")) feed.innerHTML = "";

  const info = plan.key ? LOGS.active.get(plan.key) : null;
  let rowEl = info && info.el ? info.el : null;

  if (rowEl && plan.action === "update") {
    // resolve the SAME operation: update in place, never a second row
    const oldModel = rowEl._astraModel;
    updateRow(rowEl, m);
    AstraLog.recount(LOGS, oldModel, m);
    renderLogStats();
  } else {
    const action = quiet ? { scrollToBottom: false, showIndicator: false }
                         : AstraLog.onAppend(LOGS, feedMetrics(), 1);
    rowEl = buildRow(m);
    if (plan.key) rowEl.dataset.lifecycleKey = plan.key;
    const insertedAbove = placeRow(feed, rowEl);
    trimBuffer(feed);
    AstraLog.count(LOGS, m);
    renderLogStats();

    if (!quiet) {
      if (action.scrollToBottom) { feed.scrollTop = feed.scrollHeight; jumpToLatest(); }
      else {
        // an out-of-order row inserted above the viewport must not shift what
        // the reader is looking at.
        if (insertedAbove) feed.scrollTop += rowEl.offsetHeight || 0;
        showJump(LOGS.unread);
      }
    }
  }

  // A request/run that just ended resolves any child it left "running".
  plan.closeKeys.forEach((k) => {
    const ci = LOGS.active.get(k);
    if (ci && ci.el) resolveRow(ci.el, AstraLog.INTERRUPTED_REASON);
  });

  AstraLog.commit(LOGS, plan, m);
  const tracked = plan.key ? LOGS.active.get(plan.key) : null;
  if (tracked) tracked.el = rowEl;
}

// Live arrival path: skip noise/dupes, render now, or buffer while paused.
function receiveEvent(event) {
  const verdict = AstraLog.admit(LOGS, event);
  if (verdict === "skip") return;
  if (verdict === "buffer") {
    AstraLog.buffer(LOGS, event);
    return;
  }
  upsertEvent(event);
}

function flushPending() {
  const items = LOGS.pending;
  LOGS.pending = [];
  if (!items.length) return;
  items.forEach((e) => upsertEvent(e, true));
  if (LOGS.follow) jumpToLatest(); else showJump(LOGS.unread);
}

function initLogsToolbar() {
  const bar = $("#logs-filters");
  if (bar && !bar.dataset.hooked) {
    bar.dataset.hooked = "1";
    bar.addEventListener("click", (e) => {
      const chip = e.target.closest(".chip");
      if (!chip) return;
      $$(".chip", bar).forEach((c) => c.classList.toggle("active", c === chip));
      LOGS.filter = chip.dataset.filter;
      applyLogsFilter();
    });
  }
  const search = $("#logs-search");
  if (search && !search.dataset.hooked) {
    search.dataset.hooked = "1";
    search.addEventListener("input", () => {
      LOGS.query = search.value.trim().toLowerCase();
      applyLogsFilter();
    });
  }
  const pauseBtn = $("#btn-logs-pause");
  if (pauseBtn && !pauseBtn.dataset.hooked) {
    pauseBtn.dataset.hooked = "1";
    pauseBtn.addEventListener("click", () => {
      LOGS.paused = !LOGS.paused;
      pauseBtn.textContent = LOGS.paused ? "▶ Resume" : "⏸ Pause";
      pauseBtn.classList.toggle("on", LOGS.paused);
      refreshLiveState();
      // resuming replays everything buffered while paused, in order.
      if (!LOGS.paused) flushPending();
    });
  }
  const clearBtn = $("#btn-logs-clear");
  if (clearBtn && !clearBtn.dataset.hooked) {
    clearBtn.dataset.hooked = "1";
    clearBtn.addEventListener("click", async () => {
      AstraLog.reset(LOGS);
      LOGS.paused = false;
      const pause = $("#btn-logs-pause");
      if (pause) { pause.textContent = "⏸ Pause"; pause.classList.remove("on"); }
      renderLogStats();
      refreshLiveState();
      const feed = $("#live-feed");
      if (feed) feed.innerHTML = `<div class="empty">Cleared — listening…</div>`;
      // Also wipe the persisted history server-side — otherwise a page
      // refresh reloads the same old events right back into the panel.
      try { await del("/api/events"); } catch (_) { /* best-effort */ }
    });
  }
  const copyBtn = $("#btn-logs-copy");
  if (copyBtn && !copyBtn.dataset.hooked) {
    copyBtn.dataset.hooked = "1";
    copyBtn.addEventListener("click", () => copyLogsToClipboard(copyBtn));
  }
  const jumpBtn = $("#logs-jump");
  if (jumpBtn && !jumpBtn.dataset.hooked) {
    jumpBtn.dataset.hooked = "1";
    jumpBtn.addEventListener("click", jumpToLatest);
  }
  // Follow the user's scroll position: reaching the bottom resumes
  // live-follow, scrolling up stops it. Throttled to one rAF per burst.
  const feed = $("#live-feed");
  if (feed && !feed.dataset.scrollHooked) {
    feed.dataset.scrollHooked = "1";
    let ticking = false;
    feed.addEventListener("scroll", () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        ticking = false;
        AstraLog.onScroll(LOGS, feedMetrics());
        if (LOGS.follow) {
          const jump = $("#logs-jump");
          if (jump) jump.hidden = true;
        }
      });
    }, { passive: true });
  }
  renderLogStats();
  refreshLiveState();
}

// Copies the currently-visible (i.e. filter/search-matched) rows as plain
// text, top-to-bottom, in the same chronological order shown on screen. Each
// row includes its FULL details (fields, complete Input and Output) whether or
// not it is expanded on screen — built from the row models, not the DOM text.
async function copyLogsToClipboard(btn) {
  const feed = $("#live-feed");
  const models = $$(".tl-row", feed)
    .filter((el) => !el.classList.contains("hidden"))
    .map((el) => el._astraModel)
    .filter(Boolean);
  const out = AstraLog.rowsToText(models, metaText);
  const flash = (label) => {
    if (!btn) return;
    const prev = btn.dataset.label || btn.textContent;
    btn.dataset.label = prev;
    btn.textContent = label;
    setTimeout(() => { btn.textContent = prev; }, 1400);
  };
  if (!out) { flash("Nothing to copy"); return; }
  try {
    await copyText(out);
    flash("✅ Copied");
  } catch (_) {
    flash("⚠️ Copy failed");
  }
}

function applyLogsFilter() {
  const feed = $("#live-feed");
  if (!feed) return;
  $$(".tl-row", feed).forEach((el) => {
    el.classList.toggle("hidden",
      !AstraLog.matchesRow(el.dataset.cats, el.dataset.text, LOGS.filter, LOGS.query));
  });
}

/* ------------------------------ providers (core) --------------------------- */
// Model-level test results kept client-side so a redraw from /api/providers
// (which only knows aggregate provider health, not "model X took 42ms just
// now") never wipes out what the last Test click just showed.
const PROVIDER_MODEL_RESULTS = {};   // provider name -> [{model, ok, latency_ms, error, pending}]
// Provider name -> that provider's model id list, refreshed on every
// /api/providers render. The per-model test click reads from here instead
// of re-fetching, so it knows exactly which models to fire requests for.
const PROVIDER_MODELS = {};
// Provider name -> its API keys as the server reports them (secret-free:
// {key_id, label:"key 1", healthy, in_cooldown, last_error}). Read by the
// per-key test so it knows which keys to fire every model through.
const PROVIDER_KEYS = {};
const HEALTH_KEY_SELECTION_KEY = "astra_health_test_key_selection";
const HEALTH_KEY_SELECTION = { providers: {}, gateway: {} };
function _loadHealthKeySelection() {
  try {
    const saved = JSON.parse(localStorage.getItem(HEALTH_KEY_SELECTION_KEY) || "{}");
    Object.assign(HEALTH_KEY_SELECTION.providers, saved.providers || {});
    Object.assign(HEALTH_KEY_SELECTION.gateway, saved.gateway || {});
  } catch (_) {}
}
function _saveHealthKeySelection() {
  try { localStorage.setItem(HEALTH_KEY_SELECTION_KEY, JSON.stringify(HEALTH_KEY_SELECTION)); }
  catch (_) {}
}
_loadHealthKeySelection();
// Providers/connections temporarily revealed by their own Test click while
// the main Hide/Show toggle is set to hidden — session-only (never saved to
// localStorage), so a real page refresh drops back to fully hidden per the
// main toggle's persisted state, exactly like it did before this override
// existed. Cleared whenever the main toggle itself is clicked.
const FORCE_SHOWN_PROVIDERS = new Set();
const FORCE_SHOWN_GATEWAY = new Set();

/* ---- Running-test persistence (survives a real page refresh) -------------
 * Test / Test all fire their requests from the browser, so a page refresh
 * used to wipe every "⏳ testing…" row even though the server kept working
 * on (and saving) those calls. Each running test is therefore also written
 * to localStorage (with a snapshot of the last-known saved results). After a
 * refresh, loaders.providers / renderGatewayCard read it back, show the
 * still-running rows as pending (card force-revealed, Test buttons disabled)
 * and poll the server until each row's saved result differs from the
 * snapshot — then paint the real result. Entries older than
 * RUNNING_MAX_AGE_MS are treated as dead and dropped.
 */
const RUNNING_KEY = "astra_running_tests";
const RUNNING_MAX_AGE_MS = 3 * 60 * 1000;
const RUN_SEP = "\u001f";
const LIVE_PROVIDER_TESTS = new Set();      // tests THIS page instance is running
const LIVE_GATEWAY_TESTS = new Set();
const RESTORED_PROVIDER_PENDING = new Set(); // tests restored after a refresh, still running
const RESTORED_GATEWAY_PENDING = new Set();
const LAST_PROVIDER_DATA = {};               // provider name -> last /api/providers entry
const LAST_GATEWAY_DATA = {};                // connection key -> last gateway connection entry
let _runningPollTimer = null;
// A refresh cancels every request the browser had queued but not yet sent
// (Chrome allows only ~6 parallel connections per host), so those never reach
// the server and would stay "pending" forever. After this grace period — long
// enough for requests already in flight at the server to land — the restored
// page re-fires whatever is still pending (see _maybeResumeRuns).
const PAGE_LOADED_AT = Date.now();
const RESUME_GRACE_MS = 3500;

function _loadRunning() {
  let st;
  try { st = JSON.parse(localStorage.getItem(RUNNING_KEY) || "{}") || {}; }
  catch (_e) { st = {}; }
  st.providers = st.providers || {};
  st.gateway = st.gateway || {};
  const now = Date.now();
  for (const grp of ["providers", "gateway"]) {
    for (const [k, v] of Object.entries(st[grp])) {
      if (!v || now - (v.startedAt || 0) > RUNNING_MAX_AGE_MS) delete st[grp][k];
    }
  }
  if (st.testAll && now - st.testAll > RUNNING_MAX_AGE_MS) st.testAll = 0;
  return st;
}
function _runningUpdate(fn) {
  const st = _loadRunning();
  fn(st);
  try { localStorage.setItem(RUNNING_KEY, JSON.stringify(st)); }
  catch (_e) { /* storage unavailable — live UI still works, only refresh-restore is lost */ }
}
const _gwSig = (h) => h ? `${h.success_count || 0}/${h.failure_count || 0}` : "0/0";
function _providerRunBase(name) {
  const base = {};
  const kr = (LAST_PROVIDER_DATA[name] || {}).key_results || {};
  Object.entries(kr).forEach(([m, byKey]) =>
    Object.entries(byKey || {}).forEach(([kid, r]) => { base[m + RUN_SEP + kid] = (r && r.tested_at) || ""; }));
  return base;
}
function _gatewayRunBase(key) {
  const base = {};
  Object.entries((LAST_GATEWAY_DATA[key] || {}).model_health || {})
    .forEach(([m, h]) => { base[m] = _gwSig(h); });
  return base;
}
function _runningNoteLocal(name, result) {
  _runningUpdate((st) => {
    const e = st.providers[name];
    if (e) { e.local = e.local || {}; e.local[result.model] = result; }
  });
}
// Rebuild one provider's rows from the server's saved data + a stored run.
// Returns how many rows/chips are still waiting on the server.
function _restoreProviderRun(n, p, run) {
  const models = p.models || [];
  const keys = p.keys || [];
  let pending = 0;
  if (run.mode === "keys" && keys.length) {
    PROVIDER_MODEL_RESULTS[n] = models.map((m) => ({
      model: m,
      keys: keys.map((k) => {
        const r = ((p.key_results || {})[m] || {})[k.key_id];
        const fresh = r && (r.tested_at || "") !== (run.base[m + RUN_SEP + k.key_id] || "");
        if (fresh) return { key_id: k.key_id, label: k.label, ok: r.ok, latency_ms: r.latency_ms,
                            error: r.error, tested_at: r.tested_at };
        pending += 1;
        return { key_id: k.key_id, label: k.label, pending: true };
      }),
    }));
    return pending;
  }
  // Providers without keys: results only exist client-side, so the ones that
  // landed before the refresh were stored in the run; the rest stay pending
  // until the server's call counter (reset at test start) covers every model.
  const local = run.local || {};
  const finished = (p.calls || 0) >= models.length;
  PROVIDER_MODEL_RESULTS[n] = models.map((m) => {
    if (local[m]) return local[m];
    if (finished) return { model: m, untested: true };
    pending += 1;
    return { model: m, pending: true };
  });
  return pending;
}
function _gwRow(modelId, h) {
  const tested = h && ((h.success_count || 0) + (h.failure_count || 0) > 0);
  if (!tested) return { model: modelId, untested: true };
  const lastOk = h.last_success && (!h.last_failure || h.last_success > h.last_failure);
  return lastOk
    ? { model: modelId, ok: true, latency_ms: Math.round(h.average_latency_ms || 0) }
    : { model: modelId, ok: false, error: "last test failed" };
}
// Re-fire whatever a refresh left unfinished (see PAGE_LOADED_AT above). Each
// restored provider/connection becomes a normal live run for just its pending
// rows, so it streams, persists and finishes exactly like a fresh click.
function _maybeResumeRuns() {
  if (Date.now() - PAGE_LOADED_AT < RESUME_GRACE_MS) return;
  const names = [...RESTORED_PROVIDER_PENDING];
  const conns = [...RESTORED_GATEWAY_PENDING];
  if (!names.length && !conns.length) return;
  const jobs = [
    ...names.map((n) => testProviderStreaming(n, null, true)),
    ...conns.map((k) => testGatewayConnectionStreaming(k, null, true)),
  ];
  Promise.allSettled(jobs).then(() => loaders.providers());
}
// Disable/relabel the bulk buttons while a restored run is still going, and
// keep polling the server until it's done. Live (this-page) runs own their
// buttons via dataset.live and are left alone.
function _syncRunningUi() {
  const st = _loadRunning();
  const pendingCount = RESTORED_PROVIDER_PENDING.size + RESTORED_GATEWAY_PENDING.size;
  const liveCount = LIVE_PROVIDER_TESTS.size + LIVE_GATEWAY_TESTS.size;
  const anyLive = liveCount > 0;
  if (pendingCount === 0 && !anyLive && st.testAll) {
    _runningUpdate((s2) => { s2.testAll = 0; });
    st.testAll = 0;
  }
  const setBusy = (btn, busyLabel, busy) => {
    if (!btn || btn.dataset.live) return;
    if (busy) {
      if (!btn.dataset.restored) { btn.dataset.orig = btn.textContent; btn.dataset.restored = "1"; }
      btn.disabled = true;
      btn.textContent = busyLabel;
    } else if (btn.dataset.restored) {
      btn.disabled = false;
      btn.textContent = btn.dataset.orig;
      delete btn.dataset.restored;
    }
  };
  setBusy($("#btn-providers-test-all"), "⏳ Testing all…",
          (pendingCount + liveCount) > 0 && !!st.testAll);
  clearTimeout(_runningPollTimer);
  _runningPollTimer = null;
  if (pendingCount > 0) _runningPollTimer = setTimeout(() => loaders.providers(), 2000);
}

// One API key's result for one model. `k` = {key_id, label, pending?, ok?,
// latency_ms?, error?}; `ok` undefined/null means "never tested".
function keyChipHtml(k) {
  const id = `data-key="${esc(k.key_id)}"`;
  if (k.pending) return `<span class="key-chip pending" ${id}>⏳ ${esc(k.label)}</span>`;
  if (k.waiting) return `<span class="key-chip pending" ${id}>⏳ ${esc(k.label)} · waiting</span>`;
  if (k.ok === undefined || k.ok === null)
    return `<span class="key-chip none" ${id}>${esc(k.label)} · not tested</span>`;
  const when = k.tested_at ? ` title="${esc(k.tested_at)}"` : "";
  if (k.ok) return `<span class="key-chip ok" ${id}${when}>✓ ${esc(k.label)} · ${k.latency_ms}ms</span>`;
  return `<span class="key-chip bad" ${id}${when || ` title="${esc(k.error || "")}"`}>` +
    `❌ ${esc(k.label)} · ${esc(k.error || "error")}</span>`;
}

// Saved (server-side) per-key results -> the same row shape a live test
// produces, so a page reload shows the last known state of every key.
function savedKeyRows(models, keys, keyResults) {
  let any = false;
  const rows = models.map((m) => ({
    model: m,
    keys: keys.map((k) => {
      const r = ((keyResults || {})[m] || {})[k.key_id];
      if (r) any = true;
      return r ? { key_id: k.key_id, label: k.label, ok: r.ok, latency_ms: r.latency_ms,
                   error: r.error, tested_at: r.tested_at }
               : { key_id: k.key_id, label: k.label };
    }),
  }));
  return any ? rows : [];
}

// Saved (server-side) per-model Gateway health -> the same row shape a
// live test produces, so a page reload shows each connection's last
// known state instead of an empty table. Mirrors savedKeyRows above,
// which already does this for provider per-key results — the Gateway
// card was missing this restoration entirely, so it went blank on every
// reload even though the server has the data (routing_state health).
function savedGatewayModelRows(models, modelHealth) {
  let any = false;
  const rows = (models || []).map((modelId) => {
    const h = (modelHealth || {})[modelId];
    const tested = h && ((h.success_count || 0) + (h.failure_count || 0) > 0);
    if (!tested) return { model: modelId, untested: true };
    any = true;
    const lastOk = h.last_success && (!h.last_failure || h.last_success > h.last_failure);
    return lastOk
      ? { model: modelId, ok: true, latency_ms: Math.round(h.average_latency_ms || 0) }
      : { model: modelId, ok: false, error: "last test failed" };
  });
  return any ? rows : [];
}

function _healthKeys(kind, name) {
  return kind === "gateway" ? (GATEWAY_KEYS[name] || []) : (PROVIDER_KEYS[name] || []);
}
function _ensureHealthKey(kind, name) {
  const keys = _healthKeys(kind, name);
  if (!keys.length) return "";
  const map = kind === "gateway" ? HEALTH_KEY_SELECTION.gateway : HEALTH_KEY_SELECTION.providers;
  if (map[name] && keys.some((k) => k.key_id === map[name])) return map[name];
  map[name] = keys[0].key_id;
  _saveHealthKeySelection();
  return map[name];
}
function _selectedHealthKey(kind, name) { return _ensureHealthKey(kind, name); }
function _healthKeyLabel(kind, name) {
  const id = _selectedHealthKey(kind, name);
  const key = _healthKeys(kind, name).find((k) => k.key_id === id);
  return key ? key.label : "Key 1";
}

function _renderHealthKeyPicker() {
  const panel = $("#health-key-selector");
  if (!panel) return;
  const rows = [];
  for (const [name, keys] of Object.entries(PROVIDER_KEYS || {})) {
    if (!keys.length) continue;
    const selected = _selectedHealthKey("provider", name);
    rows.push(`<div class="health-key-row"><b>Provider · ${esc(name)}</b><select data-health-key-kind="provider" data-health-key-name="${esc(name)}">${keys.map(k => `<option value="${esc(k.key_id)}" ${k.key_id === selected ? "selected" : ""}>${esc(k.label)}</option>`).join("")}</select></div>`);
  }
  for (const [name, keys] of Object.entries(GATEWAY_KEYS || {})) {
    if (!keys.length) continue;
    const selected = _selectedHealthKey("gateway", name);
    rows.push(`<div class="health-key-row"><b>Gateway · ${esc(GATEWAY_LABELS[name] || name)}</b><select data-health-key-kind="gateway" data-health-key-name="${esc(name)}">${keys.map(k => `<option value="${esc(k.key_id)}" ${k.key_id === selected ? "selected" : ""}>${esc(k.label)}</option>`).join("")}</select></div>`);
  }
  panel.innerHTML = `<div class="health-key-presets"><button class="btn mini" data-health-key-preset="first">First key</button><button class="btn mini" data-health-key-preset="last">Last key</button></div>${rows.join("")}`;
}
function _initHealthKeyPicker() {
  const btn = $("#btn-health-key-selector"), panel = $("#health-key-selector");
  if (!btn || !panel || btn.dataset.hooked) return;
  btn.dataset.hooked = "1";
  btn.onclick = () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) _renderHealthKeyPicker();
  };
  panel.addEventListener("click", (e) => {
    const b = e.target.closest("[data-health-key-preset]");
    if (!b) return;
    const last = b.dataset.healthKeyPreset === "last";
    for (const kind of ["provider", "gateway"]) {
      const src = kind === "provider" ? PROVIDER_KEYS : GATEWAY_KEYS;
      const dst = kind === "provider" ? HEALTH_KEY_SELECTION.providers : HEALTH_KEY_SELECTION.gateway;
      for (const [name, keys] of Object.entries(src || {})) {
        if (keys.length) dst[name] = (last ? keys[keys.length - 1] : keys[0]).key_id;
      }
    }
    _saveHealthKeySelection();
    _renderHealthKeyPicker();
  });
  panel.addEventListener("change", (e) => {
    const sel = e.target.closest("[data-health-key-kind]");
    if (!sel) return;
    const dst = sel.dataset.healthKeyKind === "provider"
      ? HEALTH_KEY_SELECTION.providers : HEALTH_KEY_SELECTION.gateway;
    dst[sel.dataset.healthKeyName] = sel.value;
    _saveHealthKeySelection();
  });
}
function _setAllHealthKeys(position) {
  for (const [name, keys] of Object.entries(PROVIDER_KEYS)) {
    if (keys.length) HEALTH_KEY_SELECTION.providers[name] =
      position === "last" ? keys[keys.length - 1].key_id : keys[0].key_id;
  }
  for (const [name, keys] of Object.entries(GATEWAY_KEYS)) {
    if (keys.length) HEALTH_KEY_SELECTION.gateway[name] =
      position === "last" ? keys[keys.length - 1].key_id : keys[0].key_id;
  }
  _saveHealthKeySelection();
  renderHealthKeySelector();
}
function renderHealthKeySelector() {
  const panel = $("#health-key-selector");
  const button = $("#btn-health-key-selector");
  if (!panel || !button) return;
  const providers = Object.entries(PROVIDER_KEYS).filter(([, keys]) => keys.length);
  const gateways = Object.entries(GATEWAY_KEYS).filter(([, keys]) => keys.length);
  const all = providers.concat(gateways);
  if (!all.length) { button.textContent = "🔑 Test key"; panel.hidden = true; return; }
  const labels = providers.map(([n]) => _healthKeyLabel("provider", n))
    .concat(gateways.map(([n]) => _healthKeyLabel("gateway", n)));
  const same = labels.length > 0 && labels.every((x) => x === labels[0]);
  button.textContent = "🔑 Test key: " + (same ? labels[0] : "Custom");
  const row = (kind, name, keys) => {
    const selected = _selectedHealthKey(kind, name);
    const label = kind === "gateway" ? (GATEWAY_LABELS[name] || name) : name;
    return '<div class="health-key-selector-row"><span>' + esc(label) +
      '</span><select data-health-key-kind="' + kind +
      '" data-health-key-name="' + esc(name) + '">' +
      keys.map((k) => '<option value="' + esc(k.key_id) + '"' +
        (k.key_id === selected ? ' selected' : '') + '>' +
        esc(k.label) + '</option>').join("") +
      '</select></div>';
  };
  panel.innerHTML =
    '<div class="health-key-selector-head"><b>Select test key</b>' +
    '<button type="button" class="btn mini" data-health-key-shortcut="first">1st key</button>' +
    '<button type="button" class="btn mini" data-health-key-shortcut="last">Last key</button>' +
    '<span class="muted">Only the selected key makes the real call; other key rows reuse the result.</span></div>' +
    '<div class="health-key-selector-grid">' +
    providers.map(([n, keys]) => row("provider", n, keys)).join("") +
    gateways.map(([n, keys]) => row("gateway", n, keys)).join("") +
    '</div>';
  if (!panel.dataset.hooked) {
    panel.dataset.hooked = "1";
    panel.addEventListener("change", (ev) => {
      const sel = ev.target.closest("[data-health-key-kind]");
      if (!sel) return;
      const map = sel.dataset.healthKeyKind === "gateway"
        ? HEALTH_KEY_SELECTION.gateway : HEALTH_KEY_SELECTION.providers;
      map[sel.dataset.healthKeyName] = sel.value;
      _saveHealthKeySelection();
      renderHealthKeySelector();
    });
    panel.addEventListener("click", (ev) => {
      const b = ev.target.closest("[data-health-key-shortcut]");
      if (b) _setAllHealthKeys(b.dataset.healthKeyShortcut);
    });
  }
}
if ($("#btn-health-key-selector")) {
  $("#btn-health-key-selector").addEventListener("click", () => {
    const panel = $("#health-key-selector");
    if (!panel) return;
    renderHealthKeySelector();
    panel.hidden = !panel.hidden;
  });
}

function modelHealthRowsHtml(results) {
  if (!results || !results.length) {
    return "";
  }
  return results.map((m) => {
    if (m.untested) {
      return `<div class="model-health-row" data-model-row="${esc(m.model)}">` +
        `<span class="status-dot"></span>` +
        `<span class="model-name">${esc(m.model)}</span>` +
        `<span class="model-latency muted">not tested yet</span></div>`;
    }
    if (m.keys) {
      const anyPending = m.keys.some((k) => k.pending);
      const anyOk = m.keys.some((k) => k.ok === true);
      const anyTested = m.keys.some((k) => k.ok === true || k.ok === false);
      const cls = anyPending ? "pending" : (anyOk ? "ok" : (anyTested ? "bad" : "none"));
      const dot = anyPending ? "warn" : (anyOk ? "ok" : (anyTested ? "bad" : "warn"));
      return `<div class="model-health-row keyed ${cls}" data-model-row="${esc(m.model)}">` +
        `<span class="status-dot ${dot}"></span>` +
        `<span class="model-name">${esc(m.model)}</span>` +
        `<div class="key-results">${m.keys.map(keyChipHtml).join("")}</div></div>`;
    }
    if (m.pending) {
      return `<div class="model-health-row pending" data-model-row="${esc(m.model)}">` +
        `<span class="status-dot warn"></span>` +
        `<span class="model-name">${esc(m.model)}</span>` +
        `<span class="model-latency">⏳ testing…</span></div>`;
    }
    const cls = m.ok ? "ok" : "bad";
    const right = m.ok ? `${m.latency_ms}ms` : `❌ ${esc(m.error || "error")}`;
    return `<div class="model-health-row ${cls}" data-model-row="${esc(m.model)}">` +
      `<span class="status-dot ${m.ok ? "ok" : "bad"}"></span>` +
      `<span class="model-name">${esc(m.model)}</span>` +
      `<span class="model-latency">${right}</span></div>`;
  }).join("");
}

// Shared by every "test" button (single provider, single gateway
// connection, and the combined Test All): seed every model as a pending
// row, fire one request per model in parallel via `testOneFn`, and paint
// each row into `tableEl` the instant that model's own response lands —
// no waiting on the slowest model before the fast ones show up.
// `resultsArray` is mutated in place (it's the same array object held in
// PROVIDER_MODEL_RESULTS / GATEWAY_MODEL_RESULTS) so a later re-render
// from /api/providers still sees each finished result. Returns the
// Promise that resolves once every model has settled (success or error).
function streamModelTests(models, tableEl, resultsArray, testOneFn, onResult, keep) {
  if (keep) {
    // Resume: leave rows that already finished alone, re-mark only `models`.
    models.forEach((m) => {
      const i = resultsArray.findIndex((r) => r.model === m);
      const row = { model: m, pending: true };
      if (i >= 0) resultsArray[i] = row; else resultsArray.push(row);
    });
  } else {
    resultsArray.length = 0;
    models.forEach((m) => resultsArray.push({ model: m, pending: true }));
  }
  if (tableEl) tableEl.innerHTML = modelHealthRowsHtml(resultsArray);

  const updateRow = (result) => {
    const idx = resultsArray.findIndex((r) => r.model === result.model);
    if (idx >= 0) resultsArray[idx] = result; else resultsArray.push(result);
    if (!tableEl) return;
    const rowEl = $(`[data-model-row="${CSS.escape(result.model)}"]`, tableEl);
    const html = modelHealthRowsHtml([result]);
    if (rowEl) rowEl.outerHTML = html; else tableEl.insertAdjacentHTML("beforeend", html);
  };

  const probes = models.map((modelId) =>
    testOneFn(modelId)
      .then((result) => { updateRow(result); if (onResult) onResult(result); })
      .catch((e) => {
        const result = { model: modelId, ok: false, latency_ms: 0, error: String(e) };
        updateRow(result);
        if (onResult) onResult(result);
      })
  );
  // Promise.allSettled (not Promise.all) so one model erroring can never
  // stop the caller from moving on once the rest are done — every
  // individual result was already saved+shown the moment it arrived.
  return Promise.allSettled(probes);
}

// Hide/show toggle state (AI Providers health + Astra AI Gateway model
// tables), persisted so it survives a real browser refresh — a plain
// DOM class survives re-renders within the page's lifetime but resets on
// reload since the whole document/JS is torn down and rebuilt.
const MODELS_HIDDEN_KEY = "astra_models_hidden";
function _loadModelsHiddenState() {
  try { return JSON.parse(localStorage.getItem(MODELS_HIDDEN_KEY) || "{}"); }
  catch (_e) { return {}; }
}
function _saveModelsHiddenState(state) {
  try { localStorage.setItem(MODELS_HIDDEN_KEY, JSON.stringify(state)); }
  catch (_e) { /* storage unavailable/full — toggle still works this session */ }
}

loaders.providers = async function () {
  _initHealthKeyPicker();
  const r = await api("/api/providers");
  const list = $("#providers-list");
  // Re-apply the persisted hide/show state on every load — classList
  // survives re-renders within one page session (innerHTML only replaces
  // children), but a real browser refresh tears down the whole DOM/JS, so
  // without this the class (and thus the hidden tables) reset to shown.
  if (_loadModelsHiddenState().providers) list.classList.add("models-hidden");
  if (!r.ok) {
    // A transient failure (a burst of parallel "Test all" requests is
    // exactly when the backend is most likely to hiccup) must never wipe
    // rows that are already on screen — only show the error state if
    // this is genuinely the first load. `list.dataset.hooked` is set the
    // moment a successful render has completed at least once, so it's a
    // reliable "have we shown real data before" flag.
    if (!list.dataset.hooked) {
      list.innerHTML = `<div class="empty">${esc(r.error || "providers unavailable")}</div>`;
    }
    return;
  }
  const provs = (r.data && r.data.providers) ? r.data.providers : r.data;
  RESTORED_PROVIDER_PENDING.clear();
  const rows = Object.entries(provs || {}).map(([n, p]) => {
    const dot = p.healthy ? "ok" : (p.state === "down" ? "bad" : "warn");
    const modelCount = p.models ? p.models.length : 0;
    PROVIDER_MODELS[n] = p.models || [];
    PROVIDER_KEYS[n] = p.keys || [];
    _ensureHealthKey("provider", n);
    LAST_PROVIDER_DATA[n] = p;
    // First paint after a reload: show the last saved per-key results.
    if (!(PROVIDER_MODEL_RESULTS[n] || []).length && (p.keys || []).length) {
      PROVIDER_MODEL_RESULTS[n] = savedKeyRows(p.models || [], p.keys, p.key_results);
    }
    // A test that was still running when the page was refreshed: show its
    // unfinished rows as pending (and reveal the card) until the server has
    // saved their results. Tests running on THIS page keep their live rows.
    RESTORED_PROVIDER_PENDING.delete(n);
    if (!LIVE_PROVIDER_TESTS.has(n)) {
      const run = _loadRunning().providers[n];
      if (run) {
        if (_restoreProviderRun(n, p, run) > 0) {
          RESTORED_PROVIDER_PENDING.add(n);
          FORCE_SHOWN_PROVIDERS.add(n);
        } else {
          _runningUpdate((st) => { delete st.providers[n]; });
        }
      }
    }
    const busy = LIVE_PROVIDER_TESTS.has(n) || RESTORED_PROVIDER_PENDING.has(n);
    const keyCount = (p.keys || []).length;
    const forceShow = FORCE_SHOWN_PROVIDERS.has(n) ? " force-show" : "";
    return `<div class="provider-card${forceShow}" data-provider-row="${esc(n)}">` +
      `<div class="provider-card-head">` +
      `<span class="status-dot ${dot}"></span><b>${esc(n)}</b>` +
      `<span class="grow muted" data-role="provider-counts" ` +
      `data-calls="${p.calls || 0}" data-errors="${p.errors || 0}" ` +
      `data-label="${esc(p.state || (p.healthy ? "healthy" : "?"))}" data-modelcount="${modelCount}" ` +
      `data-keycount="${keyCount}">` +
      `${esc(p.state || (p.healthy ? "healthy" : "?"))} · ` +
      `${modelCount} model(s) · ${keyCount} key(s) · ${p.calls || 0} calls · ${p.errors || 0} err</span>` +
      `<button class="btn mini" data-role="provider-test" data-provider="${esc(n)}"${busy ? " disabled" : ""}>` +
      (busy ? "⏳ Testing…"
            : `🧪 Test (${modelCount || 0} model${modelCount === 1 ? "" : "s"} · ${keyCount ? _healthKeyLabel("provider", n) : "direct"})`) +
      `</button>` +
      `</div>` +
      `<div class="model-health-table" data-role="model-table">` +
      modelHealthRowsHtml(PROVIDER_MODEL_RESULTS[n]) +
      `</div></div>`;
  }).join("");
  list.innerHTML = rows || `<div class="empty">kono provider e creds nai (offline mode)</div>`;
  // Hide/show toggle for every provider's per-model rows (gemini-3.7-flash,
  // key chips, etc.) — one button, same click alternates hide <-> show.
  // The state lives as a class on #providers-list itself (not on the rows
  // just rebuilt above), so it survives every re-render: list.innerHTML
  // replaces the children each time, never this element's own classList.
  const toggleBtn = $("#btn-providers-toggle-models");
  if (toggleBtn && !toggleBtn.dataset.hooked) {
    toggleBtn.dataset.hooked = "1";
    toggleBtn.onclick = () => {
      const hidden = list.classList.toggle("models-hidden");
      toggleBtn.textContent = hidden ? "👁 Show models" : "🙈 Hide models";
      const st = _loadModelsHiddenState();
      st.providers = hidden;
      _saveModelsHiddenState(st);
      // Flipping the main toggle either way makes any per-provider
      // "revealed by testing it" override moot — clear it so it doesn't
      // linger and confuse the next hide.
      FORCE_SHOWN_PROVIDERS.clear();
    };
  }
  if (toggleBtn) {
    toggleBtn.textContent = list.classList.contains("models-hidden")
      ? "👁 Show models" : "🙈 Hide models";
  }
  // per-provider manual test: click fires ONE request PER MODEL, all in
  // parallel — each model's row flips from "testing…" to its result (and
  // is saved server-side, see test_provider_model) the instant that one
  // model's own response comes back, independent of every other model.
  // Nothing here waits for the slowest model before showing the fast ones.
  if (!list.dataset.hooked) {
    list.dataset.hooked = "1";
    list.addEventListener("click", async (ev) => {
      const b = ev.target.closest('[data-role="provider-test"]');
      if (!b) return;
      const name = b.dataset.provider;
      // Testing one specific provider reveals just that provider's card,
      // even while the main toggle is hidden — everything else stays
      // hidden. Session-only (see FORCE_SHOWN_PROVIDERS above): a real
      // page refresh drops back to fully hidden.
      if (list.classList.contains("models-hidden")) {
        FORCE_SHOWN_PROVIDERS.add(name);
        // Reveal immediately too, not just on the resync at the end —
        // the Set alone only takes effect the next time rows are rebuilt.
        const cardEl = $(`[data-provider-row="${CSS.escape(name)}"]`);
        if (cardEl) cardEl.classList.add("force-show");
      }
      b.disabled = true;
      const prevLabel = b.textContent;
      await testProviderStreaming(name, b);
      b.disabled = false;
      b.textContent = prevLabel;
      // final resync with the server's own aggregate numbers (covers any
      // other counters — latency, credential health — the header doesn't
      // track locally), without touching the per-model rows already painted.
      loaders.providers();
    });
  }
  const testAllBtn = $("#btn-providers-test-all");
  if (testAllBtn && !testAllBtn.dataset.hooked) {
    testAllBtn.dataset.hooked = "1";
    testAllBtn.onclick = async () => {
      testAllBtn.disabled = true;
      testAllBtn.dataset.live = "1";
      _runningUpdate((st) => { st.testAll = Date.now(); });
      const prevLabel = testAllBtn.dataset.restored ? testAllBtn.dataset.orig : testAllBtn.textContent;
      // Same streaming UI as a single provider/connection Test click, just
      // fired for every provider AND every Gateway connection at once, all
      // in parallel — every model's row (and every provider's live call/
      // err count) updates the instant that one model's own result lands,
      // nothing here waits for the whole run to finish.
      const providerNames = Object.keys(PROVIDER_MODELS);
      const connectionKeys = Object.keys(GATEWAY_MODELS);
      const total = providerNames.length + connectionKeys.length;
      let done = 0;
      testAllBtn.textContent = `⏳ Testing all (0/${total})…`;
      const bumpProgress = () => {
        done += 1;
        testAllBtn.textContent = `⏳ Testing all (${done}/${total})…`;
      };
      try {
        // Reset previous saved/shared health once for this whole run.
        // Individual workers deliberately skip reset so Provider↔Gateway
        // probes can share the same in-flight/fresh result.
        await post("/api/v1/providers/reset-all-health");
        BULK_HEALTH_RUN.active = true;
        BULK_HEALTH_RUN.providerModels.clear();
        BULK_HEALTH_RUN.deferredGatewayResults.clear();
        try {
          await Promise.allSettled([
            ...providerNames.map((name) =>
              testProviderStreaming(name, undefined, false, true).then(bumpProgress)),
            ...connectionKeys.map((key) =>
              testGatewayConnectionStreaming(key, undefined, false, true).then(bumpProgress)),
          ]);
        } finally {
          BULK_HEALTH_RUN.active = false;
          BULK_HEALTH_RUN.providerModels.clear();
          BULK_HEALTH_RUN.deferredGatewayResults.clear();
        }
      } finally {
        delete testAllBtn.dataset.live;
        _runningUpdate((st) => { st.testAll = 0; });
        testAllBtn.disabled = false;
        testAllBtn.textContent = prevLabel;
        loaders.providers();
      }
    };
  }
  renderGatewayCard(r.ok ? (r.data.astra_ai_gateway || null) : null);
  renderHealthKeySelector();
  _maybeResumeRuns();
  _syncRunningUi();
};

// Streams a single provider's every model test, live — the same routine
// the single "🧪 Test" button uses, factored out so Test All can run it
// for every provider in parallel without duplicating the logic. `btn`
// (optional) gets its label updated while this provider's own test runs;
// omit it when called as part of a bulk Test All (the bulk button owns
// its own progress label instead).
async function testProviderStreaming(name, btn, resume, bulk = false) {
  const models = PROVIDER_MODELS[name] || [];
  if (models.length && resume) {
    LIVE_PROVIDER_TESTS.add(name);
    RESTORED_PROVIDER_PENDING.delete(name);
    _runningUpdate((st) => { if (st.providers[name]) st.providers[name].startedAt = Date.now(); });
  } else if (models.length) {
    // Persist "this provider is testing" so a page refresh can bring the
    // pending rows back (see the running-test persistence block above).
    LIVE_PROVIDER_TESTS.add(name);
    RESTORED_PROVIDER_PENDING.delete(name);
    const entry = { startedAt: Date.now(), mode: (PROVIDER_KEYS[name] || []).length ? "keys" : "models",
                    base: _providerRunBase(name), local: {} };
    _runningUpdate((st) => { st.providers[name] = entry; });
  }
  try {
    await _testProviderStreamingInner(name, btn, resume, bulk);
  } finally {
    if (LIVE_PROVIDER_TESTS.delete(name)) _runningUpdate((st) => { delete st.providers[name]; });
  }
}

async function _testProviderStreamingInner(name, btn, resume, bulk = false) {
  const models = PROVIDER_MODELS[name] || [];
  const tableEl = $(`[data-provider-row="${CSS.escape(name)}"] [data-role="model-table"]`);
  if (!models.length) {
    if (tableEl) tableEl.innerHTML = `<div class="model-health-empty">no model configured</div>`;
    return;
  }
  if (btn) btn.textContent = `⏳ Testing ${models.length} model${models.length === 1 ? "" : "s"}…`;

  // Wipe THIS provider's previously saved calls/errors/latency before the
  // fresh run starts — a test's numbers should be this test's own result,
  // not the old save with new numbers piled on top. Scoped to just this
  // one provider; every other provider's saved health is left untouched.
  const countsEl = $(`[data-provider-row="${CSS.escape(name)}"] [data-role="provider-counts"]`);
  const renderCounts = (calls, errors) => {
    if (!countsEl) return;
    countsEl.dataset.calls = String(calls);
    countsEl.dataset.errors = String(errors);
    countsEl.textContent = `${countsEl.dataset.label} · ${countsEl.dataset.modelcount} model(s) · ` +
      `${countsEl.dataset.keycount || 0} key(s) · ${calls} calls · ${errors} err`;
  };
  if (!resume && !bulk) {
    try {
      const reset = await post(`/api/v1/providers/${encodeURIComponent(name)}/reset-health`);
      renderCounts((reset.ok && reset.data && reset.data.calls) || 0,
                   (reset.ok && reset.data && reset.data.errors) || 0);
    } catch (_e) { /* reset failing shouldn't block the test itself */ }
  }

  // "20 calls · 3 err" in the header — bumped by +1 call (and +1 err on
  // failure) the instant EACH model's own result lands, on top of the
  // zeroed count the reset above just set, instead of waiting for the
  // whole test batch to finish before the number moves.
  const bumpCounts = (ok) => {
    if (!countsEl) return;
    renderCounts((parseInt(countsEl.dataset.calls, 10) || 0) + 1,
                 (parseInt(countsEl.dataset.errors, 10) || 0) + (ok ? 0 : 1));
  };

  // Provider has API keys the server told us about: test EVERY model through
  // EVERY key (one request per model x key, all in parallel). Each key's chip
  // flips from "testing" to its result the moment that one call returns, and
  // the server saves it against (provider, key, model).
  const keys = PROVIDER_KEYS[name] || [];
  if (keys.length) {
    const selectedKey = _selectedHealthKey("provider", name);
    await testProviderSelectedKeyStreaming(
      name, models, keys, selectedKey, tableEl,
      (r) => bumpCounts(r.ok),
      resume ? PROVIDER_MODEL_RESULTS[name] : null);
    return;
  }

  PROVIDER_MODEL_RESULTS[name] = PROVIDER_MODEL_RESULTS[name] || [];
  const toRun = resume
    ? PROVIDER_MODEL_RESULTS[name].filter((r) => r.pending).map((r) => r.model)
    : models;
  await streamModelTests(toRun, tableEl, PROVIDER_MODEL_RESULTS[name],
    (modelId) => post(`/api/v1/providers/${encodeURIComponent(name)}/test/${encodeURIComponent(modelId)}`)
      .then((res) => (res.ok && res.data) ? res.data :
        { model: modelId, ok: false, latency_ms: 0, error: res.error || "test failed" }),
    (result) => { bumpCounts(result.ok); _runningNoteLocal(name, result); }, !!resume);
}

async function testProviderSelectedKeyStreaming(name, models, keys, selectedKey, tableEl, onResult, resumeRows) {
  const chosen = keys.some((k) => k.key_id === selectedKey) ? selectedKey : keys[0].key_id;
  const rows = resumeRows || models.map((m) => ({
    model: m,
    keys: keys.map((k) => ({
      key_id: k.key_id,
      label: k.label,
      pending: k.key_id === chosen,
      waiting: k.key_id !== chosen,
    })),
  }));
  PROVIDER_MODEL_RESULTS[name] = rows;
  if (tableEl) tableEl.innerHTML = modelHealthRowsHtml(rows);
  const repaint = (row) => {
    if (!tableEl) return;
    const el = `[data-model-row="${CSS.escape(row.model)}"]`;
    const node = $(el, tableEl);
    if (node) node.outerHTML = modelHealthRowsHtml([row]);
  };
  const pendingModels = rows.filter((row) =>
    row.keys.some((slot) => slot.pending)).map((row) => row.model);
  const probes = pendingModels.map((modelId) =>
    post(`/api/v1/providers/${encodeURIComponent(name)}/test/${encodeURIComponent(modelId)}?key=${encodeURIComponent(chosen)}`)
      .then((res) => (res.ok && res.data) ? res.data
        : { model: modelId, ok: false, latency_ms: 0, error: res.error || "test failed" })
      .catch((e) => ({ model: modelId, ok: false, latency_ms: 0, error: String(e) }))
      .then((result) => {
        const row = rows.find((r) => r.model === modelId);
        if (!row) return;
        if (bulk && _bulkGatewayShouldWait(key, modelId)) {
          BULK_HEALTH_RUN.deferredGatewayResults.set(key, {
            token: _bulkModelToken(_bulkProviderNameForGateway(key), modelId), result
          });
          return;
        }
        row.keys.forEach((slot) => Object.assign(slot, {
          pending: false,
          waiting: false,
          ok: !!result.ok,
          latency_ms: result.latency_ms,
          error: result.error,
          tested_at: new Date().toLocaleString(),
          selected: slot.key_id === chosen,
          shared: slot.key_id !== chosen
        }));
        repaint(row);
        if (BULK_HEALTH_RUN.active) {
          BULK_HEALTH_RUN.providerModels.add(_bulkModelToken(name, modelId));
          _flushBulkGatewayResult(name, modelId, result);
        }
        if (onResult) onResult(result);
      })
  );
  return Promise.allSettled(probes);
}

/* -------------------------- Astra AI Gateway ------------------------- */
const GATEWAY_LABELS = {
  "astra-gw-gemini": "Gemini",
  "astra-gw-groq": "Groq",
  "astra-gw-cloudflare": "Cloudflare",
  "astra-gw-bedrock": "Bedrock",
  "astra-gw-openrouter": "OpenRouter",
  "astra-gw-mistral": "Mistral",
  "astra-gw-cerebras": "Cerebras",
  "astra-gw-sambanova": "SambaNova",
  "astra-gw-cohere": "Cohere",
  "astra-gw-zai": "Z.AI",
};
// Model-level Gateway test results, kept client-side for the same reason
// as PROVIDER_MODEL_RESULTS above.
const GATEWAY_MODEL_RESULTS = {};   // connection name -> [{model, ok, latency_ms, error, pending}]
// Connection key -> its model id list, refreshed on every render — same
// role as PROVIDER_MODELS, read by the per-model test instead of
// re-fetching.
const GATEWAY_MODELS = {};
const GATEWAY_KEYS = {};

// Bulk Test All keeps matching Gateway rows in Waiting until the Provider
// result for the same provider+model has been saved. The Gateway probe
// may run in parallel and reuse the shared result, but its UI waits.
const BULK_HEALTH_RUN = { active: false, providerModels: new Set(), deferredGatewayResults: new Map() };
function _bulkProviderNameForGateway(key) { return String(key || "").replace(/^astra-gw-/, ""); }
function _bulkModelToken(provider, model) { return provider + "\0" + model; }
function _bulkGatewayShouldWait(key, modelId) {
  if (!BULK_HEALTH_RUN.active) return false;
  const provider = _bulkProviderNameForGateway(key);
  return Array.isArray(PROVIDER_MODELS[provider]) &&
    PROVIDER_MODELS[provider].includes(modelId) &&
    !BULK_HEALTH_RUN.providerModels.has(_bulkModelToken(provider, modelId));
}
function _applyGatewayBulkResult(key, modelId, result) {
  const rows = GATEWAY_MODEL_RESULTS[key] || [];
  const row = rows.find((r) => r.model === modelId);
  if (!row) return;
  row.keys.forEach((slot) => Object.assign(slot, {
    pending: false, waiting: false, ok: !!result.ok,
    latency_ms: result.latency_ms, error: result.error,
    tested_at: new Date().toLocaleString(), shared: true
  }));
  const tableEl = $(`[data-gw-conn="${CSS.escape(key)}"] [data-role="gw-model-table"]`);
  if (tableEl) {
    const node = $(`[data-model-row="${CSS.escape(modelId)}"]`, tableEl);
    if (node) node.outerHTML = modelHealthRowsHtml([row]);
  }
}
function _flushBulkGatewayResult(provider, modelId, result) {
  const token = _bulkModelToken(provider, modelId);
  for (const [key, deferred] of BULK_HEALTH_RUN.deferredGatewayResults) {
    if (deferred.token !== token) continue;
    _applyGatewayBulkResult(key, modelId, result);
    BULK_HEALTH_RUN.deferredGatewayResults.delete(key);
  }
}

// Best-average-latency across a connection's tracked models — used purely
// to *display* connections fastest/healthiest first, mirroring how the
// Gateway itself prefers a healthy, low-latency target when it fallbacks.
function _connAvgLatency(c) {
  const rows = Object.values(c.model_health || {});
  if (!rows.length) return null;
  const withLatency = rows.filter((h) => h.average_latency_ms || h.avg_latency_ms);
  if (!withLatency.length) return null;
  const vals = withLatency.map((h) => h.average_latency_ms ?? h.avg_latency_ms);
  return vals.reduce((a, b) => a + b, 0) / vals.length;
}

// Streams one Gateway connection's every model test, live — the exact
// same per-model, parallel, no-waiting behaviour testProviderStreaming
// uses for providers, so a single connection's "🧪 Test" and the bulk
// Test All look and behave identically. `btn` (optional) gets its label
// updated while this connection's own test runs.
async function testGatewayConnectionStreaming(key, btn, resume, bulk = false) {
  const models = GATEWAY_MODELS[key] || [];
  if (models.length && resume) {
    LIVE_GATEWAY_TESTS.add(key);
    RESTORED_GATEWAY_PENDING.delete(key);
    _runningUpdate((st) => { if (st.gateway[key]) st.gateway[key].startedAt = Date.now(); });
  } else if (models.length) {
    LIVE_GATEWAY_TESTS.add(key);
    RESTORED_GATEWAY_PENDING.delete(key);
    const entry = { startedAt: Date.now(), base: _gatewayRunBase(key) };
    _runningUpdate((st) => { st.gateway[key] = entry; });
  }
  try {
    await _testGatewayConnectionStreamingInner(key, btn, resume, bulk);
  } finally {
    if (LIVE_GATEWAY_TESTS.delete(key)) _runningUpdate((st) => { delete st.gateway[key]; });
  }
}

async function _testGatewayConnectionStreamingInner(key, btn, resume, bulk = false) {
  const models = GATEWAY_MODELS[key] || [];
  const tableEl = $(`[data-gw-conn="${CSS.escape(key)}"] [data-role="gw-model-table"]`);
  if (!models.length) {
    if (tableEl) tableEl.innerHTML = `<div class="model-health-empty">no model configured</div>`;
    return;
  }
  if (btn) btn.textContent = `⏳ Testing ${models.length} model${models.length === 1 ? "" : "s"}…`;
  GATEWAY_MODEL_RESULTS[key] = GATEWAY_MODEL_RESULTS[key] || [];
  if (!resume && !bulk) {
    await post(`/api/v1/gateway/${encodeURIComponent(key)}/reset-health`);
  }
  const toRun = resume
    ? GATEWAY_MODEL_RESULTS[key].filter((r) => r.pending).map((r) => r.model)
    : models;
  const keys = GATEWAY_KEYS[key] || [];
  if (!keys.length) {
    await streamModelTests(toRun, tableEl, GATEWAY_MODEL_RESULTS[key],
      (modelId) => post(`/api/v1/gateway/${encodeURIComponent(key)}/test/${encodeURIComponent(modelId)}`)
        .then((res) => (res.ok && res.data) ? res.data :
          { model: modelId, ok: false, latency_ms: 0, error: res.error || "test failed" }),
      undefined, !!resume);
    return;
  }
  await testGatewaySelectedKeyStreaming(
    key, toRun, keys, _selectedHealthKey("gateway", key),
    tableEl, GATEWAY_MODEL_RESULTS[key], !!resume, !!bulk);
}

async function testGatewaySelectedKeyStreaming(key, models, keys, selectedKey, tableEl, resultsArray, resume, bulk = false) {
  const chosen = keys.some((k) => k.key_id === selectedKey) ? selectedKey : keys[0].key_id;
  const existing = Object.fromEntries(resultsArray.map((r) => [r.model, r]));
  const rows = models.map((modelId) => {
    const prior = existing[modelId];
    if (resume && prior && Array.isArray(prior.keys)) return prior;
    const sharedWaiting = bulk && _bulkGatewayShouldWait(key, modelId);
    return {
      model: modelId,
      keys: keys.map((k) => ({
        key_id: k.key_id,
        label: k.label,
        pending: !sharedWaiting && !(resume && prior && prior.pending === false) && k.key_id === chosen,
        waiting: sharedWaiting || (!(resume && prior && prior.pending === false) && k.key_id !== chosen),
        ...(resume && prior && prior.pending === false ? {
          ok: !!prior.ok,
          latency_ms: prior.latency_ms || 0,
          error: prior.error || ""
        } : {})
      })),
    };
  });
  resultsArray.length = 0;
  rows.forEach((r) => resultsArray.push(r));
  if (tableEl) tableEl.innerHTML = modelHealthRowsHtml(resultsArray);
  const repaint = (row) => {
    if (!tableEl) return;
    const node = $(`[data-model-row="${CSS.escape(row.model)}"]`, tableEl);
    if (node) node.outerHTML = modelHealthRowsHtml([row]);
  };
  const probeModels = rows.filter((r) =>
    r.keys.some((k) => k.pending) || (bulk && _bulkGatewayShouldWait(key, r.model)))
    .map((r) => r.model);
  const probes = probeModels.map((modelId) =>
    post(`/api/v1/gateway/${encodeURIComponent(key)}/test/${encodeURIComponent(modelId)}?key=${encodeURIComponent(chosen)}`)
      .then((res) => (res.ok && res.data) ? res.data :
        { model: modelId, ok: false, latency_ms: 0, error: res.error || "test failed" })
      .catch((e) => ({ model: modelId, ok: false, latency_ms: 0, error: String(e) }))
      .then((result) => {
        const row = rows.find((r) => r.model === modelId);
        if (!row) return;
        row.keys.forEach((slot) => Object.assign(slot, {
          pending: false,
          waiting: false,
          ok: !!result.ok,
          latency_ms: result.latency_ms,
          error: result.error,
          tested_at: new Date().toLocaleString(),
          selected: slot.key_id === chosen,
          shared: slot.key_id !== chosen
        }));
        repaint(row);
      })
  );
  return Promise.allSettled(probes);
}

async function runGatewayConnectionTest(key, btn, card) {
  // Testing one specific connection reveals just that connection's card,
  // even while the main toggle is hidden — same override as the provider
  // side. Session-only: cleared on toggle click, never persisted, so a
  // real page refresh drops back to fully hidden.
  if (card && card.classList.contains("models-hidden")) {
    FORCE_SHOWN_GATEWAY.add(key);
    const cardEl = $(`[data-gw-conn="${CSS.escape(key)}"]`);
    if (cardEl) cardEl.classList.add("force-show");
  }
  if (btn) { btn.disabled = true; btn.dataset.prev = btn.textContent; }
  try {
    await testGatewayConnectionStreaming(key, btn);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = btn.dataset.prev; }
    loaders.providers();
  }
}

function renderGatewayCard(core) {
  const card = $("#gateway-card");
  if (!card) return;
  // Same persisted hide/show restore as loaders.providers — must happen
  // before the early "not configured" return too, so the state is already
  // applied by the time the connection rows exist on a later render.
  if (_loadModelsHiddenState().gateway) card.classList.add("models-hidden");
  const conns = Object.entries((core && core.connections) || {});
  RESTORED_GATEWAY_PENDING.clear();
  if (!core || core.state === "not_configured" || conns.length === 0) {
    card.innerHTML = `<div class="row"><span class="status-dot warn"></span>` +
      `<b>Not configured</b></div>` +
      `<div class="hint muted">Set any GW_*_API_KEYS / GW_*_CREDENTIALS to enable ` +
      `the Astra AI Gateway (fallback: Gemini → Groq → Cloudflare → Bedrock).</div>`;
    return;
  }
  const dots = { healthy: "ok", degraded: "warn", not_configured: "warn", unhealthy: "bad" };
  // healthy-and-fastest first, so the card visually matches actual
  // fallback preference — never a fixed Gemini→Groq→Cloudflare→Bedrock order.
  const ranked = conns.slice().sort(([, a], [, b]) => {
    const ah = a.state === "healthy" ? 0 : 1;
    const bh = b.state === "healthy" ? 0 : 1;
    if (ah !== bh) return ah - bh;
    const al = _connAvgLatency(a); const bl = _connAvgLatency(b);
    if (al == null && bl == null) return 0;
    if (al == null) return 1;
    if (bl == null) return -1;
    return al - bl;
  });
  const rows = ranked.map(([key, c]) => {
    const label = GATEWAY_LABELS[key] || key;
    const dot = dots[c.state] || "warn";
    const modelCount = (c.models || []).length;
    const models = modelCount ? `${modelCount} model(s)` : "no models";
    GATEWAY_MODELS[key] = c.models || [];
    GATEWAY_KEYS[key] = c.keys || [];
    _ensureHealthKey("gateway", key);
    // First paint after a reload: show the last saved per-model results
    // (same idea as PROVIDER_MODEL_RESULTS restoration in loaders.providers),
    // instead of leaving this connection's table empty until someone
    // clicks Test again.
    if (!(GATEWAY_MODEL_RESULTS[key] || []).length) {
      GATEWAY_MODEL_RESULTS[key] = savedGatewayModelRows(c.models || [], c.model_health || {});
    }
    LAST_GATEWAY_DATA[key] = c;
    // Test still running when the page was refreshed: models whose saved
    // health hasn't changed since the run started stay "testing…"; the rest
    // show their freshly saved result.
    if (!LIVE_GATEWAY_TESTS.has(key)) {
      const run = _loadRunning().gateway[key];
      if (run) {
        let pending = 0;
        GATEWAY_MODEL_RESULTS[key] = (c.models || []).map((m) => {
          const h = (c.model_health || {})[m];
          if (_gwSig(h) === ((run.base || {})[m] || "0/0")) { pending += 1; return { model: m, pending: true }; }
          return _gwRow(m, h);
        });
        if (pending > 0) {
          RESTORED_GATEWAY_PENDING.add(key);
          FORCE_SHOWN_GATEWAY.add(key);
        } else {
          _runningUpdate((st) => { delete st.gateway[key]; });
        }
      }
    }
    const busy = LIVE_GATEWAY_TESTS.has(key) || RESTORED_GATEWAY_PENDING.has(key);
    const forceShow = FORCE_SHOWN_GATEWAY.has(key) ? " force-show" : "";
    return `<div class="provider-card${forceShow}" data-gw-conn="${esc(key)}">` +
      `<div class="provider-card-head">` +
      `<span class="status-dot ${dot}"></span><b>${esc(label)}</b>` +
      `<span class="grow muted">${esc(c.state)} · ${models}</span>` +
      `<button class="btn mini" data-role="gw-test" data-conn="${esc(key)}"${busy ? " disabled" : ""}>` +
      (busy ? "⏳ Testing…" : `🧪 Test (${modelCount || 0} model${modelCount === 1 ? "" : "s"} · ${(c.keys || []).length ? _healthKeyLabel("gateway", key) : "direct"})`) +
      `</button>` +
      `</div>` +
      `<div class="model-health-table" data-role="gw-model-table">` +
      modelHealthRowsHtml(GATEWAY_MODEL_RESULTS[key]) +
      `</div></div>`;
  }).join("");
  card.innerHTML = `<div class="list" style="margin-top:8px">${rows}</div>`;

  // per-connection Test button — tests every model of just that ONE
  // connection (e.g. only Gemini's models), not the whole gateway.
  if (!card.dataset.hooked) {
    card.dataset.hooked = "1";
    card.addEventListener("click", (ev) => {
      const b = ev.target.closest('[data-role="gw-test"]');
      if (!b) return;
      runGatewayConnectionTest(b.dataset.conn, b, card);
    });
  }

  // Hide/show toggle for every connection's per-model rows — same pattern
  // as #btn-providers-toggle-models: state lives as a class on #gateway-card
  // itself, so it survives this function's own re-renders.
  const gwToggleBtn = $("#btn-gateway-toggle-models");
  if (gwToggleBtn && !gwToggleBtn.dataset.hooked) {
    gwToggleBtn.dataset.hooked = "1";
    gwToggleBtn.onclick = () => {
      const hidden = card.classList.toggle("models-hidden");
      gwToggleBtn.textContent = hidden ? "👁 Show models" : "🙈 Hide models";
      const st = _loadModelsHiddenState();
      st.gateway = hidden;
      _saveModelsHiddenState(st);
      FORCE_SHOWN_GATEWAY.clear();
    };
  }
  if (gwToggleBtn) {
    gwToggleBtn.textContent = card.classList.contains("models-hidden")
      ? "👁 Show models" : "🙈 Hide models";
  }
}

/* -------------------------------- router (core) ---------------------------- */
loaders.router = async function () {
  const [m, sr] = await Promise.all([api("/api/v1/models"), api("/api/v1/router/stats")]);
  const list = $("#router-list");
  const blocks = [];
  if (m.ok) {
    const models = m.data.models || [];
    blocks.push(`<div class="panel"><div class="panel-head"><h3>Model registry</h3></div><div class="table">` +
      (models.length ? models.map((md) =>
        `<div class="row"><b>${esc(md.display_name || md.model_id)}</b>` +
        `<span>${esc(md.provider)}</span>` +
        `<span>${esc((md.capabilities || []).slice(0, 5).join("・"))}</span>` +
        `<span>${md.preferred ? "★" : ""}</span></div>`).join("")
        : `<span class="muted">no models — provider API key add korle ekhane asbe</span>`) +
      `</div></div>`);
  }
  if (sr.ok) {
    const task = sr.data.task || {};
    const rows = Object.entries(task).map(([k, v]) =>
      `<div class="row"><b>${esc(k)}</b><span>${esc(String(v))}</span></div>`).join("");
    blocks.push(`<div class="panel"><div class="panel-head"><h3>Task routing stats</h3></div>` +
      `<div class="table">${rows || `<span class="muted">routing kora ekhono bondho — kotha bolo age</span>`}</div></div>`);
  }
  list.innerHTML = blocks.join("");
};

/* ------------------------------ wallet / web3 (core) ----------------------- */
loaders.web3 = async function () {
  const [pol, txs] = await Promise.all([
    api("/api/v1/web3/transaction-policy"), api("/api/v1/web3/transactions")]);
  const lim = (n) => n == null ? "—" : (Number(n) / 1e18).toFixed(4) + " ETH";
  if (pol.ok) {
    const d = pol.data, p = d.policy || {};
    const stopped = d.stopped ? "🚨 EMERGENCY STOP" : "running";
    $("#web3-policy").innerHTML =
      `<div class="table">` +
      `<div class="row"><b>Mode</b><span>${esc(d.mode)} (CONFIRM = review, AUTO = policy-approved)</span></div>` +
      `<div class="row"><b>Status</b><span>${esc(stopped)}</span></div>` +
      `<div class="row"><b>Max per tx</b><span>${esc(lim(p.tx_limit_wei))}</span></div>` +
      `<div class="row"><b>Max daily</b><span>${esc(lim(p.daily_limit_wei))}</span></div>` +
      `<div class="row"><b>Allowlist</b><span>${p.recipients_allowed?.length || 0} recipients · ${p.contracts_allowed?.length || 0} contracts · ${p.wallets_allowed?.length || 0} wallets</span></div>` +
      `</div>`;
  } else {
    $("#web3-policy").innerHTML = `<span class="muted">Web3 unavailable</span>`;
  }
  if (txs.ok) {
    const rows = (txs.data.transactions || []).map((t) =>
      `<div class="row"><b>${esc(String(t.tx_id || t.tx_hash || "").slice(0, 12))}…</b>` +
      `<span>${esc(t.status || "?")}</span>` +
      `<span>${esc(lim(t.value_wei))}</span></div>`).join("");
    $("#web3-txs").innerHTML = rows || `<span class="muted">kono transaction nei</span>`;
  }
};

async function eventsPoll() {
  // Polling fallback for browsers without EventSource. Id-based dedupe in
  // receiveEvent() makes a repeated window harmless, so re-offer the tail.
  const r = await api("/api/events?limit=50");
  if (!r.ok) return;
  AstraLog.orderHistory(r.data || []).forEach((e) => {
    receiveEvent(e);
    chatStatusTrack(e);
  });
}

// SSE connection state for the header badge (● LIVE / ○ RECONNECTING).
// Initialized at script load, before the Logs tab is ever opened.
let SSE_STATE = "reconnecting";

// ONE shared live feed for the whole app. The Activity Log and the Assistant
// chat both consume the same /api/events/stream (the chat must not depend on
// the user having opened the Logs tab first, and the server must not get a
// second connection). Idempotent: the first caller sets the resume point,
// later callers reuse the connection — id-based de-dupe in receiveEvent() /
// AstraChatStatus.apply() keeps a replay harmless.
let EVENT_SOURCE = null;

function ensureEventStream(afterId) {
  if (!window.EventSource) return false;
  if (EVENT_SOURCE) return true;
  const url = afterId ? `/api/events/stream?after_id=${encodeURIComponent(afterId)}`
                       : "/api/events/stream";
  const es = new EventSource(url);
  EVENT_SOURCE = es;
  es.onopen = () => { SSE_STATE = "live"; refreshLiveState(); };
  es.onmessage = (ev) => {
    let e = {};
    try { e = JSON.parse(ev.data); } catch (_) { return; }
    receiveEvent(e);
    chatStatusTrack(e);        // the same event, as a human-readable line
  };
  es.onerror = () => {
    // EventSource auto-reconnects, resuming via Last-Event-ID — reflect that
    // in the header instead of looking dead.
    SSE_STATE = "reconnecting";
    refreshLiveState();
  };
  return true;
}

function openSse(afterId) { return ensureEventStream(afterId); }

/* ------------------------------ attachments (multimodal) --------------------- */
const _pendingAttachments = [];
let _attachIdCounter = 0;

function _fileIcon(name) {
  const ext = (name || "").split(".").pop().toLowerCase();
  const map = {
    pdf: "📄", doc: "📄", docx: "📄", txt: "📝", md: "📝",
    csv: "📊", tsv: "📊", xls: "📊", xlsx: "📊",
    ppt: "📽️", pptx: "📽️",
    json: "🔧", xml: "🔧", yaml: "🔧", toml: "🔧",
    png: "🖼️", jpg: "🖼️", jpeg: "🖼️", webp: "🖼️", gif: "🖼️",
    mp3: "🎵", wav: "🎵", m4a: "🎵", aac: "🎵", ogg: "🎵", flac: "🎵", opus: "🎵",
    mp4: "🎬", webm: "🎬", mov: "🎬", avi: "🎬", mkv: "🎬", m4v: "🎬",
    zip: "📦", tar: "📦", tgz: "📦", gz: "📦",
  };
  return map[ext] || "📎";
}

function _humanSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1048576).toFixed(1) + " MB";
}

function attachFiles(files) {
  for (const f of files) {
    _pendingAttachments.push({ id: ++_attachIdCounter, file: f });
  }
  renderAttachmentPreviews();
  updateSendBtnState();
}

function removeAttachment(id) {
  const idx = _pendingAttachments.findIndex((a) => a.id === id);
  if (idx >= 0) _pendingAttachments.splice(idx, 1);
  renderAttachmentPreviews();
  updateSendBtnState();
}

function clearAttachments() {
  _pendingAttachments.length = 0;
  renderAttachmentPreviews();
}

function updateSendBtnState() {
  const input = $("#chat-input");
  const sendBtn = $("#chat-send");
  if (sendBtn) sendBtn.disabled = !input.value.trim() && !_pendingAttachments.length;
}

function renderAttachmentPreviews() {
  const bar = $("#attachments-bar");
  if (!bar) return;
  if (!_pendingAttachments.length) { bar.hidden = true; bar.innerHTML = ""; return; }
  bar.hidden = false;
  bar.innerHTML = _pendingAttachments.map((a) => {
    const f = a.file;
    const icon = _fileIcon(f.name);
    let preview = "";
    if (f.type.startsWith("image/")) {
      // NOTE: blob: URLs require CSP img-src to include blob: (updated in web.py)
      const url = URL.createObjectURL(f);
      preview = `<img src="${url}" alt="${esc(f.name)}" onload="URL.revokeObjectURL(this.src)">`;
    } else if (f.type.startsWith("audio/")) {
      preview = `<span class="attachment-type-icon">🎵</span>`;
    } else if (f.type.startsWith("video/")) {
      preview = `<span class="attachment-type-icon">🎬</span>`;
    } else {
      preview = `<span class="attachment-type-icon">${icon}</span>`;
    }
    return `<div class="attachment-preview" data-id="${a.id}">
      ${preview}
      <div class="attachment-info">
        <span class="attachment-name">${esc(f.name)}</span>
        <span class="attachment-size">${_humanSize(f.size)}</span>
      </div>
      <button type="button" class="attachment-remove" data-id="${a.id}" aria-label="Remove">✕</button>
    </div>`;
  }).join("");
}

// wire attach button + file input
document.addEventListener("click", (e) => {
  if (e.target.closest("#chat-attach")) {
    $("#chat-file-input").click();
  }
  const rmBtn = e.target.closest(".attachment-remove");
  if (rmBtn) removeAttachment(Number(rmBtn.dataset.id));
});
const _fileInput = $("#chat-file-input");
if (_fileInput) _fileInput.addEventListener("change", (e) => {
  if (e.target.files.length) attachFiles(e.target.files);
  e.target.value = "";
});

// drag-and-drop on chat area
const _chatShell = $(".chat-shell");
if (_chatShell) {
  _chatShell.addEventListener("dragover", (e) => { e.preventDefault(); _chatShell.classList.add("drag-over"); });
  _chatShell.addEventListener("dragleave", (e) => { if (!_chatShell.contains(e.relatedTarget)) _chatShell.classList.remove("drag-over"); });
  _chatShell.addEventListener("drop", (e) => {
    e.preventDefault(); _chatShell.classList.remove("drag-over");
    if (e.dataTransfer.files.length) attachFiles(e.dataTransfer.files);
  });
}

/* ------------------------------ artifact display ----------------------------- */
function renderArtifact(a) {
  const el = document.createElement("div");
  el.className = "artifact-card";
  const url = `/api/v1/artifacts/${encodeURIComponent(a.id)}/${encodeURIComponent(a.filename)}`;
  const type = (a.artifact_type || a.type || a.mime_type || a.mime || "").split("/")[0];
  if (type === "image") {
    el.innerHTML = `<img class="artifact-image" src="${esc(url)}" alt="${esc(a.filename)}">
      <div class="artifact-label">${_fileIcon(a.filename)} ${esc(a.filename)}</div>
      <div class="artifact-actions">
        <a class="btn mini" href="${esc(url)}" target="_blank" rel="noopener">Open</a>
        <a class="btn mini" href="${esc(url)}" download="${esc(a.filename)}">Download</a>
      </div>`;
  } else if (type === "audio") {
    el.innerHTML = `<audio class="artifact-audio" controls src="${esc(url)}"></audio>
      <div class="artifact-label">${_fileIcon(a.filename)} ${esc(a.filename)}</div>`;
  } else if (type === "video") {
    el.innerHTML = `<video class="artifact-video" controls src="${esc(url)}"></video>
      <div class="artifact-label">${_fileIcon(a.filename)} ${esc(a.filename)}</div>`;
  } else {
    el.className = "artifact-card artifact-file";
    el.innerHTML = `<span class="artifact-file-icon">${_fileIcon(a.filename)}</span>
      <div class="artifact-file-info">
        <span class="artifact-file-name">${esc(a.filename)}</span>
        ${a.size ? `<span class="artifact-file-size">${_humanSize(a.size)}</span>` : ""}
      </div>
      <a class="btn mini" href="${esc(url)}" download="${esc(a.filename)}">Download</a>`;
  }
  return el;
}

/* ------------------------------ upload indicator ----------------------------- */
function showUploadIndicator(hasFiles) {
  if (!hasFiles) return;
  let el = $("#upload-indicator");
  if (!el) {
    el = document.createElement("div");
    el.id = "upload-indicator";
    el.className = "upload-indicator";
    el.innerHTML = `<span class="upload-spinner"></span> Uploading & processing…`;
    $(".chat-shell")?.insertBefore(el, $("#chat-form")?.nextSibling || null);
  }
  el.hidden = false;
}
function hideUploadIndicator() {
  const el = $("#upload-indicator");
  if (el) el.hidden = true;
}

/* ---------------------------------- boot ------------------------------------ */
/* Resolves once every <script> in index.html has executed. terminal.js and
 * workflow.js register their loaders when they run; if showTab() fired before
 * that (fast /api/manifest on a refresh) the saved tab opened with no loader
 * and the page stayed blank. */
function pageScriptsReady() {
  if (document.readyState === "complete") return Promise.resolve();
  return new Promise((res) => window.addEventListener("load", res, { once: true }));
}

function showBootError(msg) {
  const dash = $("#dash-blocks") || $("#view");
  if (!dash) return;
  dash.innerHTML = `<div class="empty" style="padding:24px;text-align:center">
    <div style="margin-bottom:10px">⚠ Astra could not finish loading: ${esc(msg)}</div>
    <button class="btn" id="boot-retry">Retry</button></div>`;
  const b = $("#boot-retry");
  if (b) b.addEventListener("click", () => { boot(); });
}

async function boot() {
  let r = null;
  for (let attempt = 0; attempt < 4; attempt++) {     // ride out a busy/restarting server
    r = await api("/api/manifest");
    if (r && r.ok) break;
    await new Promise((res) => setTimeout(res, 400 * (attempt + 1)));
  }
  if (!r || !r.ok) {
    $("#netstatus").textContent = "✗";
    showBootError((r && r.error) || "server not responding");
    return;
  }
  $("#netstatus").textContent = "●";
  MANIFEST = r.data;
  document.title = MANIFEST.name;
  MANIFEST.tabs.forEach((t) => { TAB_LABELS[t.tab] = t.label; });

  // tab bar
  $("#nav").innerHTML = MANIFEST.tabs.map((t) =>
    `<button data-tab="${esc(t.tab)}" class="tab${t.tab === "dashboard" ? " active" : ""}">${t.label}</button>`).join("");
  $$("#nav .tab").forEach((t) =>
    t.addEventListener("click", () => showTab(t.dataset.tab)));

  // per-plugin tabview + load plugin JS (one failing plugin must not abort boot)
  const scriptLoads = [];
  MANIFEST.tabs.forEach((t) => {
    if (!t.plugin) return;
    let section = $(`#tab-${t.plugin}`);
    if (!section) {
      section = document.createElement("section");
      section.id = `tab-${t.plugin}`;
      section.className = "tabview";
      $("#view").appendChild(section);
    }
    section.innerHTML = `<div class="empty">Loading ${esc(t.title)}…</div>`;
    scriptLoads.push(loadScript(t.js).then(() => {
      const def = Astra.plugins[t.plugin];
      loaders[t.plugin] = async () => {
        if (!def) return;
        if (!Astra.plugins[t.plugin].rendered) {
          Astra.plugins[t.plugin].rendered = true;
          if (def.render) def.render($("#tab-" + t.plugin));
        }
      };
    }).catch(() => {
      section.innerHTML = `<div class="empty">Could not load ${esc(t.title)}. Refresh to retry.</div>`;
    }));
  });
  await Promise.allSettled(scriptLoads);
  await pageScriptsReady();          // terminal.js / workflow.js have registered

  // Reopen whichever tab was active before the last refresh, if it still
  // exists; otherwise fall back to Dashboard. showTab runs the tab's loader,
  // so calling loaders.dashboard() here as well fired two identical
  // /api/dashboard requests on every load.
  let initialTab = "dashboard";
  try {
    const saved = localStorage.getItem("astra:active-tab");
    if (saved && $("#tab-" + saved)) initialTab = saved;
  } catch (_) { /* ignore */ }
  try {
    showTab(initialTab);
  } catch (e) {                      // a broken saved tab must not blank the app
    console.error("tab failed to open:", initialTab, e);
    if (initialTab !== "dashboard") showTab("dashboard");
  }
  if (!window.__astraDashTimer) {
    window.__astraDashTimer = setInterval(() => {
      if ($("#tab-dashboard").classList.contains("active")) loaders.dashboard();
    }, 60_000);
  }
}

function loadScript(src) {
  return new Promise((res, rej) => {
    const s = document.createElement("script");
    s.src = src; s.onload = res; s.onerror = rej;
    document.head.appendChild(s);
  });
}

boot();
