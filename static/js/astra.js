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
    AstraLog.orderHistory(rows).forEach((e) => appendEvent(e, true));
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
  if (m.status === "err") return "✖ " + (m.detail || "error");
  if (m.status === "warn") return "⚠ " + (m.detail || "warning");
  if (m.status === "running") return "… running";
  if (m.status === "ok") return "✓ " + (m.detail || "done");
  return m.detail ? "• " + m.detail : "•";
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
  const fields = [["Status", m.status]].concat(m.fields);
  if (m.duration) fields.push(["Duration", m.duration]);
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
function buildRow(m) {
  const row = document.createElement("div");
  row.className = "tl-row";
  row.dataset.category = m.category;
  row.dataset.status = m.status;
  row.dataset.id = m.id;
  row.dataset.text = m.search;
  row.dataset.cats = m.category + (m.status === "err" ? ",errors" : "");
  row.setAttribute("role", "button");
  row.tabIndex = 0;
  row.setAttribute("aria-expanded", "false");

  const ind = document.createElement("span");
  ind.className = "tl-ind";
  const dot = document.createElement("span");
  dot.className = "tl-dot " + m.status;
  ind.appendChild(dot);

  const time = document.createElement("span");
  time.className = "tl-time";
  time.textContent = m.time;

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

  const toggle = () => {
    const open = row.classList.toggle("open");
    detail.hidden = !open;
    row.setAttribute("aria-expanded", open ? "true" : "false");
  };
  row.addEventListener("click", toggle);
  row.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); }
  });
  return row;
}

// Append one event at the bottom, honouring follow/scroll and the DOM cap.
// `quiet` (history load / pause flush) skips the per-row scroll bookkeeping so
// a large batch is one layout pass, not hundreds.
function appendEvent(event, quiet) {
  if (!event || event.id == null) return;
  if (!isImportantEvent(event)) return;
  const key = String(event.id);
  if (LOGS.rendered.has(key)) return;   // SSE replay / poll overlap dedupe
  const feed = $("#live-feed");
  if (!feed) return;

  const m = AstraLog.normalize(event);
  AstraLog.markRendered(LOGS, event);
  if (feed.firstElementChild &&
      feed.firstElementChild.classList.contains("empty")) feed.innerHTML = "";

  const action = quiet ? { scrollToBottom: false, showIndicator: false }
                       : AstraLog.onAppend(LOGS, feedMetrics(), 1);
  feed.appendChild(buildRow(m));

  // bounded DOM: drop the oldest rows once over the cap (compensating the
  // reader's scroll position so their place does not jump).
  if (feed.children.length > LOGS_MAX_BUFFER) {
    const before = feed.scrollTop;
    const removedH = feed.firstElementChild.offsetHeight || 0;
    feed.removeChild(feed.firstElementChild);
    if (!LOGS.follow) feed.scrollTop = AstraLog.compensateTrim(before, removedH);
  }

  AstraLog.count(LOGS, m);
  renderLogStats();

  if (quiet) return;
  if (action.scrollToBottom) { feed.scrollTop = feed.scrollHeight; jumpToLatest(); }
  else showJump(LOGS.unread);
}

// Live arrival path: skip noise/dupes, render now, or buffer while paused.
function receiveEvent(event) {
  const verdict = AstraLog.admit(LOGS, event);
  if (verdict === "skip") return;
  if (verdict === "buffer") {
    AstraLog.buffer(LOGS, event);
    return;
  }
  appendEvent(event);
}

function flushPending() {
  const items = LOGS.pending;
  LOGS.pending = [];
  if (!items.length) return;
  items.forEach((e) => appendEvent(e, true));
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

function openSse(afterId) {
  const url = afterId ? `/api/events/stream?after_id=${encodeURIComponent(afterId)}`
                       : "/api/events/stream";
  const es = new EventSource(url);
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
