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
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
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
  let typing = chatTyping();
  const started = Date.now();
  const finish = () => {
    if (gen !== CHAT.viewGen) return;   // a later view already owns this state
    if (typing) typing.remove();
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
        typing.remove();
        chatRenderMessages(fresh, false);
        typing = r.data.pending ? chatTyping() : null;
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

function chatBubble(who, text, action, attachedFiles, artifacts, meta) {
  hideChatEmpty();
  const row = document.createElement("div");
  row.className = "msg " + (who === "me" ? "user" : "assistant");
  const content = document.createElement("div");
  content.className = "msg-content";
  if (who !== "me") {
    const avatar = document.createElement("div");
    avatar.className = "msg-avatar";
    avatar.textContent = "🚀";
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
  textEl.innerHTML = String(text || "").replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`(.+?)`/g, "<code>$1</code>").replace(/\n/g, "<br>");
  content.appendChild(textEl);
  if (artifacts && artifacts.length) {
    const artWrap = document.createElement("div");
    artWrap.className = "msg-artifacts";
    artifacts.forEach((a) => { artWrap.appendChild(renderArtifact(a)); });
    content.appendChild(artWrap);
  }
  if (action === "confirm") {
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
      const typingRow = chatTyping();
      try {
        const r = await post("/api/chat/resume", { execution_id: eid, allow });
        typingRow.remove();
        if (!r.ok || !r.data) {
          chatBubble("ai", "Server e problem — `" + (r.error || "unknown error") + "`");
          return;
        }
        chatBubble("ai", r.data.reply, r.data.action, null,
                   r.data.artifacts, r.data.data);
      } catch (err) {
        typingRow.remove();
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
function chatTyping() {
  hideChatEmpty();
  const row = document.createElement("div");
  row.className = "msg assistant";
  row.innerHTML = `<div class="msg-avatar">🚀</div>
    <div class="msg-content"><div class="typing"><span></span><span></span><span></span></div></div>`;
  $("#chat-log").appendChild(row);
  $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
  return row;
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
  const typingRow = chatTyping();
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
    typingRow.remove();
    if (!r.ok || !r.data) {
      chatBubble("ai", "Server e problem — `" + (r.error || "unknown error") + "`");
      return;
    }
    chatBubble("ai", r.data.reply, r.data.action, null, r.data.artifacts, r.data.data);
    if (r.data.action === "dashboard") loaders.dashboard();
  } catch (err) {
    hideUploadIndicator();
    if (sentGen !== CHAT.viewGen) return;   // moved to a different chat — leave it be
    typingRow.remove();
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
  const text = state === "live" ? "● LIVE"
             : state === "paused" ? "Ⅱ PAUSED"
             : "○ RECONNECTING";
  // The Agent Workflow tab mirrors the same connection state, so the badge
  // reads "LIVE" only when the shared feed really is live.
  ["#logs-live", "#wf-live"].forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    el.textContent = text;
    el.dataset.state = state;
  });
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

function buildBlock(title, text) {
  const wrap = document.createElement("div");
  wrap.className = "tl-block";
  const h = document.createElement("h5");
  h.textContent = title;
  const pre = document.createElement("pre");
  pre.textContent = text;         // textContent => no HTML injection
  wrap.append(h, pre);
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
  row.addEventListener("click", toggle);
  row.addEventListener("keydown", (ev) => {
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
  // One shared SSE connection feeds both panels; the Agent Workflow tab gets
  // every event regardless of the Activity Log's own pause/filter state.
  if (window.AstraWorkflowFeed) window.AstraWorkflowFeed.ingest(event);
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
// text, top-to-bottom, in the same chronological order shown on screen.
async function copyLogsToClipboard(btn) {
  const feed = $("#live-feed");
  const lines = $$(".tl-row", feed)
    .filter((el) => !el.classList.contains("hidden"))
    .map((el) => {
      const part = (sel) => { const n = $(sel, el); return n ? n.textContent.trim() : ""; };
      return [part(".tl-time"), part(".tl-title"), part(".tl-sub"), part(".tl-meta")]
        .filter(Boolean).join("  ");
    });
  const out = lines.join("\n");
  const flash = (label) => {
    if (!btn) return;
    const prev = btn.textContent;
    btn.textContent = label;
    setTimeout(() => { btn.textContent = prev; }, 1400);
  };
  if (!out) { flash("Nothing to copy"); return; }
  try {
    await navigator.clipboard.writeText(out);
    flash("✅ Copied");
  } catch (_) {
    // clipboard API unavailable/blocked (e.g. non-https localhost webview) —
    // fall back to a hidden textarea + execCommand.
    try {
      const ta = document.createElement("textarea");
      ta.value = out;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      flash("✅ Copied");
    } catch (_e) {
      flash("⚠️ Copy failed");
    }
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
  if (st.gatewayAll && now - st.gatewayAll > RUNNING_MAX_AGE_MS) st.gatewayAll = 0;
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
  if (pendingCount === 0 && !anyLive && (st.testAll || st.gatewayAll)) {
    _runningUpdate((s2) => { s2.testAll = 0; s2.gatewayAll = 0; });
    st.testAll = 0; st.gatewayAll = 0;
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
  setBusy($("#btn-gateway-test"), "⏳ Testing gateway…",
          (RESTORED_GATEWAY_PENDING.size + LIVE_GATEWAY_TESTS.size) > 0 && !!(st.gatewayAll || st.testAll));
  clearTimeout(_runningPollTimer);
  _runningPollTimer = null;
  if (pendingCount > 0) _runningPollTimer = setTimeout(() => loaders.providers(), 2000);
}

// One API key's result for one model. `k` = {key_id, label, pending?, ok?,
// latency_ms?, error?}; `ok` undefined/null means "never tested".
function keyChipHtml(k) {
  const id = `data-key="${esc(k.key_id)}"`;
  if (k.pending) return `<span class="key-chip pending" ${id}>⏳ ${esc(k.label)}</span>`;
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
            : `🧪 Test (${modelCount || 0} model${modelCount === 1 ? "" : "s"}${keyCount > 1 ? ` × ${keyCount} keys` : ""})`) +
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
      _runningUpdate((st) => { st.testAll = Date.now(); st.gatewayAll = Date.now(); });
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
        await Promise.allSettled([
          ...providerNames.map((name) =>
            testProviderStreaming(name).then(bumpProgress)),
          ...connectionKeys.map((key) =>
            testGatewayConnectionStreaming(key).then(bumpProgress)),
        ]);
      } finally {
        delete testAllBtn.dataset.live;
        _runningUpdate((st) => { st.testAll = 0; st.gatewayAll = 0; });
        testAllBtn.disabled = false;
        testAllBtn.textContent = prevLabel;
        loaders.providers();
      }
    };
  }
  renderGatewayCard(r.ok ? (r.data.astra_ai_gateway || null) : null);
  _maybeResumeRuns();
  _syncRunningUi();
};

// Streams a single provider's every model test, live — the same routine
// the single "🧪 Test" button uses, factored out so Test All can run it
// for every provider in parallel without duplicating the logic. `btn`
// (optional) gets its label updated while this provider's own test runs;
// omit it when called as part of a bulk Test All (the bulk button owns
// its own progress label instead).
async function testProviderStreaming(name, btn, resume) {
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
    await _testProviderStreamingInner(name, btn, resume);
  } finally {
    if (LIVE_PROVIDER_TESTS.delete(name)) _runningUpdate((st) => { delete st.providers[name]; });
  }
}

async function _testProviderStreamingInner(name, btn, resume) {
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
  if (!resume) {
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
    await testProviderKeysStreaming(name, models, keys, tableEl, (r) => bumpCounts(r.ok),
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

async function testProviderKeysStreaming(name, models, keys, tableEl, onResult, resumeRows) {
  // resumeRows: rows restored after a refresh — only their still-pending chips
  // are (re)fired; finished chips keep the result the server saved.
  const rows = resumeRows || models.map((m) => ({
    model: m,
    keys: keys.map((k) => ({ key_id: k.key_id, label: k.label, pending: true })),
  }));
  PROVIDER_MODEL_RESULTS[name] = rows;
  if (tableEl) tableEl.innerHTML = modelHealthRowsHtml(rows);
  const repaint = (row) => {
    if (!tableEl) return;
    const el = $(`[data-model-row="${CSS.escape(row.model)}"]`, tableEl);
    if (el) el.outerHTML = modelHealthRowsHtml([row]);
  };
  const probes = [];
  rows.forEach((row) => row.keys.forEach((slot) => {
    if (!slot.pending) return;
    probes.push(
      post(`/api/v1/providers/${encodeURIComponent(name)}/test/${encodeURIComponent(row.model)}` +
           `?key=${encodeURIComponent(slot.key_id)}`)
        .then((res) => (res.ok && res.data) ? res.data
          : { ok: false, latency_ms: 0, error: res.error || "test failed" })
        .catch((e) => ({ ok: false, latency_ms: 0, error: String(e) }))
        .then((r) => {
          Object.assign(slot, { pending: false, ok: !!r.ok, latency_ms: r.latency_ms,
                                error: r.error, tested_at: new Date().toLocaleString() });
          repaint(row);
          if (onResult) onResult(r);
        }));
  }));
  // allSettled: one key/model failing never stops the rest from finishing.
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
async function testGatewayConnectionStreaming(key, btn, resume) {
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
    await _testGatewayConnectionStreamingInner(key, btn, resume);
  } finally {
    if (LIVE_GATEWAY_TESTS.delete(key)) _runningUpdate((st) => { delete st.gateway[key]; });
  }
}

async function _testGatewayConnectionStreamingInner(key, btn, resume) {
  const models = GATEWAY_MODELS[key] || [];
  const tableEl = $(`[data-gw-conn="${CSS.escape(key)}"] [data-role="gw-model-table"]`);
  if (!models.length) {
    if (tableEl) tableEl.innerHTML = `<div class="model-health-empty">no model configured</div>`;
    return;
  }
  if (btn) btn.textContent = `⏳ Testing ${models.length} model${models.length === 1 ? "" : "s"}…`;
  GATEWAY_MODEL_RESULTS[key] = GATEWAY_MODEL_RESULTS[key] || [];
  const toRun = resume
    ? GATEWAY_MODEL_RESULTS[key].filter((r) => r.pending).map((r) => r.model)
    : models;
  await streamModelTests(toRun, tableEl, GATEWAY_MODEL_RESULTS[key],
    (modelId) => post(`/api/v1/gateway/${encodeURIComponent(key)}/test/${encodeURIComponent(modelId)}`)
      .then((res) => (res.ok && res.data) ? res.data :
        { model: modelId, ok: false, latency_ms: 0, error: res.error || "test failed" }),
    undefined, !!resume);
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
      (busy ? "⏳ Testing…" : `🧪 Test (${modelCount || 0} model${modelCount === 1 ? "" : "s"})`) +
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

  const testBtn = $("#btn-gateway-test");
  if (testBtn && !testBtn.dataset.hooked) {
    testBtn.dataset.hooked = "1";
    testBtn.onclick = async () => {
      testBtn.disabled = true;
      testBtn.dataset.live = "1";
      _runningUpdate((st) => { st.gatewayAll = Date.now(); });
      const prevLabel = testBtn.dataset.restored ? testBtn.dataset.orig : testBtn.textContent;
      const keys = Object.keys(GATEWAY_MODELS);
      const total = keys.length;
      let done = 0;
      testBtn.textContent = `⏳ Testing gateway (0/${total})…`;
      try {
        await Promise.allSettled(keys.map((key) =>
          testGatewayConnectionStreaming(key).then(() => {
            done += 1;
            testBtn.textContent = `⏳ Testing gateway (${done}/${total})…`;
          })));
      } finally {
        delete testBtn.dataset.live;
        _runningUpdate((st) => { st.gatewayAll = 0; });
        testBtn.disabled = false;
        testBtn.textContent = prevLabel;
        loaders.providers();
      }
    };
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

/* --------------------------- 🔀 Agent Workflow (core) -----------------------
 * A view over the REAL execution path — never a second engine:
 *
 *  · "Runtime pipeline" — one live ChatPipeline turn, one node per real stage
 *    (astra/ai/chat_pipeline.py → gateway.py → router.py → agent_tool_loop.py
 *    → tools/registry.py → gateway_task_completion.py → response_boundary.py).
 *    Status is folded from the shared /api/events feed by
 *    static/js/workflow_model.js (chat.pipeline.* / router.* / ai.* /
 *    agent.tool_loop.* / tool.* / terminal.* / web3.* / gateway.*).
 *
 *  · "Workflows" — astra/workflows/engine.py definitions rendered as their
 *    real step graph (`depends_on` + `{{step.param}}` + `if` conditions),
 *    runs from GET /api/workflows/runs with live overlay, and the
 *    SchedulerManager schedules that fire them.
 *
 *  · "Runs & schedules" — execution history (incl. step errors) + schedules.
 *
 * Everything the page shows comes from existing endpoints; the pure layout /
 * reduction rules live in static/js/workflow_model.js and are node-tested.
 */
const WM = window.AstraWorkflow;

const WFLOW = {
  view: "pipeline",
  booted: false, loading: false,
  toolList: [], toolByName: {},
  defs: [], runs: [], schedules: [],
  defId: 0, viewRunId: null, nodeId: "",
  builder: null,
  live: null,
  turns: [], turnIdx: -1,
  seen: {},
  runsTimer: null, edgeFrame: null,
};
if (WM) WFLOW.live = WM.emptyRun();

/* ---------------------------------------------------------------- helpers */
function wfDot(status) { return `<span class="wf-dot ${esc(status || "info")}"></span>`; }
function wfChip(status, text) {
  return `<span class="wf-chip ${esc(status || "info")}">${esc(text)}</span>`;
}
function wfParamsText(obj) {
  try { return JSON.stringify(obj || {}, null, 0); } catch (_) { return "{}"; }
}
function wfPre(host, label, text) {
  const wrap = document.createElement("div");
  wrap.className = "wf-block";
  const h = document.createElement("h5");
  h.textContent = label;
  const pre = document.createElement("pre");
  pre.className = "mono small";
  pre.textContent = text == null ? "" : String(text);
  wrap.appendChild(h);
  wrap.appendChild(pre);
  host.appendChild(wrap);
}

function wfViewSet(name) {
  WFLOW.view = name;
  $$("#wf-views .chip").forEach((c) =>
    c.classList.toggle("active", c.dataset.view === name));
  ["pipeline", "workflows", "runs"].forEach((v) => {
    const el = $("#wf-view-" + v);
    if (el) el.hidden = v !== name;
  });
  if (name === "workflows") requestAnimationFrame(wfDrawEdges);
}

/* ------------------------------------------------------- runtime pipeline */
function wfTurnCurrent() { return WFLOW.turns[WFLOW.turnIdx] || null; }

function wfRenderTurnNav() {
  const label = $("#wf-turn-label");
  if (!label) return;
  const t = wfTurnCurrent();
  label.textContent = !t ? "no turn yet"
    : `turn ${WFLOW.turnIdx + 1}/${WFLOW.turns.length} · ${String(t.key).slice(0, 14)}…`;
}

function wfRenderPipeline() {
  const flow = $("#wf-flow");
  if (!flow) return;
  const t = wfTurnCurrent();
  wfRenderTurnNav();
  if (!t) {
    flow.innerHTML = `<div class="empty">Kono chat turn ekhono nei — Assistant tab e kotha bolun, tarpor ekhane live dekhun.</div>`;
    const meta0 = $("#wf-turn-meta"); if (meta0) meta0.innerHTML = "";
    const ev0 = $("#wf-events");
    if (ev0) ev0.innerHTML = `<div class="empty">…</div>`;
    return;
  }
  flow.innerHTML = WM.PIPELINE_STAGES.map((s, i) => {
    const st = t.stages[s.id] || { status: "info", detail: "", count: 0 };
    const running = st.status === "running" ? " is-running" : "";
    const chips = (s.id === "tools" && t.tools.length)
      ? `<div class="wf-toolchips">` +
        t.tools.slice(0, 8).map((n) => `<span class="tinytag">${esc(n)}</span>`).join("") +
        (t.tools.length > 8 ? `<span class="tinytag">+${t.tools.length - 8}</span>` : "") +
        `</div>` : "";
    return (i ? `<div class="wf-arrow" aria-hidden="true">→</div>` : "") +
      `<div class="wf-stage${running}" data-stage="${esc(s.id)}">
         <div class="wf-stage-top">${wfDot(st.status)}
           <span class="wf-stage-label">${s.icon} ${esc(s.label)}</span></div>
         <div class="wf-stage-src mono">${esc(s.file)} :: ${esc(s.symbol)}</div>
         <div class="wf-stage-detail">${esc(st.detail || (st.count ? st.count + " event(s)" : "—"))}</div>
         ${chips}
       </div>`;
  }).join("");

  const meta = $("#wf-turn-meta");
  if (meta) {
    const bits = [];
    const cls = t.status === "ok" ? "ok" : t.status === "err" ? "err"
              : t.status === "warn" ? "warn" : "running";
    bits.push(wfChip(cls, "turn: " + t.status));
    if (t.label) bits.push(`<span class="tag">${esc(t.label)}</span>`);
    if (t.provider || t.model) {
      bits.push(`<span class="tag">${esc([t.provider, t.model].filter(Boolean).join(" · "))}</span>`);
    }
    if (t.verdict) bits.push(`<span class="tag">verify: ${esc(t.verdict)}</span>`);
    bits.push(`<span class="muted small">${t.events.length} events · turn ${esc(t.key)}</span>`);
    meta.innerHTML = bits.join(" ");
  }
  wfRenderTurnEvents(t);
}

/* Event-derived text is written with textContent (never injected as HTML). */
function wfRenderTurnEvents(t) {
  const host = $("#wf-events");
  if (!host) return;
  host.innerHTML = "";
  const evs = (t.events || []).slice(-120);
  if (!evs.length) {
    host.innerHTML = `<div class="empty">No stage events yet.</div>`;
    return;
  }
  evs.forEach((e) => {
    const m = AstraLog.normalize(e);
    const row = document.createElement("div");
    row.className = "wf-erow";
    row.dataset.status = m.status;
    const ico = document.createElement("span");
    ico.className = "tl-ico";
    ico.textContent = m.icon || "•";
    const main = document.createElement("div");
    main.className = "tl-main";
    const title = document.createElement("div");
    title.className = "tl-title";
    title.textContent = m.title + (m.subject ? " · " + m.subject : "");
    const sub = document.createElement("div");
    sub.className = "tl-sub";
    sub.textContent = [m.kind, m.detail].filter(Boolean).join(" — ");
    main.appendChild(title);
    main.appendChild(sub);
    const time = document.createElement("span");
    time.className = "tl-time";
    time.textContent = m.time || "";
    row.appendChild(ico);
    row.appendChild(main);
    row.appendChild(time);
    host.appendChild(row);
  });
  host.scrollTop = host.scrollHeight;
}

function wfRenderSrcMap() {
  const host = $("#wf-srcmap");
  if (!host || !WM) return;
  host.innerHTML = WM.PIPELINE_STAGES.map((s) =>
    `<div class="wf-srcrow"><span class="wf-srcico">${s.icon}</span>` +
    `<div class="wf-srcbody"><div>${esc(s.label)}</div>` +
    `<div class="mono small">${esc(s.file)} :: ${esc(s.symbol)}</div>` +
    `<div class="muted small">${esc(s.note)}</div></div></div>`).join("");
}

/* The real state vocabulary, spelled out — every value below is one the
 * engine/registry actually produces (astra/core/state.py + engine results). */
function wfRenderLegend() {
  const host = $("#wf-legend");
  if (!host || !WM) return;
  host.innerHTML = [
    ["info", "pending"], ["running", "running"], ["ok", "completed"],
    ["err", "failed"], ["warn", "skipped / blocked"],
  ].map(([cls, label]) => `<span>${wfDot(cls)}${esc(label)}</span>`).join("") +
    `<span class="muted">· dashed edge = {{step.param}} data flow</span>`;
}

/* ------------------------------------------------------------ definitions */
function wfCurrentDef() {
  return WFLOW.defs.find((d) => Number(d.id) === Number(WFLOW.defId)) || null;
}
function wfSteps(steps) { return WM.normalizeSteps(steps); }

function wfBuilderDef() {
  if (!WFLOW.builder) return null;
  const collected = wfCollectBuilder();
  return { id: 0, name: collected.name, description: collected.description,
           steps: collected.steps };
}
function wfGraphDef() { return WFLOW.builder ? wfBuilderDef() : wfCurrentDef(); }

/* Real per-node state: the live event snapshot when the displayed run is the
 * one currently emitting events, otherwise the run's persisted `results`. */
function wfNodeStates(def) {
  const out = {};
  if (!def) return out;
  const steps = wfSteps(def.steps);
  const runId = WFLOW.viewRunId;
  if (runId != null && WFLOW.live && String(WFLOW.live.run_id) === String(runId)) {
    steps.forEach((s) => {
      const st = WFLOW.live.steps[s.id];
      out[s.id] = st ? st.state : "pending";
    });
    return out;
  }
  const run = WFLOW.runs.find((r) => Number(r.id) === Number(runId));
  if (run) return WM.runStates(run, steps);
  return out;
}

function wfRenderDefControls() {
  const sel = $("#wf-select");
  if (sel) {
    sel.innerHTML = WFLOW.defs.length
      ? WFLOW.defs.map((d) =>
          `<option value="${d.id}"${Number(d.id) === Number(WFLOW.defId) ? " selected" : ""}>` +
          `${esc(d.name)}${d.enabled ? "" : " (disabled)"}</option>`).join("")
      : `<option value="">no workflows</option>`;
  }
  const hint = $("#wf-def-hint");
  const def = wfCurrentDef();
  if (hint) {
    if (!def) hint.textContent = "Kono workflow definition nei — ＋ New diye ekta banao.";
    else {
      const steps = wfSteps(def.steps);
      const run = WFLOW.runs.find((r) => Number(r.id) === Number(WFLOW.viewRunId));
      hint.textContent = `${steps.length} step(s) · id ${def.id}` +
        (run ? ` · showing run #${run.id} (${run.status})` : " · live view");
    }
  }
  const cards = $("#wf-cards");
  if (cards) {
    const def2 = def;
    const steps = def2 ? wfSteps(def2.steps) : [];
    const g = def2 ? WM.buildGraph(def2.steps) : { edges: [], run_params: [] };
    const deps = g.edges.filter((e) => e.kind === "dep").length;
    const dataEdges = g.edges.filter((e) => e.kind === "data").length;
    const conds = steps.filter((s) => s.if).length;
    cards.innerHTML = [
      { v: steps.length, k: "steps", s: "workflow_definitions.steps" },
      { v: deps, k: "depends_on edges", s: "engine._topo()" },
      { v: dataEdges, k: "{{ref}} flows", s: "engine._resolve()" },
      { v: conds, k: "if conditions", s: "engine._condition()" },
      { v: WFLOW.runs.length, k: "runs recorded", s: "workflow_runs" },
    ].map((c) => `<div class="card"><div class="card-v">${esc(c.v)}</div>` +
                 `<div class="card-k">${esc(c.k)}</div>` +
                 `<div class="card-s">${esc(c.s)}</div></div>`).join("");
  }
}

function wfRenderGraph() {
  const host = $("#wf-graph");
  if (!host || !WM) return;
  const def = wfGraphDef();
  if (!def || !wfSteps(def.steps).length) {
    host.innerHTML = `<div class="empty">Kono step nei — ${WFLOW.builder ? "＋ Add step" : "＋ New"} diye shuru korun.</div>`;
    wfDrawEdges();
    return;
  }
  const g = WM.buildGraph(def.steps);
  const states = WFLOW.builder ? {} : wfNodeStates(def);
  host.innerHTML = g.layers.map((ids, col) =>
    `<div class="wf-col" data-col="${col}">` +
    ids.map((id) => {
      const n = g.nodes.find((x) => x.id === id) || { id: id, tool: "", condition: null };
      const state = states[id] || "pending";
      const status = (WM.STEP_STATE_STATUS[state] || "info");
      const tool = WFLOW.toolByName[n.tool] || {};
      const cond = (n.condition && n.condition.step)
        ? `<span class="tinytag">if ${esc(n.condition.step)}${n.condition.op ? " " + esc(n.condition.op) : ""}</span>`
        : "";
      const sel = WFLOW.nodeId === id ? " selected" : "";
      return `<div class="wf-gnode${sel}" data-node="${esc(id)}" tabindex="0" role="button">` +
        `<div class="wf-gnode-head">${wfDot(status)}<b>${esc(id)}</b>` +
        `<span class="wf-gnode-tool mono">${esc(n.tool || "?")}</span></div>` +
        `<div class="wf-gnode-sub">${esc(tool.category ? tool.category + " · " + state : state)}</div>` +
        cond + `</div>`;
    }).join("") + `</div>`).join("");
  wfDrawEdges();
}

function wfDrawEdges() {
  const svg = $("#wf-edges");
  const host = $("#wf-graph");
  const wrap = $(".wf-graph-wrap");
  if (!svg || !host || !wrap || !WM) return;
  const def = wfGraphDef();
  if (!def || !wfSteps(def.steps).length) { svg.innerHTML = ""; return; }
  const g = WM.buildGraph(def.steps);
  const wbox = wrap.getBoundingClientRect();
  const pts = {};
  $$(".wf-gnode", host).forEach((el) => {
    const r = el.getBoundingClientRect();
    pts[el.dataset.node] = { x: r.left - wbox.left, y: r.top - wbox.top,
                             w: r.width, h: r.height };
  });
  const paths = g.edges.map((e) => {
    const a = pts[e.from], b = pts[e.to];
    if (!a || !b) return "";
    const x1 = a.x + a.w, y1 = a.y + a.h / 2;
    const x2 = b.x, y2 = b.y + b.h / 2;
    if (x2 <= x1) return "";
    const mx = (x1 + x2) / 2;
    const cls = e.kind === "data" ? "wf-edge data" : "wf-edge";
    const tip = e.kind === "data" ? `<title>${esc(e.via || "")}</title>` : "";
    return `<path class="${cls}" d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}">${tip}</path>`;
  }).join("");
  svg.setAttribute("viewBox", `0 0 ${wbox.width} ${wbox.height}`);
  svg.setAttribute("width", String(wbox.width));
  svg.setAttribute("height", String(wbox.height));
  svg.innerHTML = paths;
}

function wfRenderInspector() {
  const host = $("#wf-inspector");
  if (!host || !WM) return;
  const def = wfGraphDef();
  const id = WFLOW.nodeId;
  if (!def || !id) {
    host.innerHTML = `<div class="empty">Graph e ekta node click korun.</div>`;
    return;
  }
  const g = WM.buildGraph(def.steps);
  const n = g.nodes.find((x) => x.id === id);
  if (!n) {
    host.innerHTML = `<div class="empty">Node paoa jay ni.</div>`;
    return;
  }
  const tool = WFLOW.toolByName[n.tool] || null;
  const run = WFLOW.runs.find((r) => Number(r.id) === Number(WFLOW.viewRunId)) || null;
  const live = (WFLOW.live && String(WFLOW.live.run_id) === String(WFLOW.viewRunId))
    ? WFLOW.live.steps[id] : null;
  const result = (run && run.results) ? run.results[id] : null;
  const state = live ? live.state : (result ? WM.stepStateFromResult(result) : "pending");
  const status = WM.STEP_STATE_STATUS[state] || "info";
  const resolved = WM.resolveParams(n.params, (run && run.results) || {},
                                    (run && run.params) || {});
  const row = (k, v) => `<div class="row wf-irow"><b>${esc(k)}</b><span>${esc(v)}</span></div>`;
  const err = (live && live.error) || (result ? WM.stepError(result) : "");

  let html = `<div class="wf-ihead">${wfDot(status)} <b>${esc(id)}</b> ${wfChip(status, state)}`;
  if (n.name) html += ` <span class="muted small">${esc(n.name)}</span>`;
  html += `</div>`;

  html += `<div class="wf-isect"><h5>Tool</h5>`;
  if (tool) {
    html += row("name", tool.name);
    html += row("source", `${tool.module || "?"} :: ${tool.function || "?"}`);
    html += row("category", tool.category);
    html += row("risk", tool.risk_level + (tool.requires_confirmation ? " · requires confirmation" : ""));
    html += row("calls", `timeout ${tool.timeout_s || 0}s · retries ${tool.retries || 0} · rate limit ${tool.rate_limit_per_min || 0}/min`);
    if (tool.description) html += `<div class="muted small wf-idesc">${esc(tool.description)}</div>`;
  } else {
    html += `<div class="muted small">Tool <b>${esc(n.tool || "?")}</b> registry te nei — engine.run() e ei step ta failed hobe (KeyError).</div>`;
  }
  html += `</div>`;

  html += `<div class="wf-isect"><h5>Wiring</h5>`;
  html += row("depends_on", n.depends_on.length ? n.depends_on.join(", ") : "—");
  html += row("dependents", n.dependents.length ? n.dependents.join(", ") : "—");
  html += row("{{refs}}", n.refs.length ? n.refs.join(", ") : "—");
  html += row("condition", n.condition && n.condition.step
    ? `if ${n.condition.step} ${n.condition.op || "ok"}` : "—");
  html += `</div>`;

  if (tool && tool.input_schema && tool.input_schema.properties) {
    const props = tool.input_schema.properties;
    const keys = Object.keys(props);
    html += `<div class="wf-isect"><h5>Input schema (registry)</h5>`;
    html += keys.length
      ? keys.map((k) => {
          const spec = props[k] || {};
          const req = spec.required ? " · required" : "";
          return `<div class="row wf-irow"><b class="mono">${esc(k)}</b><span>${esc(spec.type || "any")}${esc(req)}</span></div>`;
        }).join("")
      : `<div class="muted small">no declared arguments</div>`;
    html += `</div>`;
  }

  host.innerHTML = html;
  wfPre(host, "params (definition)", wfParamsText(n.params));
  wfPre(host, "resolved input (engine._resolve preview)", wfParamsText(resolved));
  if (result) {
    wfPre(host, "run result #" + (run ? run.id : ""),
          JSON.stringify(result, null, 2));
  }
  if (err) wfPre(host, "error", err);
}

/* ----------------------------------------------------------------- runs */
function wfRenderRuns() {
  const host = $("#wf-runs");
  if (!host || !WM) return;
  if (!WFLOW.runs.length) {
    host.innerHTML = `<div class="empty">Kono run nei — ekta workflow ▶ Run korun.</div>`;
    return;
  }
  host.innerHTML = WFLOW.runs.map((r) => {
    const status = r.status || "?";
    const cls = WM.RUN_STATUS_STATUS[status] || "info";
    const sel = Number(r.id) === Number(WFLOW.viewRunId) ? " selected" : "";
    const spanR = [r.started_at, r.completed_at].filter(Boolean).join(" → ");
    const failed = Object.keys(r.results || {}).filter((k) => {
      const res = r.results[k];
      return res && res.ok === false;
    });
    const err = r.error ? `<div class="muted small">${esc(String(r.error).slice(0, 160))}</div>` : "";
    const ferr = failed.length
      ? `<div class="muted small">failed steps: ${esc(failed.join(", "))}</div>` : "";
    return `<div class="row wf-runrow${sel}" data-run="${r.id}" tabindex="0" role="button">` +
      `<b>#${esc(r.id)}</b><span class="wf-runname">${esc(r.name || "")}</span>` +
      `${wfChip(cls, status)}` +
      `<span class="muted small">${esc(r.current_step ? "at step " + r.current_step : "")}</span>` +
      `<span class="muted small">${esc(spanR)}</span>${err}${ferr}</div>`;
  }).join("");
}

function wfRenderSchedules() {
  const host = $("#wf-schedules");
  if (!host) return;
  if (!WFLOW.schedules.length) {
    host.innerHTML = `<div class="empty">Kono schedule nei.</div>`;
    return;
  }
  host.innerHTML = WFLOW.schedules.map((s) => {
    const wf = WFLOW.defs.find((d) => Number(d.id) === Number(s.workflow_id));
    return `<div class="row wf-runrow">` +
      `<b>${esc(s.name)}</b><span>${esc(s.kind)}</span>` +
      `<span class="mono small">${esc(s.value || "")}</span>` +
      `${wfChip(s.enabled ? "ok" : "warn", s.enabled ? "enabled" : "disabled")}` +
      `<span class="muted small">${esc(wf ? "→ " + wf.name : (s.workflow_id ? "→ workflow #" + s.workflow_id : "no workflow"))}</span>` +
      `<span class="muted small">${esc(s.next_run ? "next " + s.next_run : "")}</span></div>`;
  }).join("");
}

/* ---------------------------------------------------------------- builder */
function wfOpenBuilder(def) {
  WFLOW.builder = {
    id: def && def.id ? def.id : 0,
    name: def ? (def.name || "") : "",
    description: def ? (def.description || "") : "",
    steps: def ? WM.normalizeSteps(def.steps).map((s) => ({ ...s })) : [],
  };
  if (!WFLOW.builder.steps.length) {
    const first = WFLOW.toolList[0] || null;
    WFLOW.builder.steps.push(WM.newStepTemplate(first, []));
  }
  const nameEl = $("#wf-builder-name");
  const descEl = $("#wf-builder-desc");
  if (nameEl) nameEl.value = WFLOW.builder.name;
  if (descEl) descEl.value = WFLOW.builder.description;
  const panel = $("#wf-builder");
  if (panel) panel.hidden = false;
  const hint = $("#wf-builder-hint");
  if (hint) hint.textContent = def
    ? `Editing “${def.name}” — 💾 Save PATCHes /api/workflows/${def.id} in place; the definition's run history is preserved.`
    : "New workflow — 💾 Save POSTs /api/workflows (the name must be unique).";
  wfRenderBuilderSteps();
  WFLOW.nodeId = "";
  wfRenderGraph();
  wfRenderInspector();
}

function wfCloseBuilder() {
  WFLOW.builder = null;
  const panel = $("#wf-builder");
  if (panel) panel.hidden = true;
  wfRenderAll();
}

function wfRenderBuilderSteps() {
  const host = $("#wf-builder-steps");
  if (!host || !WFLOW.builder) return;
  const steps = WFLOW.builder.steps || [];
  host.innerHTML = steps.map((s, i) => {
    const cond = s.if || {};
    const depsText = (s.depends_on || []).join(", ");
    const params = wfParamsText(s.params);
    const idList = steps.map((x) => x.id).filter(Boolean);
    return `<div class="wf-bstep" data-idx="${i}">
      <div class="wf-bstep-head">
        <input class="wf-bstep-id mono" value="${esc(s.id || "")}" placeholder="step id" aria-label="step id">
        <input class="wf-bstep-tool mono" list="wf-tools" value="${esc(s.tool || "")}" placeholder="tool" aria-label="tool">
        <span class="tinytag">#${i + 1}</span>
        <button type="button" class="btn mini danger wf-bstep-del" data-idx="${i}" aria-label="Remove step">✕</button>
      </div>
      <div class="wf-bstep-grid">
        <label>params (JSON object)
          <textarea class="wf-bstep-params mono" rows="3" spellcheck="false">${esc(params)}</textarea></label>
        <label>depends_on (comma ids)
          <input class="wf-bstep-deps mono" value="${esc(depsText)}" placeholder="${esc(idList.join(", "))}"></label>
        <label>if step
          <input class="wf-bstep-ifstep mono" value="${esc(cond.step || "")}" placeholder="(always run)"></label>
        <label>if op
          <input class="wf-bstep-ifop mono" value="${esc(cond.op || "")}" placeholder="ok"></label>
      </div>
    </div>`;
  }).join("") || `<div class="empty">Kono step nei — ＋ Add step.</div>`;
}

function wfCollectBuilder() {
  const host = $("#wf-builder-steps");
  const steps = [];
  if (host) {
    $$(".wf-bstep", host).forEach((el) => {
      const model = (WFLOW.builder && WFLOW.builder.steps[Number(el.dataset.idx)]) || {};
      const paramsRaw = ($(".wf-bstep-params", el) || {}).value || "";
      const parsed = WM.parseParamsJson(paramsRaw);
      const deps = (($(".wf-bstep-deps", el) || {}).value || "")
        .split(",").map((x) => x.trim()).filter(Boolean);
      const ifStep = (($(".wf-bstep-ifstep", el) || {}).value || "").trim();
      const ifOp = (($(".wf-bstep-ifop", el) || {}).value || "").trim();
      const step = {
        id: (($(".wf-bstep-id", el) || {}).value || "").trim(),
        tool: (($(".wf-bstep-tool", el) || {}).value || "").trim(),
        params: parsed.ok ? parsed.value : {},
      };
      if (model.name) step.name = model.name;
      if (deps.length) step.depends_on = deps;
      if (ifStep) step.if = { step: ifStep, op: ifOp || "ok" };
      step.__params_error = parsed.ok ? "" : parsed.error;
      steps.push(step);
    });
  }
  return {
    name: ($("#wf-builder-name") || {}).value || "",
    description: ($("#wf-builder-desc") || {}).value || "",
    steps: steps,
  };
}

async function wfSaveBuilder() {
  const collected = wfCollectBuilder();
  const hint = $("#wf-builder-hint");
  const steps = collected.steps.map((s) => {
    const c = { ...s };
    delete c.__params_error;
    return c;
  });
  const known = {};
  WFLOW.toolList.forEach((t) => { known[t.name] = t; });
  const v = WM.validateDefinition(collected.name, steps, known);
  collected.steps.forEach((s) => {
    if (s.__params_error) v.errors.push(`step ${s.id || "?"}: params is not a valid JSON object — ${s.__params_error}`);
  });
  const editing = WFLOW.builder && Number(WFLOW.builder.id) > 0;
  // workflow_definitions.name is UNIQUE — catch the collision locally instead
  // of surfacing a raw IntegrityError. An edit keeps its own row, so only a
  // create can collide.
  if (!editing && WFLOW.defs.some((d) => d.name === collected.name.trim())) {
    v.errors.push(`name “${collected.name.trim()}” already exists (workflow_definitions.name is UNIQUE)`);
  }
  if (!v.ok) {
    if (hint) hint.textContent = "✕ " + v.errors.join(" · ");
    return;
  }
  const payload = { name: collected.name.trim(), description: collected.description,
                    steps: steps };
  const res = editing
    ? await patch(`/api/workflows/${encodeURIComponent(WFLOW.builder.id)}`, payload)
    : await post("/api/workflows", payload);
  if (!res || !res.ok) {
    if (hint) hint.textContent = "✕ " + ((res && res.error) || "save failed");
    return;
  }
  if (hint) {
    hint.textContent = `✓ ${editing ? "updated" : "saved"} “${collected.name.trim()}” (id ${res.data.id})` +
      (v.warnings.length ? " ⚠ " + v.warnings.join(" · ") : "");
  }
  WFLOW.builder = null;
  const panel = $("#wf-builder");
  if (panel) panel.hidden = true;
  WFLOW.defId = res.data.id;
  WFLOW.viewRunId = null;
  await wfLoadDefinitions(true);
  await wfLoadRuns();
  wfRenderAll();
}

async function wfDeleteCurrent() {
  const def = wfCurrentDef();
  if (!def) return;
  const hint = $("#wf-def-hint");
  const yes = window.confirm(`Delete workflow “${def.name}”? Its runs (workflow_runs) cascade.`);
  if (!yes) return;
  const res = await del(`/api/workflows/${encodeURIComponent(def.id)}`);
  if (!res || !res.ok) {
    if (hint) hint.textContent = "✕ " + ((res && res.error) || "delete failed");
    return;
  }
  WFLOW.viewRunId = null;
  WFLOW.nodeId = "";
  await Promise.all([wfLoadDefinitions(true), wfLoadRuns()]);
  wfRenderAll();
  if (hint) hint.textContent = `✓ deleted “${def.name}”`;
}

async function wfRunCurrent() {
  const def = wfCurrentDef();
  const hint = $("#wf-def-hint");
  if (!def) { if (hint) hint.textContent = "Kono workflow select kora nei."; return; }
  const parsed = WM.parseParamsJson(($("#wf-run-params") || {}).value || "{}");
  if (!parsed.ok) { if (hint) hint.textContent = "✕ run params: " + parsed.error; return; }
  if (hint) hint.textContent = `▶ running “${def.name}”…`;
  const res = await post(`/api/workflows/${encodeURIComponent(def.id)}/run`,
                         { params: parsed.value });
  if (!res || !res.ok) {
    if (hint) hint.textContent = "✕ " + ((res && res.error) || "run failed");
    return;
  }
  const run = res.data || {};
  WFLOW.viewRunId = run.id;
  wfViewSet("workflows");
  await wfLoadRuns();
  wfRenderAll();
  if (hint) hint.textContent = `✓ run #${run.id} → ${run.status}`;
}

/* ------------------------------------------------------------- data loads */
async function wfLoadTools() {
  const r = await api("/api/tools");
  const tools = (r && r.ok && r.data && r.data.tools) || [];
  WFLOW.toolList = tools;
  WFLOW.toolByName = {};
  tools.forEach((t) => { WFLOW.toolByName[t.name] = t; });
  const dl = $("#wf-tools");
  if (dl) dl.innerHTML = tools.map((t) => `<option value="${esc(t.name)}"></option>`).join("");
}

async function wfLoadDefinitions(keepSelection) {
  const r = await api("/api/workflows");
  const defs = (r && r.ok && r.data) || [];
  WFLOW.defs = Array.isArray(defs) ? defs : [];
  if (!WFLOW.defs.length) { WFLOW.defId = 0; return; }
  const stillThere = WFLOW.defs.some((d) => Number(d.id) === Number(WFLOW.defId));
  if (!keepSelection || !stillThere) WFLOW.defId = WFLOW.defs[0].id;
}

async function wfLoadRuns() {
  const r = await api("/api/workflows/runs");
  const runs = (r && r.ok && r.data) || [];
  WFLOW.runs = Array.isArray(runs) ? runs : [];
}

async function wfLoadSchedules() {
  const r = await api("/api/schedules");
  const sc = (r && r.ok && r.data) || [];
  WFLOW.schedules = Array.isArray(sc) ? sc : [];
}

function wfRenderAll() {
  wfRenderDefControls();
  wfRenderGraph();
  wfRenderInspector();
  wfRenderRuns();
  wfRenderSchedules();
  wfRenderLegend();
}

/* Schedule a runs refresh (the run list is the source of truth once a run
 * ends; the live event stream is the source of truth while it runs). */
function wfRefreshRunsSoon() {
  if (WFLOW.runsTimer) return;
  WFLOW.runsTimer = setTimeout(async () => {
    WFLOW.runsTimer = null;
    await wfLoadRuns();
    if (WFLOW.view === "runs") wfRenderRuns();
    else { wfRenderRuns(); wfRenderDefControls(); }
  }, 400);
}

/* --------------------------------------------------- live event ingestion */
function wfIngestTurn(event) {
  const key = WM.turnKeyOf(event);
  if (!key) return;
  const t = WFLOW.turns.find((x) => x.key === key);
  let target = t;
  if (!target) {
    target = WM.newTurn(key, event, event.id);
    WFLOW.turns.push(target);
    if (WFLOW.turns.length > 40) {
      WFLOW.turns.shift();
      if (WFLOW.turnIdx > 0) WFLOW.turnIdx -= 1;
    }
    WFLOW.turnIdx = Math.max(0, WFLOW.turns.length - 1);
  }
  WM.reduceTurn(target, event);
  if (WFLOW.view === "pipeline" && wfTurnCurrent() === target) wfRenderPipeline();
  else wfRenderTurnNav();
}

function wfIngest(event) {
  if (!event || event.id == null || !WM) return;
  const k = String(event.id);
  if (WFLOW.seen[k]) return;
  WFLOW.seen[k] = 1;
  const keys = Object.keys(WFLOW.seen);
  if (keys.length > 4000) keys.slice(0, 2000).forEach((x) => { delete WFLOW.seen[x]; });

  const data = event.data || {};
  const kind = String(event.kind || "");
  const runId = WM.runIdOf(data);
  if (runId) {
    if (kind === "workflow.started") {
      WFLOW.live = WM.emptyRun();
      // Live-follow: a run of the definition the user is looking at (the
      // SchedulerManager fires these in a background thread too) becomes the
      // displayed run, so the graph tracks it as it executes.
      const def = WFLOW.defs.find((d) => d.name === String(data.workflow || ""));
      if (def && Number(def.id) === Number(WFLOW.defId)) {
        WFLOW.viewRunId = runId;
        if (WFLOW.view === "workflows") {
          wfRenderDefControls();
          wfRenderGraph();
          wfRenderInspector();
        }
      }
    }
    else if (WFLOW.live.run_id && WFLOW.live.run_id !== runId) return;
    WM.reduceRun(WFLOW.live, event);
    if (String(WFLOW.viewRunId) === String(runId) && WFLOW.view === "workflows") {
      wfRenderGraph();
      wfRenderInspector();
    }
    if (kind === "workflow.started" || kind === "workflow.completed" ||
        kind === "workflow.failed") wfRefreshRunsSoon();
    // fall through — a run's events also belong to the pipeline view
  }
  if (WM.turnKeyOf(event)) wfIngestTurn(event);
}

window.AstraWorkflowFeed = { ingest: wfIngest };

/* ------------------------------------------------------------------- wire */
function wfWire() {
  const views = $("#wf-views");
  if (views && !views.dataset.hooked) {
    views.dataset.hooked = "1";
    views.addEventListener("click", (e) => {
      const chip = e.target.closest(".chip");
      if (chip) wfViewSet(chip.dataset.view);
    });
  }
  const prev = $("#btn-wf-turn-prev");
  const next = $("#btn-wf-turn-next");
  if (prev && !prev.dataset.hooked) {
    prev.dataset.hooked = "1";
    prev.addEventListener("click", () => {
      WFLOW.turnIdx = Math.max(0, WFLOW.turnIdx - 1);
      wfRenderPipeline();
    });
  }
  if (next && !next.dataset.hooked) {
    next.dataset.hooked = "1";
    next.addEventListener("click", () => {
      WFLOW.turnIdx = Math.min(WFLOW.turns.length - 1, WFLOW.turnIdx + 1);
      wfRenderPipeline();
    });
  }
  const sel = $("#wf-select");
  if (sel && !sel.dataset.hooked) {
    sel.dataset.hooked = "1";
    sel.addEventListener("change", () => {
      WFLOW.defId = Number(sel.value) || 0;
      WFLOW.viewRunId = null;
      WFLOW.nodeId = "";
      wfRenderAll();
    });
  }
  const newBtn = $("#btn-wf-new");
  if (newBtn && !newBtn.dataset.hooked) {
    newBtn.dataset.hooked = "1";
    newBtn.addEventListener("click", () => wfOpenBuilder(null));
  }
  const editBtn = $("#btn-wf-edit");
  if (editBtn && !editBtn.dataset.hooked) {
    editBtn.dataset.hooked = "1";
    editBtn.addEventListener("click", () => wfOpenBuilder(wfCurrentDef()));
  }
  const delBtn = $("#btn-wf-delete");
  if (delBtn && !delBtn.dataset.hooked) {
    delBtn.dataset.hooked = "1";
    delBtn.addEventListener("click", wfDeleteCurrent);
  }
  const runBtn = $("#btn-wf-run");
  if (runBtn && !runBtn.dataset.hooked) {
    runBtn.dataset.hooked = "1";
    runBtn.addEventListener("click", wfRunCurrent);
  }
  const refresh = $("#btn-wf-refresh");
  if (refresh && !refresh.dataset.hooked) {
    refresh.dataset.hooked = "1";
    refresh.addEventListener("click", async () => {
      await Promise.all([wfLoadTools(), wfLoadDefinitions(true), wfLoadRuns(),
                         wfLoadSchedules()]);
      wfRenderAll();
    });
  }
  const runsRefresh = $("#btn-wf-runs-refresh");
  if (runsRefresh && !runsRefresh.dataset.hooked) {
    runsRefresh.dataset.hooked = "1";
    runsRefresh.addEventListener("click", async () => {
      await Promise.all([wfLoadRuns(), wfLoadSchedules()]);
      wfRenderRuns();
      wfRenderSchedules();
    });
  }
  const addStep = $("#btn-wf-add-step");
  if (addStep && !addStep.dataset.hooked) {
    addStep.dataset.hooked = "1";
    addStep.addEventListener("click", () => {
      if (!WFLOW.builder) return;
      const collected = wfCollectBuilder();
      WFLOW.builder.steps = collected.steps;
      const preferred = WFLOW.toolByName[collected.steps.length
        ? collected.steps[collected.steps.length - 1].tool : ""] ||
        WFLOW.toolList[0] || null;
      WFLOW.builder.steps.push(WM.newStepTemplate(preferred, collected.steps));
      wfRenderBuilderSteps();
      wfRenderGraph();
    });
  }
  const save = $("#btn-wf-save");
  if (save && !save.dataset.hooked) {
    save.dataset.hooked = "1";
    save.addEventListener("click", wfSaveBuilder);
  }
  const cancel = $("#btn-wf-cancel");
  if (cancel && !cancel.dataset.hooked) {
    cancel.dataset.hooked = "1";
    cancel.addEventListener("click", wfCloseBuilder);
  }
  const graph = $("#wf-graph");
  if (graph && !graph.dataset.hooked) {
    graph.dataset.hooked = "1";
    graph.addEventListener("click", (e) => {
      const node = e.target.closest(".wf-gnode");
      if (!node) return;
      WFLOW.nodeId = node.dataset.node;
      wfRenderGraph();
      wfRenderInspector();
    });
    graph.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      const node = e.target.closest(".wf-gnode");
      if (!node) return;
      e.preventDefault();
      WFLOW.nodeId = node.dataset.node;
      wfRenderGraph();
      wfRenderInspector();
    });
  }
  const runsList = $("#wf-runs");
  if (runsList && !runsList.dataset.hooked) {
    runsList.dataset.hooked = "1";
    runsList.addEventListener("click", (e) => {
      const row = e.target.closest(".wf-runrow");
      if (!row) return;
      WFLOW.viewRunId = Number(row.dataset.run);
      wfViewSet("workflows");
      wfRenderAll();
      const hint = $("#wf-def-hint");
      const run = WFLOW.runs.find((r) => Number(r.id) === Number(WFLOW.viewRunId));
      if (hint && run) {
        hint.textContent = `Showing run #${run.id} (${run.status}) on the graph.`;
      }
    });
  }
  const builderSteps = $("#wf-builder-steps");
  if (builderSteps && !builderSteps.dataset.hooked) {
    builderSteps.dataset.hooked = "1";
    builderSteps.addEventListener("input", () => {
      if (!WFLOW.builder) return;
      WFLOW.builder.steps = wfCollectBuilder().steps;
      if (WFLOW.edgeFrame) cancelAnimationFrame(WFLOW.edgeFrame);
      WFLOW.edgeFrame = requestAnimationFrame(() => { wfRenderGraph(); });
    });
    builderSteps.addEventListener("click", (e) => {
      const del2 = e.target.closest(".wf-bstep-del");
      if (!del2 || !WFLOW.builder) return;
      const idx = Number(del2.dataset.idx);
      WFLOW.builder.steps = wfCollectBuilder().steps.filter((_, i) => i !== idx);
      wfRenderBuilderSteps();
      wfRenderGraph();
    });
  }
  if (!window.__astraWfResize) {
    window.__astraWfResize = true;
    window.addEventListener("resize", () => {
      if (WFLOW.view === "workflows") wfDrawEdges();
    });
  }
}

/* ------------------------------------------------------------------ loader */
loaders.workflows = async function () {
  if (!WM) return;
  wfWire();
  wfRenderSrcMap();
  await ensureLiveFeed();
  if (!WFLOW.booted) {
    WFLOW.booted = true;
    const hist = await api("/api/events?limit=500");
    const rows = (hist && hist.ok && Array.isArray(hist.data)) ? hist.data : [];
    // /api/events is newest-first; the reducer wants chronological order.
    WFLOW.turns = WM.pipelineTurns(AstraLog.orderHistory(rows));
    rows.forEach((e) => { if (e && e.id != null) WFLOW.seen[String(e.id)] = 1; });
    WFLOW.turnIdx = WFLOW.turns.length - 1;
    await Promise.all([wfLoadTools(), wfLoadDefinitions(false), wfLoadRuns(),
                       wfLoadSchedules()]);
  } else {
    await Promise.all([wfLoadRuns(), wfLoadSchedules()]);
  }
  wfRenderPipeline();
  wfRenderAll();
  wfViewSet(WFLOW.view);
};

async function eventsPoll() {
  // Polling fallback for browsers without EventSource. Id-based dedupe in
  // receiveEvent() makes a repeated window harmless, so re-offer the tail.
  const r = await api("/api/events?limit=50");
  if (!r.ok) return;
  AstraLog.orderHistory(r.data || []).forEach((e) => receiveEvent(e));
}

// SSE connection state for the header badge (● LIVE / ○ RECONNECTING).
// Initialized at script load, before the Logs tab is ever opened.
let SSE_STATE = "reconnecting";
// One EventSource for the whole app (Logs + Agent Workflow). Two consumers
// used to mean two connections to an SSE endpoint that holds each one open;
// a single fan-out keeps an idle phone to one.
let SSE_ES = null;

function openSse(afterId) {
  if (SSE_ES) return SSE_ES;
  const url = afterId ? `/api/events/stream?after_id=${encodeURIComponent(afterId)}`
                       : "/api/events/stream";
  const es = new EventSource(url);
  SSE_ES = es;
  es.onopen = () => { SSE_STATE = "live"; refreshLiveState(); };
  es.onmessage = (ev) => {
    let e = {};
    try { e = JSON.parse(ev.data); } catch (_) { return; }
    receiveEvent(e);
  };
  es.onerror = () => {
    // EventSource auto-reconnects, resuming via Last-Event-ID — reflect that
    // in the header instead of looking dead.
    SSE_STATE = "reconnecting";
    refreshLiveState();
  };
  return es;
}

// Open the shared feed if nobody has yet (the Workflow tab may be the first
// tab opened). Resumes from the newest persisted id so nothing is replayed;
// duplicates are deduped by id on both consumers anyway.
async function ensureLiveFeed() {
  if (SSE_ES) return;
  if (!window.EventSource) { setInterval(eventsPoll, 3000); return; }
  let lastId = 0;
  try {
    const r = await api("/api/events/last");
    if (r && r.ok) lastId = (r.data || {}).last_id || 0;
  } catch (_) { /* connect from now */ }
  openSse(lastId);
}

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
  const type = (a.type || a.mime || "").split("/")[0];
  if (type === "image") {
    el.innerHTML = `<img class="artifact-image" src="${esc(url)}" alt="${esc(a.filename)}">
      <div class="artifact-label">${_fileIcon(a.filename)} ${esc(a.filename)}</div>`;
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
async function boot() {
  const r = await api("/api/manifest");
  if (!r.ok) { $("#netstatus").textContent = "✗"; return; }
  MANIFEST = r.data;
  document.title = MANIFEST.name;
  MANIFEST.tabs.forEach((t) => { TAB_LABELS[t.tab] = t.label; });

  // tab bar
  $("#nav").innerHTML = MANIFEST.tabs.map((t) =>
    `<button data-tab="${esc(t.tab)}" class="tab${t.tab === "dashboard" ? " active" : ""}">${t.label}</button>`).join("");
  $$("#nav .tab").forEach((t) =>
    t.addEventListener("click", () => showTab(t.dataset.tab)));

  // per-plugin tabview + load plugin JS
  const scriptLoads = [];
  MANIFEST.tabs.forEach((t) => {
    if (!t.plugin) return;
    const section = document.createElement("section");
    section.id = `tab-${t.plugin}`;
    section.className = "tabview";
    section.innerHTML = `<div class="empty">Loading ${esc(t.title)}…</div>`;
    $("#view").appendChild(section);
    scriptLoads.push(loadScript(t.js).then(() => {
      const def = Astra.plugins[t.plugin];
      loaders[t.plugin] = async () => {
        if (!def) return;
        if (!Astra.plugins[t.plugin].rendered) {
          Astra.plugins[t.plugin].rendered = true;
          if (def.render) def.render($("#tab-" + t.plugin));
        }
      };
    }));
  });
  await Promise.all(scriptLoads);

  // Reopen whichever tab was active before the last refresh, if it still
  // exists; otherwise fall back to Dashboard. showTab runs the tab's loader,
  // so calling loaders.dashboard() here as well fired two identical
  // /api/dashboard requests on every load.
  let initialTab = "dashboard";
  try {
    const saved = localStorage.getItem("astra:active-tab");
    if (saved && $("#tab-" + saved)) initialTab = saved;
  } catch (_) { /* ignore */ }
  showTab(initialTab);
  setInterval(() => {
    if ($("#tab-dashboard").classList.contains("active")) loaders.dashboard();
  }, 60_000);
}

function loadScript(src) {
  return new Promise((res, rej) => {
    const s = document.createElement("script");
    s.src = src; s.onload = res; s.onerror = rej;
    document.head.appendChild(s);
  });
}

boot();
