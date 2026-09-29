"use strict";
/* Web3 Center — dashboard model + renderer.
 *
 * Presentation only. Everything shown comes from endpoints that already exist
 * (/api/v1/web3/transaction-policy, /api/v1/web3/transactions, /api/tools).
 * Nothing here fetches, stores or renders secrets, and nothing is invented:
 * where the backend has no data (balances, prices, NFTs, DeFi, wallet
 * import/creation) the UI says so and the control is disabled.
 * Agent actions never execute directly: they prefill the chat input and the
 * user must press send; transfers stay gated by the transaction policy. */
(function (root) {
  // Static id -> label map mirroring astra/web3/chains.py (ids come from the
  // policy's chains_allowed; unknown ids still render as "Chain <id>").
  const CHAINS = {
    1: { n: "Ethereum", s: "ETH" }, 8453: { n: "Base", s: "ETH" },
    42161: { n: "Arbitrum", s: "ETH" }, 10: { n: "Optimism", s: "ETH" },
    137: { n: "Polygon", s: "POL" }, 56: { n: "BNB Chain", s: "BNB" },
    11155111: { n: "Sepolia", s: "ETH" },
  };
  const chainOf = (id) => CHAINS[id] || { n: "Chain " + id, s: "native" };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const short = (a) => (a && a.length > 12 ? a.slice(0, 6) + "…" + a.slice(-4) : a || "—");
  const nativeAmt = (wei) => {
    try { const w = BigInt(wei || 0), d = 10n ** 18n;
      return w / d + "." + (w % d).toString().padStart(18, "0").slice(0, 4);
    } catch (e) { return "—"; }
  };
  const limit = (n) => (n == null ? "—" : (Number(n) / 1e18).toFixed(4) + " ETH");
  const ymd = (d) => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  const ago = (ts, now) => {
    const t = Date.parse(String(ts || "").replace(" ", "T"));
    if (isNaN(t)) return "";
    const s = Math.max(0, Math.round(((now || new Date()).getTime() - t) / 1000));
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago";
    return Math.floor(s / 86400) + " d ago";
  };
  const TONE = { CONFIRMED: "good", FAILED: "bad", REJECTED: "bad", CANCELLED: "bad" };

  // Agent actions = the Web3 tools actually registered (category "web3").
  const ACTIONS = {
    token_balance: ["Check Balance", "Native / ERC-20 balance", (c) => `Check the token balance of ${c.address || "<address>"} on ${c.network || "<network>"}.`],
    chain_status: ["Chain Status", "Block height & RPC latency", (c) => `Show the current chain status for ${c.network || "Ethereum"}.`],
    rpc_status: ["RPC Health", "Check failover RPC endpoints", (c) => `Check RPC status for ${c.network || "Ethereum"}.`],
    tx_prepare: ["Prepare Transfer", "Policy-gated send", (c) => `Prepare a transaction from ${c.address || "<wallet>"} to <recipient address> for <amount> on ${c.network || "<network>"}. Do not proceed without approval under the transaction policy.`],
    tx_status: ["Transaction Status", "Look up a prepared tx", () => "Check the status of transaction <tx_id>."],
  };

  function activitySeries(list, days, now) {
    const n = now || new Date(), out = [];
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date(n.getFullYear(), n.getMonth(), n.getDate() - i);
      out.push({ day: ymd(d), count: 0 });
    }
    const idx = Object.fromEntries(out.map((p, i) => [p.day, i]));
    (list || []).forEach((t) => { const k = String(t.created_at || "").slice(0, 10); if (k in idx) out[idx[k]].count++; });
    return out;
  }
  function chartPaths(series, w, h) {
    const max = Math.max(1, ...series.map((p) => p.count)), pad = 6;
    const pts = series.map((p, i) => [series.length < 2 ? 0 : (i / (series.length - 1)) * w,
      h - pad - (p.count / max) * (h - pad * 2)]);
    const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    return { line, area: line + ` L${w} ${h} L0 ${h} Z` };
  }

  function buildModel(pol, txs, tools, now) {
    const okp = !!(pol && pol.ok && pol.data), policy = okp ? pol.data.policy || {} : {};
    const list = (txs && txs.ok && txs.data && txs.data.transactions) || [];
    const by = (txs && txs.ok && txs.data && txs.data.stats && txs.data.stats.by_status) || {};
    const reg = new Set(((tools && tools.ok && tools.data && tools.data.tools) || [])
      .filter((t) => t.category === "web3").map((t) => t.name));
    return {
      available: okp, mode: okp ? pol.data.mode : "", stopped: okp && !!pol.data.stopped, policy,
      wallets: (policy.wallets_allowed || []).map((a) => ({ address: a })),
      chains: (policy.chains_allowed || []).map((id) => ({ id, name: chainOf(id).n, symbol: chainOf(id).s })),
      txs: list.map((t) => ({ id: String(t.tx_id || ""), status: t.status || "?", to: t.to || "",
        chainId: t.chain_id, network: chainOf(t.chain_id).n, symbol: chainOf(t.chain_id).s,
        amount: nativeAmt(t.value_wei), error: t.error || "", at: t.created_at || "" })),
      total: Object.values(by).reduce((a, b) => a + Number(b || 0), 0) || list.length,
      awaiting: Number(by.PREPARED || 0),
      activity: activitySeries(list, 14, now),
      actions: Object.keys(ACTIONS).filter((k) => reg.has(k)),
      now: now || new Date(),
    };
  }

  const btnOff = (label, why, cls) => `<button class="w3-btn ${cls || ""}" disabled title="${esc(why)}">${label}</button>`;
  const NOBE = "Not available: the backend has no wallet registry yet";
  const empty = (t) => `<div class="w3-empty">${t}</div>`;

  function walletsHtml(m, S) {
    const q = S.q.trim().toLowerCase();
    const ws = m.wallets.filter((w) => !q || w.address.toLowerCase().includes(q));
    if (!m.wallets.length) return empty("No wallets are registered with the transaction policy. Wallet import and creation aren’t available in this build — the backend has no wallet registry endpoint.");
    if (!ws.length) return empty("No wallets match your search.");
    return ws.map((w) => `<button class="w3-wallet${w.address === S.sel ? " sel" : ""}" data-wsel="${esc(w.address)}" title="${esc(w.address)}">
      <span class="w3-av">${esc(w.address.slice(2, 4).toUpperCase())}</span>
      <span class="w3-wname">${esc(short(w.address))}<small>Allowlisted wallet</small></span>
      <span class="w3-wval">—<small>balance not indexed</small></span></button>`).join("");
  }

  function html(m, S) {
    const sel = m.wallets.find((w) => w.address === S.sel) || m.wallets[0] || null;
    const net = m.chains.find((c) => String(c.id) === S.net);
    const ctx = { address: sel && sel.address, network: net && net.name };
    const txs = m.txs.filter((t) => S.net === "all" || String(t.chainId) === S.net);
    const shown = S.all ? txs : txs.slice(0, 6);
    const chart = chartPaths(m.activity, 600, 110), active = m.activity.some((p) => p.count);
    const netOpts = `<option value="all">All networks</option>` + m.chains.map((c) => `<option value="${c.id}"${String(c.id) === S.net ? " selected" : ""}>${esc(c.name)}</option>`).join("");
    const tabs = [["overview", "Overview"], ["wallets", "Wallets"], ["assets", "Assets"], ["transactions", "Transactions"], ["networks", "Networks"], ["agents", "Agent Actions"]];
    const p = m.policy;
    return `<div class="w3" data-view="${esc(S.view)}">
<div class="w3-head"><div><h2>⛓️ Web3 Center</h2><p>Manage wallets, assets, networks and Web3 agent actions</p></div>
<div class="w3-acts">${btnOff("Import Wallet", NOBE, "primary")}${btnOff("Create Wallet", NOBE)}${btnOff("Manage Groups", NOBE)}</div></div>
<nav class="w3-tabs">${tabs.map((t) => `<button class="w3-tab${S.view === t[0] ? " on" : ""}" data-view="${t[0]}">${t[1]}</button>`).join("")}</nav>
<div class="w3-grid"><div class="w3-main">
<section class="w3-card w3-portfolio" data-sec="overview"><div class="w3-pf-l"><h3>Total Portfolio Value</h3><div class="w3-big">—</div>
<p class="w3-note">Balances unavailable — no wallet registry is connected to the backend yet.</p>
<div class="w3-chart-h">Transaction activity · last 14 days</div>
<svg viewBox="0 0 600 110" preserveAspectRatio="none" class="w3-chart" role="img" aria-label="Transactions per day"><defs><linearGradient id="w3g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#7c5cff" stop-opacity=".45"/><stop offset="1" stop-color="#7c5cff" stop-opacity="0"/></linearGradient></defs>
<path d="${chart.area}" fill="url(#w3g)"/><path d="${chart.line}" fill="none" stroke="#8b7bff" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>
${active ? "" : `<p class="w3-note">No transactions in this period.</p>`}</div>
<div class="w3-stats">${[["Networks", m.chains.length], ["Allowed wallets", m.wallets.length], ["Transactions", m.total], ["Awaiting approval", m.awaiting]].map((s) => `<div class="w3-stat"><small>${s[0]}</small><b>${s[1]}</b></div>`).join("")}</div></section>
<section class="w3-card" data-sec="overview wallets"><div class="w3-row"><h3>Wallets <span class="w3-dim">(${m.wallets.length})</span></h3>
<div class="w3-tools"><input data-q class="w3-in" placeholder="Search wallets…" value="${esc(S.q)}" aria-label="Search wallets">
<button class="w3-ico${S.layout === "grid" ? " on" : ""}" data-layout="grid" title="Grid">▦</button><button class="w3-ico${S.layout === "list" ? " on" : ""}" data-layout="list" title="List">☰</button>
${btnOff("+ Add Wallet", NOBE, "primary")}</div></div>
<div id="w3-wallets" class="w3-wallets ${S.layout}">${walletsHtml(m, S)}</div></section>
<div class="w3-two"><section class="w3-card" data-sec="overview assets"><div class="w3-row"><h3>Assets</h3><div class="w3-tools"><span class="w3-pill on">Tokens</span><select class="w3-in" data-net aria-label="Network filter">${netOpts}</select></div></div>
<div class="w3-thead"><span>Token</span><span>Balance</span><span>Price</span><span>Value</span><span>24h</span></div>
${empty(`Token balances, prices and NFT/DeFi positions aren’t indexed by the backend yet.${m.actions.includes("token_balance") ? `<br><button class="w3-btn" data-act="token_balance">Ask the agent for a balance</button>` : ""}`)}</section>
<section class="w3-card" data-sec="overview transactions"><div class="w3-row"><h3>Recent Transactions</h3>${txs.length > 6 ? `<button class="w3-btn" data-all>${S.all ? "Show less" : "View All"}</button>` : ""}</div>
<div id="web3-txs" class="w3-txs">${shown.map((t) => `<div class="w3-tx" title="${esc(t.error || t.id)}"><span class="w3-chain">${esc(t.network.charAt(0))}</span>
<span class="w3-tm">To ${esc(short(t.to))}<small>${esc(t.network)} · ${esc(ago(t.at, m.now))}</small></span>
<span class="w3-ta">−${esc(t.amount)} ${esc(t.symbol)}<em class="w3-st ${TONE[t.status] || "warn"}">${esc(t.status)}</em></span></div>`).join("") || empty("No transactions yet.")}</div></section></div></div>
<aside class="w3-side">
<section class="w3-card" data-sec="overview wallets assets"><h3>Active Wallet</h3>${sel ? `<div class="w3-active"><span class="w3-av lg">${esc(sel.address.slice(2, 4).toUpperCase())}</span>
<div><b title="${esc(sel.address)}">${esc(short(sel.address))}</b> <button class="w3-ico" data-copy="${esc(sel.address)}" title="Copy address">⧉</button><small>Allowlisted wallet · value not indexed</small></div></div>` : empty("No wallet selected.")}
<div class="w3-qa">${m.actions.includes("tx_prepare") && sel ? `<button class="w3-btn" data-act="tx_prepare">Send</button>` : btnOff("Send", "Requires a wallet and the tx_prepare tool")}
${sel ? `<button class="w3-btn" data-copy="${esc(sel.address)}">Receive</button>` : btnOff("Receive", "No wallet selected")}
${m.actions.includes("token_balance") && sel ? `<button class="w3-btn" data-act="token_balance">Balance</button>` : btnOff("Balance", "Requires a wallet and the token_balance tool")}
${["Swap", "Bridge", "Buy", "Sell", "Stake"].map((a) => btnOff(a, "Not supported by the backend yet")).join("")}</div></section>
<section class="w3-card" data-sec="overview networks"><h3>Networks</h3>${m.chains.map((c) => `<button class="w3-net${String(c.id) === S.net ? " sel" : ""}" data-netpick="${c.id}"><span class="w3-chain">${esc(c.name.charAt(0))}</span><span>${esc(c.name)}<small>${esc(c.symbol)} · chain ${c.id}</small></span><em class="w3-st good">Allowed</em></button>`).join("") || empty("No networks reported by the policy.")}</section>
<section class="w3-card" data-sec="overview agents"><h3>Agent Web3 Actions</h3><p class="w3-note">Opens Chat with a prepared prompt. Nothing runs until you send it; transfers stay gated by the transaction policy.</p>
<div class="w3-actions">${m.actions.map((k) => `<button class="w3-action" data-act="${k}"><b>${esc(ACTIONS[k][0])}</b><small>${esc(ACTIONS[k][1])}</small></button>`).join("") || empty("No Web3 tools are registered.")}</div></section>
<section class="w3-card" data-sec="overview agents transactions"><h3>Safety Policy</h3>
<div id="web3-policy" class="table">${m.available ? `<div class="row"><b>Mode</b><span>${esc(m.mode)} (CONFIRM = review, AUTO = policy-approved)</span></div>
<div class="row"><b>Status</b><span>${m.stopped ? "🚨 EMERGENCY STOP" : "running"}</span></div>
<div class="row"><b>Max per tx</b><span>${esc(limit(p.tx_limit_wei))}</span></div><div class="row"><b>Max daily</b><span>${esc(limit(p.daily_limit_wei))}</span></div>
<div class="row"><b>Allowlist</b><span>${(p.recipients_allowed || []).length} recipients · ${(p.contracts_allowed || []).length} contracts · ${(p.wallets_allowed || []).length} wallets</span></div>` : `<span class="muted">Web3 unavailable</span>`}</div></section>
</aside></div><div id="w3-toast" class="w3-toast" role="status"></div></div>`;
  }

  // ---- DOM binding (browser only) -----------------------------------------
  const S = { view: "overview", q: "", layout: "grid", sel: null, net: "all", all: false };
  let M = null, hooks = {}, el = null;
  const toast = (t) => { const n = el && el.querySelector("#w3-toast"); if (!n) return; n.textContent = t; n.classList.add("on"); setTimeout(() => n.classList.remove("on"), 2000); };
  function paint() { if (el && M) el.innerHTML = html(M, S); }
  function render(target, model, h) {
    el = target; M = model; hooks = h || {};
    if (!S.sel || !M.wallets.some((w) => w.address === S.sel)) S.sel = M.wallets[0] ? M.wallets[0].address : null;
    if (!el.__w3) {
      el.__w3 = true;
      el.addEventListener("click", (e) => {
        const g = (s) => e.target.closest(s);
        let x;
        if ((x = g(".w3-tab[data-view]"))) { S.view = x.dataset.view; return paint(); } // NOT bare [data-view]: the container carries it too
        if ((x = g("[data-wsel]"))) { S.sel = x.dataset.wsel; return paint(); }
        if ((x = g("[data-layout]"))) { S.layout = x.dataset.layout; return paint(); }
        if ((x = g("[data-netpick]"))) { S.net = S.net === x.dataset.netpick ? "all" : x.dataset.netpick; return paint(); }
        if (g("[data-all]")) { S.all = !S.all; return paint(); }
        if ((x = g("[data-copy]"))) {
          const a = x.dataset.copy;
          (navigator.clipboard ? navigator.clipboard.writeText(a) : Promise.reject()).then(() => toast("Address copied"), () => toast("Copy failed — select the address manually"));
          return;
        }
        if ((x = g("[data-act]")) && hooks.toChat) {
          const net = M.chains.find((c) => String(c.id) === S.net);
          hooks.toChat(ACTIONS[x.dataset.act][2]({ address: S.sel, network: net && net.name }));
        }
      });
      el.addEventListener("input", (e) => {
        if (!e.target.matches("[data-q]")) return;
        S.q = e.target.value;
        const w = el.querySelector("#w3-wallets"); if (w) w.innerHTML = walletsHtml(M, S);
      });
      el.addEventListener("change", (e) => { if (e.target.matches("[data-net]")) { S.net = e.target.value; paint(); } });
    }
    paint();
  }

  const api = { buildModel, html, walletsHtml, activitySeries, chartPaths, nativeAmt, short, render, ACTIONS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.Web3Center = api;
})(typeof window !== "undefined" ? window : globalThis);
