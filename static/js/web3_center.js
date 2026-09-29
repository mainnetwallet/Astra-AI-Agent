"use strict";
/* Web3 Center — dashboard model + renderer.
 *
 * Presentation only. Everything shown comes from backend endpoints
 * (/api/v1/web3/transaction-policy, /transactions, /wallets, /api/tools) —
 * the wallet registry (/api/v1/web3/wallets) is the single source of wallet
 * state; nothing is duplicated or persisted here. Secrets: a pasted private
 * key lives only in the import textarea/memory until the request completes;
 * a newly created wallet's key is shown once and dropped from memory when
 * the dialog closes. Nothing is ever written to localStorage or the DOM
 * beyond that one-time view. Where the backend has no data (balances,
 * prices, NFTs, DeFi) the UI says so and the control is disabled.
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

  function mapRegistry(d) {
    d = d || {};
    return {
      wallets: (d.wallets || []).map((w) => ({ id: w.id, address: String(w.address || "").toLowerCase(),
        name: w.name || "", source: w.source || "imported", canSign: w.can_sign !== false,
        active: !!w.active, groupIds: w.group_ids || [] })),
      groups: (d.groups || []).map((g) => ({ id: g.id, name: g.name, walletIds: g.wallet_ids || [],
        count: g.count != null ? g.count : (g.wallet_ids || []).length })),
      activeAddress: d.active_address || null,
    };
  }

  function buildModel(pol, txs, tools, now, wal) {
    const regOk = !!(wal && wal.ok && wal.data), R = mapRegistry(regOk ? wal.data : {});
    const okp = !!(pol && pol.ok && pol.data), policy = okp ? pol.data.policy || {} : {};
    const list = (txs && txs.ok && txs.data && txs.data.transactions) || [];
    const by = (txs && txs.ok && txs.data && txs.data.stats && txs.data.stats.by_status) || {};
    const reg = new Set(((tools && tools.ok && tools.data && tools.data.tools) || [])
      .filter((t) => t.category === "web3").map((t) => t.name));
    return {
      available: okp, mode: okp ? pol.data.mode : "", stopped: okp && !!pol.data.stopped, policy,
      registry: regOk, wallets: R.wallets, groups: R.groups, activeAddress: R.activeAddress,
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

  const btnOff = (label, why, cls, ic) => `<button class="w3-btn ${cls || ""}${ic ? ` ic-${ic}` : ""}" disabled title="${esc(why)}">${label}</button>`;
  const NOBE = "Not available: the wallet registry is unavailable";
  const SRC = { imported: "Imported wallet", created: "Created wallet", watch: "Watch-only" };
  const srcLabel = (w) => SRC[w.source] || "Wallet";
  const actBtn = (m, label, modal, cls, ic) => (m.registry
    ? `<button class="w3-btn ${cls || ""}${ic ? ` ic-${ic}` : ""}" data-modal="${modal}">${label}</button>`
    : btnOff(label, NOBE, cls, ic));
  const groupSel = (m, S) => (m.registry
    ? `<select class="w3-in" data-grp aria-label="Wallet group"><option value="">All Groups</option>${m.groups.map((g) => `<option value="${esc(g.id)}"${S.grp === g.id ? " selected" : ""}>${esc(g.name)} (${g.count})</option>`).join("")}</select>`
    : `<select class="w3-in" disabled title="${esc(NOBE)}" aria-label="Wallet group"><option>All Groups</option></select>`);
  const NOSUP = "Not supported by the backend yet";
  const empty = (t) => `<div class="w3-empty">${t}</div>`;
  const RANGES = { "1D": 1, "1W": 7, "1M": 30, "3M": 90, "1Y": 365 };
  const AICON = { token_balance: "◎", chain_status: "◈", rpc_status: "⌁", tx_prepare: "↗", tx_status: "☰" };
  const UNSUP = [["Swap Tokens", "Swap via DEX", "⇄"], ["Contract Interaction", "Interact with contracts", "▤"],
    ["Bridge Assets", "Cross-chain bridge", "⛓"], ["Stake / DeFi", "Stake & earn", "✦"]];

  function hourSeries(list, now) {
    const n = now || new Date(), out = [];
    for (let i = 23; i >= 0; i--) {
      const d = new Date(n.getFullYear(), n.getMonth(), n.getDate(), n.getHours() - i);
      out.push({ day: ymd(d) + " " + String(d.getHours()).padStart(2, "0"), count: 0 });
    }
    const idx = Object.fromEntries(out.map((p, i) => [p.day, i]));
    (list || []).forEach((t) => { const k = String(t.created_at || "").replace("T", " ").slice(0, 13); if (k in idx) out[idx[k]].count++; });
    return out;
  }
  const seriesFor = (list, range, now) => (range === "1D" ? hourSeries(list, now) : activitySeries(list, RANGES[range] || 7, now));

  const chainDots = (m, max) => {
    const cs = m.chains.slice(0, max || 3);
    return cs.map((c) => `<i class="w3-chain sm" title="${esc(c.name)}">${esc(c.name.charAt(0))}</i>`).join("") +
      (m.chains.length > cs.length ? `<em class="w3-more">+${m.chains.length - cs.length}</em>` : "");
  };

  // A group is open when the user toggled it; otherwise it is collapsed
  // (a search or an explicit group filter opens the matching groups).
  const groupOpen = (S, gid) => {
    const o = S.open || {};
    return gid in o ? !!o[gid] : !!(S.q.trim() || S.grp);
  };

  // Astra Wallets: standalone wallets (no group) as cards, then one collapsible
  // header per group. The serial numbers are generated here from the rendered
  // order — they are never stored as wallet data.
  function walletsHtml(m, S) {
    const q = S.q.trim().toLowerCase();
    if (!m.registry) return empty("The wallet registry is unavailable, so wallets can’t be imported or created.");
    const add = `<button class="w3-btn w3-addw" data-modal="import"><b>+</b>Add Wallet</button>`;
    if (!m.wallets.length) return empty("No wallets yet. Import or create one to get started.") + add;
    const ws = m.wallets.filter((w) => (!q || w.address.includes(q) || w.name.toLowerCase().includes(q)) &&
      (!S.grp || w.groupIds.includes(S.grp)));
    if (!ws.length) return empty("No wallets match your search or group filter.");
    const on = S.sel || m.activeAddress || (m.wallets[0] && m.wallets[0].address);
    const inGroup = new Set(m.groups.map((g) => g.id));
    const gname = (w) => w.groupIds.map((id) => (m.groups.find((g) => g.id === id) || {}).name).filter(Boolean).join(", ");
    const card = (w) => `<button class="w3-wallet${w.address === on ? " sel" : ""}" data-wsel="${esc(w.address)}" title="${esc(w.address + (gname(w) ? " · " + gname(w) : ""))}">
      <span class="w3-wtop"><span class="w3-av">${esc(w.address.slice(2, 4).toUpperCase())}</span>
      <span class="w3-wname">${esc(w.name || short(w.address))}<small>${esc(short(w.address))} · ${esc(srcLabel(w))}</small></span>${w.address === on ? `<i class="w3-dot" title="Active"></i>` : ""}</span>
      <span class="w3-wval">—<small>balance not indexed</small></span>
      <span class="w3-spark" aria-hidden="true"></span>
      <span class="w3-wchains">${chainDots(m, 3)}</span></button>`;
    const solo = ws.filter((w) => !w.groupIds.some((id) => inGroup.has(id)));
    const shown = new Map(ws.map((w) => [w.id, w]));
    const groups = m.groups.map((g) => {
      const ids = g.walletIds.concat(ws.filter((w) => w.groupIds.includes(g.id) && !g.walletIds.includes(w.id)).map((w) => w.id));
      return { g, members: ids.map((id) => shown.get(id)).filter(Boolean) };
    }).filter((x) => x.members.length || (!q && !S.grp));
    const grp = ({ g, members }) => {
      const open = groupOpen(S, g.id);
      return `<div class="w3-group${open ? " open" : ""}" data-group="${esc(g.id)}">
      <button class="w3-ghead" data-gtoggle="${esc(g.id)}" aria-expanded="${open}"><b>${esc(g.name)}</b><small>${plural(g.count, "wallet")}</small><i aria-hidden="true">${open ? "▲" : "▼"}</i></button>
      ${open ? `<div class="w3-gbody">${members.map((w, i) => `<button class="w3-wallet w3-gitem${w.address === on ? " sel" : ""}" data-wsel="${esc(w.address)}" title="${esc(w.address)}"><span class="w3-gnum">${i + 1}.</span>
      <span class="w3-wname">${esc(w.name || short(w.address))}<small>${esc(short(w.address))}</small></span>${w.address === on ? `<i class="w3-dot" title="Active"></i>` : ""}</button>`).join("") || empty("No wallets in this group yet.")}</div>` : ""}</div>`;
    };
    return solo.map(card).join("") + groups.map(grp).join("") + add;
  }

  // ---- modals (Import Wallet / Create Wallet / Astra Wallets) -------------
  const IDLE = () => ({ kind: null, text: "", name: "", group: "", results: null, busy: false, err: "",
    secret: null, created: null, ack: false, gsel: null, gnew: "", gname: null, confirmDel: false, impGroup: "" });
  const gopts = (m, sel) => `<option value="">No group</option>` + m.groups.map((g) => `<option value="${esc(g.id)}"${sel === g.id ? " selected" : ""}>${esc(g.name)}</option>`).join("");
  const plural = (n, w) => n + " " + w + (n === 1 ? "" : "s");

  // Non-blank, non-comment lines = wallets in this import. One → standalone
  // wallet; two or more → one group, which needs a name.
  const importCount = (t) => String(t || "").split(/\r?\n/).filter((l) => l.trim() && !l.trim().startsWith("#")).length;
  const importNeedsGroup = (X) => importCount(X.text) >= 2;
  const importBlocked = (X) => X.busy || !X.text.trim() || (importNeedsGroup(X) && !X.impGroup.trim());

  function importBody(m, X) {
    const rs = X.results;
    const rows = rs ? `<div class="w3-mres">${rs.results.map((r) => `<div class="w3-mrow ${r.status === "valid" ? "ok" : "bad"}"><i>${r.status === "valid" ? "✓" : "✕"}</i><span>Line ${r.line}${r.address ? ` · ${esc(short(r.address))}` : ""}<small>${esc(r.status === "valid" ? (rs.dry_run ? "valid — ready to import" : "imported") : (r.reason || r.status))}</small></span></div>`).join("")}</div>
      <p class="w3-msum">${rs.dry_run ? `${rs.valid} valid · ${rs.rejected} will be rejected` : `${plural(rs.imported, "wallet")} imported · ${rs.rejected} rejected`}</p>${rs.group ? `<p class="w3-msum">Group “${esc(rs.group.name)}” created</p>` : ""}` : "";
    return `<p class="w3-note">One wallet per line: a private key (<code>64 hex</code>, optional <code>0x</code>), <code>name key</code>, or a <code>0x…</code> address for a watch-only wallet. Keys are encrypted at rest and never sent to the AI. Seed phrases and JSON keystores aren’t supported.</p>
      <textarea class="w3-in w3-mta" data-mtext rows="6" spellcheck="false" autocomplete="off" autocapitalize="off" aria-label="Wallets to import" placeholder="One wallet per line">${esc(X.text)}</textarea>
      <div class="w3-mform" data-mgwrap${importNeedsGroup(X) ? "" : " hidden"}><label>Group Name<input class="w3-in" data-mgname maxlength="40" value="${esc(X.impGroup)}" placeholder="e.g. Binance Wallets" aria-label="Group Name" autocomplete="off"></label>
      <small class="w3-dim">You’re importing more than one wallet, so they’re saved together as one group.</small></div>
      ${X.err ? `<p class="w3-merr">${esc(X.err)}</p>` : ""}${rows}
      <div class="w3-mfoot"><button class="w3-btn" data-mvalidate${X.busy || !X.text.trim() ? " disabled" : ""}>Validate</button>
      <button class="w3-btn primary" data-mimport${importBlocked(X) ? " disabled" : ""}>${X.busy ? "Working…" : "Import"}</button></div>`;
  }

  function createBody(m, X) {
    if (X.secret) {
      const c = X.created || {};
      return `<p class="w3-note">Wallet created. <b>Save this private key now</b> — it is shown only once and can’t be recovered. Anyone with it controls the wallet.</p>
        <div class="w3-mkv"><span>Name</span><b>${esc(c.name || "")}</b><span>Address</span><b title="${esc(c.address || "")}">${esc(c.address || "")}</b></div>
        <code class="w3-msecret" data-msecret>${esc(X.secret)}</code>
        <div class="w3-mfoot"><button class="w3-btn" data-mcopysecret>Copy key</button>${c.address && c.address === m.activeAddress ? `<em class="w3-st good">Active wallet</em>` : `<button class="w3-btn" data-msetactive>Make active</button>`}</div>
        <label class="w3-mack"><input type="checkbox" data-mack${X.ack ? " checked" : ""}> I have saved my private key somewhere safe</label>
        ${X.err ? `<p class="w3-merr">${esc(X.err)}</p>` : ""}
        <div class="w3-mfoot"><button class="w3-btn primary" data-mdone${X.ack ? "" : " disabled"}>Done</button></div>`;
    }
    return `<p class="w3-note">Generates a new wallet with a secure random key. The key is encrypted at rest; you’ll see it once so you can back it up.</p>
      <div class="w3-mform"><label>Wallet name (optional)<input class="w3-in" data-mname maxlength="40" value="${esc(X.name)}" placeholder="e.g. Trading"></label>
      <label>Add to group<select class="w3-in" data-mgroup>${gopts(m, X.group)}</select></label></div>
      ${X.err ? `<p class="w3-merr">${esc(X.err)}</p>` : ""}
      <div class="w3-mfoot"><button class="w3-btn primary" data-mcreate${X.busy ? " disabled" : ""}>${X.busy ? "Creating…" : "Create Wallet"}</button></div>`;
  }

  function groupsBody(m, X) {
    const sel = m.groups.find((g) => g.id === X.gsel) || m.groups[0] || null;
    const mem = sel ? m.wallets.filter((w) => w.groupIds.includes(sel.id)) : [];
    const rest = sel ? m.wallets.filter((w) => !w.groupIds.includes(sel.id)) : [];
    const others = sel ? m.groups.filter((g) => g.id !== sel.id) : [];
    const wrow = (w, ctl) => `<div class="w3-mrow"><span>${esc(w.name || short(w.address))}<small>${esc(short(w.address))}</small></span>${ctl}</div>`;
    return `<div class="w3-gm"><div class="w3-gl">
      ${m.groups.map((g) => `<button class="w3-grow${sel && g.id === sel.id ? " on" : ""}" data-gsel="${esc(g.id)}"><b>${esc(g.name)}</b><small>${plural(g.count, "wallet")}</small></button>`).join("") || empty("No groups yet.")}
      <div class="w3-gnew"><input class="w3-in" data-gnew maxlength="40" placeholder="New group name" value="${esc(X.gnew)}" aria-label="New group name"><button class="w3-btn primary" data-gcreate>Create</button></div></div>
      <div class="w3-gr">${sel ? `<div class="w3-gren"><input class="w3-in" data-gname maxlength="40" value="${esc(X.gname == null ? sel.name : X.gname)}" aria-label="Group name"><button class="w3-btn" data-grename>Rename</button><button class="w3-btn${X.confirmDel ? " primary" : ""}" data-gdel>${X.confirmDel ? "Confirm delete" : "Delete"}</button></div>
      <h4>Wallets in ${esc(sel.name)} <span class="w3-dim">(${mem.length})</span></h4>
      ${mem.map((w) => wrow(w, `<span class="w3-mact">${others.length ? `<select class="w3-in" data-gmove="${esc(w.id)}" aria-label="Move ${esc(w.name)}"><option value="">Move to…</option>${others.map((g) => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join("")}</select>` : ""}<button class="w3-btn" data-gremove="${esc(w.id)}">Remove</button></span>`)).join("") || empty("No wallets in this group.")}
      <h4>Add wallets</h4>
      ${rest.map((w) => wrow(w, `<button class="w3-btn" data-gadd="${esc(w.id)}">Add</button>`)).join("") || empty(m.wallets.length ? "Every wallet is already in this group." : "No wallets registered yet.")}` : empty("Create a group to start organising wallets.")}</div></div>
      ${X.err ? `<p class="w3-merr">${esc(X.err)}</p>` : ""}`;
  }

  function modalHtml(m, X) {
    if (!X || !X.kind || !m.registry) return "";
    const T = { import: ["Import Wallet", importBody], create: ["Create Wallet", createBody], groups: ["Astra Wallets", groupsBody] }[X.kind];
    if (!T) return "";
    return `<div class="w3-modal" data-mbg><div class="w3-mbox${X.kind === "groups" ? " wide" : ""}" role="dialog" aria-modal="true" aria-label="${esc(T[0])}">
      <div class="w3-mhead"><h3>${esc(T[0])}</h3><button class="w3-ico flat" data-mclose title="Close" aria-label="Close">✕</button></div>${T[1](m, X)}</div></div>`;
  }

  function html(m, S, X) {
    const sel = m.wallets.find((w) => w.address === S.sel) || m.wallets.find((w) => w.address === m.activeAddress) || m.wallets[0] || null;
    const net = m.chains.find((c) => String(c.id) === S.net);
    const ctx = { address: sel && sel.address, network: net && net.name };
    const txs = m.txs.filter((t) => S.net === "all" || String(t.chainId) === S.net);
    const shown = S.all ? txs : txs.slice(0, 6);
    const range = RANGES[S.range] ? S.range : "1W";
    const series = seriesFor(m.txs.map((t) => ({ created_at: t.at })), range, m.now);
    const chart = chartPaths(series, 600, 120), active = series.some((p) => p.count);
    const atab = S.view === "nfts" || S.view === "defi" ? S.view : S.atab || "tokens";
    const chainsShown = m.chains.filter((c) => S.net === "all" || String(c.id) === S.net);
    const netOpts = `<option value="all">All networks</option>` + m.chains.map((c) => `<option value="${c.id}"${String(c.id) === S.net ? " selected" : ""}>${esc(c.name)}</option>`).join("");
    const tabs = [["overview", "Overview"], ["wallets", "Wallets"], ["assets", "Assets"], ["transactions", "Transactions"], ["defi", "DeFi"], ["nfts", "NFTs"], ["networks", "Networks"], ["agents", "Agent Actions"]];
    const TI = { overview: "◧", wallets: "▣", assets: "⇅", transactions: "☷", defi: "◎", nfts: "◍", networks: "⛓", agents: "☺" };
    const p = m.policy;
    const vis = (s) => S.view === "overview" || s.split(" ").includes(S.view);
    const sec = (s) => `data-sec="${s}"${vis(s) ? "" : " hidden"}`;
    const twoHidden = !vis("assets defi nfts") && !vis("transactions");
    const assetsBody = atab === "nfts" || atab === "defi"
      ? empty(`${atab === "nfts" ? "NFT holdings" : "DeFi positions"} aren’t indexed by the backend yet.`)
      : (chainsShown.length ? chainsShown.map((c) => `<div class="w3-trow"><span class="w3-tk"><i class="w3-chain sm">${esc(c.name.charAt(0))}</i><b>${esc(c.name)}</b><small>${esc(c.symbol)}</small></span><span>—</span><span>—</span><span>—</span><span>—</span></div>`).join("") : "") +
        empty(`Token balances, prices and NFT/DeFi positions aren’t indexed by the backend yet.${m.actions.includes("token_balance") ? `<br><button class="w3-btn" data-act="token_balance">Ask the agent for a balance</button>` : ""}`);
    const actCards = m.actions.map((k) => `<button class="w3-action" data-act="${k}"><i class="w3-aic">${AICON[k] || "•"}</i><span><b>${esc(ACTIONS[k][0])}</b><small>${esc(ACTIONS[k][1])}</small></span></button>`).join("") +
      (m.actions.length ? UNSUP.slice(0, S.view === "agents" ? 4 : Math.max(0, 6 - m.actions.length)).map((u) => `<button class="w3-action" disabled title="${esc(NOSUP)}"><i class="w3-aic">${u[2]}</i><span><b>${esc(u[0])}</b><small>${esc(u[1])}</small></span></button>`).join("") : "");
    return `<div class="w3" data-view="${esc(S.view)}"><div class="w3-main">
<div class="w3-head"><span class="w3-logo">⛓️</span><div class="w3-ht"><h2>Web3 Center</h2><p>Manage wallets, assets, networks and Web3 agent actions</p></div>
<div class="w3-acts">${actBtn(m, "Import Wallet", "import", "primary", "imp")}${actBtn(m, "Create Wallet", "create", "", "cre")}${actBtn(m, "Astra Wallets", "groups", "", "grp")}</div></div>
<nav class="w3-tabs">${tabs.map((t) => `<button class="w3-tab${S.view === t[0] ? " on" : ""}" data-view="${t[0]}"><i>${TI[t[0]]}</i>${t[1]}</button>`).join("")}</nav>
<section class="w3-card w3-portfolio" ${sec("overview")}><div class="w3-pf-l"><div class="w3-pf-top"><h3>Total Portfolio Value <i class="w3-eye">◉</i></h3>
<div class="w3-ranges" role="group" aria-label="Chart range">${Object.keys(RANGES).map((r) => `<button class="w3-rng${r === range ? " on" : ""}" data-range="${r}">${r}</button>`).join("")}</div></div>
<div class="w3-big">—</div>
<p class="w3-delta">Balances unavailable · ${m.total} transaction${m.total === 1 ? "" : "s"} · ${m.awaiting} awaiting approval${active ? "" : " · no activity in this period"}</p>
<svg viewBox="0 0 600 120" preserveAspectRatio="none" class="w3-chart" role="img" aria-label="Transactions over time"><defs><linearGradient id="w3g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#7c5cff" stop-opacity=".5"/><stop offset="1" stop-color="#7c5cff" stop-opacity="0"/></linearGradient></defs>
<path d="M0 30H600M0 60H600M0 90H600" stroke="rgba(110,130,220,.12)" stroke-width="1" vector-effect="non-scaling-stroke" fill="none"/>
<path d="${chart.area}" fill="url(#w3g)"/><path d="${chart.line}" fill="none" stroke="#8b7bff" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>
</div>
<div class="w3-stats">${[["Wallets", m.wallets.length, "▣"], ["Networks", m.chains.length, "◍"], ["Tokens", "—", "◎"], ["NFTs", "—", "▨"]].map((s) => `<div class="w3-stat"${s[1] === "—" ? ` title="Not indexed by the backend yet"` : ""}><i>${s[2]}</i><span><small>${s[0]}</small><b>${s[1]}</b></span></div>`).join("")}</div></section>
<section class="w3-card" ${sec("overview wallets")}><div class="w3-row"><h3>Astra Wallets <span class="w3-dim">(${m.wallets.length})</span></h3>
<div class="w3-tools"><input data-q class="w3-in w3-search" placeholder="Search wallets…" value="${esc(S.q)}" aria-label="Search wallets">
${groupSel(m, S)}
<button class="w3-ico${S.layout === "grid" ? " on" : ""}" data-layout="grid" title="Grid">▦</button><button class="w3-ico${S.layout === "list" ? " on" : ""}" data-layout="list" title="List">☰</button>
${actBtn(m, "Import Wallet", "import", "primary", "plus")}</div></div>
<div id="w3-wallets" class="w3-wallets ${S.layout}">${walletsHtml(m, S)}</div></section>
<div class="w3-two"${twoHidden ? " hidden" : ""}><section class="w3-card" ${sec("overview assets defi nfts")}><div class="w3-row"><h3>Assets</h3><div class="w3-tools"><div class="w3-pills">${[["tokens", "Tokens"], ["nfts", "NFTs"], ["defi", "DeFi"], ["all", "All"]].map((t) => `<button class="w3-pill${atab === t[0] ? " on" : ""}" data-atab="${t[0]}">${t[1]}</button>`).join("")}</div><select class="w3-in" data-net aria-label="Network filter">${netOpts}</select></div></div>
<div class="w3-thead"><span>Token</span><span>Balance</span><span>Price</span><span>Value</span><span>24h</span></div><div class="w3-abody">${assetsBody}</div></section>
<section class="w3-card" ${sec("overview transactions")}><div class="w3-row"><h3>Recent Transactions</h3>${txs.length > 6 ? `<button class="w3-btn" data-all>${S.all ? "Show less" : "View All"}</button>` : ""}</div>
<div id="web3-txs" class="w3-txs">${shown.map((t) => `<div class="w3-tx" title="${esc(t.error || t.id)}"><span class="w3-chain">↗</span>
<span class="w3-tm">To ${esc(short(t.to))}<small>${esc(t.network)} · ${esc(ago(t.at, m.now))}</small></span>
<span class="w3-ta">−${esc(t.amount)} ${esc(t.symbol)}<em class="w3-st ${TONE[t.status] || "warn"}">${esc(t.status)}</em></span></div>`).join("") || empty("No transactions yet.")}</div></section></div>
</div>
<aside class="w3-side">
<section class="w3-card" ${sec("overview wallets assets defi nfts")}><h3><i class="w3-h3i">◈</i>Active Wallet</h3>${sel ? `<div class="w3-active"><span class="w3-av lg">${esc(sel.address.slice(2, 4).toUpperCase())}</span>
<div class="w3-aw"><small>${esc(short(sel.address))} · ${esc(srcLabel(sel))}</small><b title="${esc(sel.address)}">${esc(sel.name || short(sel.address))}</b> <button class="w3-ico flat" data-copy="${esc(sel.address)}" title="Copy address">⧉</button>
<div class="w3-wval">—<small>balance not indexed</small></div><div class="w3-wchains">${chainDots(m, 4)}</div></div></div>` : empty("No wallet selected.")}
<div class="w3-qa">${m.actions.includes("tx_prepare") && sel ? `<button class="w3-btn ic-send" data-act="tx_prepare">Send</button>` : btnOff("Send", "Requires a wallet and the tx_prepare tool", "", "send")}
${sel ? `<button class="w3-btn ic-recv" data-copy="${esc(sel.address)}">Receive</button>` : btnOff("Receive", "No wallet selected", "", "recv")}
${[["Swap", "swap"], ["Bridge", "brg"]].map((a) => btnOff(a[0], NOSUP, "", a[1])).join("")}
${[["Buy", "buy"], ["Sell", "sell"], ["Stake", "stk"]].map((a) => btnOff(a[0], NOSUP, "", a[1])).join("")}
${m.actions.includes("token_balance") && sel ? `<button class="w3-btn ic-bal" data-act="token_balance">Balance</button>` : btnOff("Balance", "Requires a wallet and the token_balance tool", "", "bal")}</div>
<select class="w3-in w3-netsel" data-net aria-label="Network">${netOpts}</select></section>
<section class="w3-card" ${sec("overview agents")}><h3><i class="w3-h3i">☺</i>Agent Web3 Actions</h3><p class="w3-note">Prefills Chat — nothing runs until you send it. Transfers stay policy-gated.</p>
<div class="w3-actions">${actCards || empty("No Web3 tools are registered.")}</div></section>
<section class="w3-card" ${sec("overview networks")}><h3><i class="w3-h3i">⛓</i>Networks</h3><div class="w3-nets">${m.chains.map((c) => `<button class="w3-net${String(c.id) === S.net ? " sel" : ""}" data-netpick="${c.id}" title="Chain ${c.id}"><span class="w3-chain sm">${esc(c.name.charAt(0))}</span><span>${esc(c.name)}<small>${esc(c.symbol)} · chain ${c.id}</small></span><em class="w3-st good">Allowed</em></button>`).join("") || empty("No networks reported by the policy.")}</div></section>
<section class="w3-card" ${sec("overview agents transactions")}><h3><i class="w3-h3i">🛡</i>Safety Policy</h3>
<div id="web3-policy" class="w3-kv">${m.available ? `<span>Mode</span><b>${esc(m.mode)}</b><span>Status</span><b>${m.stopped ? "🚨 EMERGENCY STOP" : "running"}</b>
<span>Max per tx</span><b>${esc(limit(p.tx_limit_wei))}</b><span>Max daily</span><b>${esc(limit(p.daily_limit_wei))}</b>
<span>Allowlist</span><b>${(p.recipients_allowed || []).length} recipient(s) · ${(p.contracts_allowed || []).length} contracts · ${(p.wallets_allowed || []).length} wallets</b>` : `<span class="muted">Web3 unavailable</span>`}</div></section>
</aside>${modalHtml(m, X)}<div id="w3-toast" class="w3-toast" role="status"></div></div>`;
  }

  // ---- DOM binding (browser only) -----------------------------------------
  const S = { view: "overview", q: "", layout: "grid", sel: null, net: "all", all: false, range: "1W", atab: "tokens", grp: "", open: {} };
  let M = null, hooks = {}, el = null, MS = IDLE();
  const W = "/api/v1/web3/wallets", G = "/api/v1/web3/wallet-groups";
  const toast = (t) => { const n = el && el.querySelector("#w3-toast"); if (!n) return; n.textContent = t; n.classList.add("on"); setTimeout(() => n.classList.remove("on"), 2000); };
  function paint() { if (el && M) el.innerHTML = html(M, S, MS); }
  const call = (method, path, body) => (hooks.call ? hooks.call(method, path, body) : Promise.resolve({ ok: false, error: "unavailable" }));
  const fail = (r) => (r && r.error) || "Request failed";

  // Apply a wallet-registry snapshot ({wallets, groups, active_id, active_address}).
  function apply(d) {
    const R = mapRegistry(d);
    M.wallets = R.wallets; M.groups = R.groups; M.activeAddress = R.activeAddress;
    if (M.activeAddress) S.sel = M.activeAddress;
    else if (!M.wallets.some((w) => w.address === S.sel)) S.sel = M.wallets[0] ? M.wallets[0].address : null;
    if (S.grp && !M.groups.some((g) => g.id === S.grp)) S.grp = "";
    Object.keys(S.open).forEach((id) => { if (!M.groups.some((g) => g.id === id)) delete S.open[id]; });   // keep the rest
  }
  // The ONE reconciliation path: re-read the canonical registry, then apply it.
  // `fallback` (the snapshot the mutating call returned) is used only if the
  // GET itself fails. Expansion state lives in S.open, so it survives this.
  async function refreshAstraWallets(fallback) {
    const r = await call("GET", W);
    if (r && r.ok && r.data) apply(r.data); else if (fallback) apply(fallback);
    return !!(r && r.ok);
  }
  function closeModal(force) {
    if (MS.secret && !MS.ack && !force) { toast("Confirm you have saved the private key first"); return; }
    MS = IDLE(); paint();   // drops the one-time secret from memory and DOM
  }
  async function run(fn) {
    MS.busy = true; MS.err = ""; paint();
    try { await fn(); } catch (e) { MS.err = "Request failed"; }
    MS.busy = false; paint();
  }
  const doImport = (dry) => run(async () => {
    const gn = importNeedsGroup(MS) ? MS.impGroup.trim() : "";
    const r = await call("POST", W + "/import", { text: MS.text, dry_run: !!dry, group_name: gn || undefined });
    if (!r.ok) { MS.results = null; MS.err = fail(r); return; }
    MS.results = r.data;
    if (!dry) {
      await refreshAstraWallets(r.data);
      const bad = new Set(r.data.results.filter((x) => x.status !== "valid").map((x) => x.line));
      MS.text = MS.text.split(/\r?\n/).filter((_, i) => bad.has(i + 1)).join("\n");   // keep only rejected lines
      toast(plural(r.data.imported, "wallet") + " imported");
    }
  });
  const doCreate = () => run(async () => {
    const r = await call("POST", W + "/create", { name: MS.name, group_id: MS.group || undefined });
    if (!r.ok) { MS.err = fail(r); return; }
    MS.created = r.data.wallet; MS.secret = r.data.reveal && r.data.reveal.private_key; MS.ack = false;
    r.data.reveal = null;   // the model/registry snapshot never holds it
    await refreshAstraWallets(r.data);
  });
  const doSelect = async (address, id) => {
    S.sel = address; paint();
    const r = await call("POST", W + "/" + encodeURIComponent(id || address) + "/select");
    if (r.ok) { await refreshAstraWallets(r.data); paint(); } else { S.sel = M.activeAddress; paint(); toast(fail(r)); }
  };
  // group operations: post, then apply the returned registry snapshot
  const doGroup = (method, path, body, after) => run(async () => {
    const r = await call(method, path, body);
    if (!r.ok) { MS.err = fail(r); return; }
    await refreshAstraWallets(r.data); if (after) after(r.data);
  });

  function onClick(e) {
    const g = (s) => e.target.closest(s);
    let x;
    if ((x = g("[data-modal]"))) { MS = IDLE(); MS.kind = x.dataset.modal; if (S.grp) MS.group = S.grp; return paint(); }
    if (g("[data-mclose]") || (e.target.matches && e.target.matches("[data-mbg]"))) return closeModal();
    if (g("[data-mvalidate]")) return doImport(true);
    if (g("[data-mimport]")) return doImport(false);
    if (g("[data-mcreate]")) return doCreate();
    if (g("[data-mdone]")) return MS.ack ? closeModal(true) : undefined;
    if (g("[data-mcopysecret]")) {
      (navigator.clipboard && MS.secret ? navigator.clipboard.writeText(MS.secret) : Promise.reject()).then(() => toast("Private key copied"), () => toast("Copy failed — select the key manually"));
      return;
    }
    if (g("[data-msetactive]") && MS.created) return doSelect(MS.created.address, MS.created.id);
    if ((x = g("[data-gsel]"))) { MS.gsel = x.dataset.gsel; MS.gname = null; MS.confirmDel = false; MS.err = ""; return paint(); }
    if (g("[data-gcreate]")) return doGroup("POST", G, { name: MS.gnew }, (d) => { MS.gsel = d.group && d.group.id; MS.gnew = ""; MS.gname = null; });
    const gid = () => MS.gsel || (M.groups[0] && M.groups[0].id);
    if (g("[data-grename]")) return doGroup("POST", G + "/" + encodeURIComponent(gid()) + "/rename", { name: MS.gname == null ? "" : MS.gname }, () => { MS.gname = null; });
    if (g("[data-gdel]")) {
      if (!MS.confirmDel) { MS.confirmDel = true; return paint(); }
      return doGroup("POST", G + "/" + encodeURIComponent(gid()) + "/delete", {}, () => { MS.gsel = null; MS.gname = null; MS.confirmDel = false; });
    }
    if ((x = g("[data-gadd]"))) return doGroup("POST", G + "/" + encodeURIComponent(gid()) + "/members", { add: [x.dataset.gadd] });
    if ((x = g("[data-gremove]"))) return doGroup("POST", G + "/" + encodeURIComponent(gid()) + "/members", { remove: [x.dataset.gremove] });
    if (MS.kind) return;   // a modal is open: nothing behind it reacts
    if ((x = g("[data-gtoggle]"))) { const id = x.dataset.gtoggle; S.open[id] = !groupOpen(S, id); return paint(); }   // header only, never a wallet
    if ((x = g(".w3-tab[data-view]"))) { S.view = x.dataset.view; return paint(); } // NOT bare [data-view]: the container carries it too
    if ((x = g("[data-wsel]"))) return doSelect(x.dataset.wsel);
    if ((x = g("[data-layout]"))) { S.layout = x.dataset.layout; return paint(); }
    if ((x = g("[data-netpick]"))) { S.net = S.net === x.dataset.netpick ? "all" : x.dataset.netpick; return paint(); }
    if ((x = g("[data-range]"))) { S.range = x.dataset.range; return paint(); }
    if ((x = g("[data-atab]"))) { S.atab = x.dataset.atab; if (S.view === "nfts" || S.view === "defi") S.view = "assets"; return paint(); }
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
  }
  function render(target, model, h) {
    el = target; M = model; hooks = h || {};
    if (!S.sel || !M.wallets.some((w) => w.address === S.sel)) S.sel = M.activeAddress || (M.wallets[0] ? M.wallets[0].address : null);
    if (S.grp && !M.groups.some((g) => g.id === S.grp)) S.grp = "";
    if (!el.__w3) {
      el.__w3 = true;
      el.addEventListener("click", onClick);
      el.addEventListener("keydown", (e) => { if (e.key === "Escape" && MS.kind) closeModal(); });
      el.addEventListener("input", (e) => {
        const t = e.target;
        if (t.matches("[data-q]")) { S.q = t.value; const w = el.querySelector("#w3-wallets"); if (w) w.innerHTML = walletsHtml(M, S); return; }
        // modal fields update state only (no repaint: keeps focus + caret)
        if (t.matches("[data-mtext]") || t.matches("[data-mgname]")) {
          if (t.matches("[data-mtext]")) MS.text = t.value; else MS.impGroup = t.value;
          const wrap = el.querySelector("[data-mgwrap]"); if (wrap) wrap.hidden = !importNeedsGroup(MS);
          el.querySelectorAll("[data-mvalidate]").forEach((b) => { b.disabled = !MS.text.trim() || MS.busy; });
          el.querySelectorAll("[data-mimport]").forEach((b) => { b.disabled = importBlocked(MS); });
        }
        else if (t.matches("[data-mname]")) MS.name = t.value;
        else if (t.matches("[data-gnew]")) MS.gnew = t.value;
        else if (t.matches("[data-gname]")) MS.gname = t.value;
      });
      el.addEventListener("change", (e) => {
        const t = e.target;
        if (t.matches("[data-net]")) { S.net = t.value; paint(); }
        else if (t.matches("[data-grp]")) { S.grp = t.value; paint(); }
        else if (t.matches("[data-mgroup]")) MS.group = t.value;
        else if (t.matches("[data-mack]")) { MS.ack = t.checked; paint(); }
        else if (t.matches("[data-gmove]") && t.value) {
          const from = MS.gsel || (M.groups[0] && M.groups[0].id);
          doGroup("POST", W + "/" + encodeURIComponent(t.dataset.gmove) + "/move", { from_group: from, to_group: t.value });
        }
      });
    }
    paint();
  }

  // Test hook: forget all UI state (selection, filters, any open dialog and
  // its in-memory secrets). The page itself never needs this.
  function resetState() {
    Object.assign(S, { view: "overview", q: "", layout: "grid", sel: null, net: "all", all: false, range: "1W", atab: "tokens", grp: "", open: {} });
    MS = IDLE();
  }

  const api = { resetState, buildModel, mapRegistry, html, walletsHtml, modalHtml, activitySeries, hourSeries, seriesFor, chartPaths, nativeAmt, short, render, ACTIONS, IDLE, groupOpen, refreshAstraWallets };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.Web3Center = api;
})(typeof window !== "undefined" ? window : globalThis);
