"use strict";
const test = require("node:test");
const assert = require("node:assert");
const W = require("../../static/js/web3_center.js");

const NOW = new Date(2026, 8, 29, 12, 0, 0);
const S = () => ({ view: "overview", q: "", layout: "grid", sel: null, net: "all", all: false });
const pol = { ok: true, data: { mode: "CONFIRM", stopped: false, policy: {
  tx_limit_wei: 1e18, daily_limit_wei: 5e18, recipients_allowed: ["0xr"], contracts_allowed: [],
  wallets_allowed: ["0x1111111111111111111111111111111111111111", "0x2222222222222222222222222222222222222222"],
  chains_allowed: [1, 8453, 999] } } };
const A1 = "0x1111111111111111111111111111111111111111", A2 = "0x2222222222222222222222222222222222222222";
const wal = { ok: true, data: { active_id: "w_1", active_address: A1,
  wallets: [
    { id: "w_1", address: A1, name: "Alpha", source: "imported", can_sign: true, active: true, group_ids: ["g_1"] },
    { id: "w_2", address: A2, name: "Beta <b>x</b>", source: "created", can_sign: true, active: false, group_ids: [] }],
  groups: [{ id: "g_1", name: "Main Wallets", wallet_ids: ["w_1"], count: 1 }, { id: "g_2", name: "Trading", wallet_ids: [], count: 0 }] } };
const txs = { ok: true, data: { stats: { by_status: { PREPARED: 2, CONFIRMED: 1 } }, transactions: [
  { tx_id: "tx_a", status: "CONFIRMED", to: "0x3333333333333333333333333333333333333333", value_wei: "50000000000000000", chain_id: 1, created_at: "2026-09-29 10:00:00" },
  { tx_id: "tx_b", status: "PREPARED", to: "0x4444444444444444444444444444444444444444", value_wei: "0", chain_id: 8453, created_at: "2026-09-28 09:00:00", error: "<img src=x onerror=alert(1)>" } ] } };
const tools = { ok: true, data: { tools: [
  { name: "token_balance", category: "web3" }, { name: "tx_prepare", category: "web3" }, { name: "browser_open", category: "browser" }] } };

test("model uses only real payload data", () => {
  const m = W.buildModel(pol, txs, tools, NOW, wal);
  assert.strictEqual(m.wallets.length, 2);
  assert.strictEqual(m.registry, true);
  assert.strictEqual(m.activeAddress, A1);
  assert.deepStrictEqual(m.chains.map((c) => c.name), ["Ethereum", "Base", "Chain 999"]);
  assert.strictEqual(m.total, 3);
  assert.strictEqual(m.awaiting, 2);
  assert.strictEqual(m.txs[0].amount, "0.0500");
  assert.deepStrictEqual(m.actions, ["token_balance", "tx_prepare"]); // only registered web3 tools
});

test("empty / failed backends give honest empty states, never fake data", () => {
  const m = W.buildModel({ ok: false }, { ok: false }, { ok: false }, NOW);
  const h = W.html(m, S());
  assert.strictEqual(m.wallets.length, 0);
  assert.strictEqual(m.registry, false);
  assert.match(h, /wallet registry is unavailable/);
  assert.match(h, /Web3 unavailable/);
  assert.match(h, /No transactions yet/);
  assert.match(h, /No Web3 tools are registered/);
  assert.ok(!/\$\d/.test(h), "no dollar amounts may appear without backend data");
  assert.ok(!/Trading Wallet|DeFi Wallet|NFT Wallet|Main Wallet/.test(h));
});

test("unsupported features are disabled, supported ones are enabled", () => {
  const h = W.html(W.buildModel(pol, txs, tools, NOW, wal), S());
  for (const l of ["Swap", "Bridge", "Buy", "Sell", "Stake"])
    assert.match(h, new RegExp(`<button class="w3-btn [^"]*" disabled title="[^"]*">${l}</button>`), l);
  // wallet import / create / groups are real now
  for (const [l, k] of [["Import Wallet", "import"], ["Create Wallet", "create"], ["Astra Wallets", "groups"]])
    assert.match(h, new RegExp(`<button class="w3-btn [^"]*" data-modal="${k}">${l}</button>`), l);
  assert.match(h, /data-act="tx_prepare">Send/);
  assert.match(h, /data-copy="0x1111[^"]*">Receive/);
});

test("without a wallet registry the wallet controls stay disabled", () => {
  const h = W.html(W.buildModel(pol, txs, tools, NOW), S());
  for (const l of ["Import Wallet", "Create Wallet", "Astra Wallets"])
    assert.match(h, new RegExp(`<button class="w3-btn [^"]*" disabled title="[^"]*">${l}</button>`), l);
  assert.ok(!/data-modal=/.test(h));
});

test("all backend strings are escaped", () => {
  const h = W.html(W.buildModel(pol, txs, tools, NOW, wal), S());
  assert.ok(!h.includes("<img src=x"), "raw tx.error must not reach the DOM");
  assert.ok(!h.includes("<b>x</b>"), "wallet names are escaped too");
  assert.match(h, /Beta &lt;b&gt;x&lt;\/b&gt;/);
  assert.match(h, /&lt;img src=x/);
});

test("wallet search filters and network filter applies to transactions", () => {
  const m = W.buildModel(pol, txs, tools, NOW, wal);
  assert.ok(!W.walletsHtml(m, { ...S(), q: "2222" }).includes(A1));
  assert.ok(W.walletsHtml(m, { ...S(), q: "alpha" }).includes(A1), "search matches wallet names");
  assert.match(W.walletsHtml(m, { ...S(), q: "zzzz" }), /No wallets match/);
  const h = W.html(m, { ...S(), net: "8453" });
  assert.ok(h.includes("Base · ") && !h.includes("To 0x3333"));
});

test("activity series and chart geometry", () => {
  const s = W.activitySeries(W.buildModel(pol, txs, tools, NOW).txs.map((t) => ({ created_at: t.at })), 14, NOW);
  assert.strictEqual(s.length, 14);
  assert.strictEqual(s[13].count, 1); assert.strictEqual(s[12].count, 1);
  const c = W.chartPaths(s, 600, 110);
  assert.match(c.line, /^M0\.0 /); assert.ok(c.area.endsWith("Z"));
  assert.ok(!/NaN/.test(c.line));
});

test("agent prompts contain public addresses only and never secrets", () => {
  for (const k of Object.keys(W.ACTIONS)) {
    const p = W.ACTIONS[k][2]({ address: "0x1111", network: "Base" });
    assert.ok(!/private key|seed|mnemonic|secret/i.test(p), k);
  }
});

// ---- event binding regression: a click deep inside the dashboard must reach
// its own handler, not be swallowed by the container's data-view attribute.
function fakeDom() {
  const mk = (attrs, cls, parent) => ({ attrs, cls: cls || [], parent, dataset: Object.fromEntries(Object.entries(attrs).map(([k, v]) => [k.replace(/^data-/, ""), v])),
    matches(sel) { return match(this, sel); }, closest(sel) { for (let n = this; n; n = n.parent) if (match(n, sel)) return n; return null; } });
  function match(n, sel) { // supports "[attr]" and ".cls[attr]"
    const m = /^(?:\.([\w-]+))?\[([\w-]+)\]$/.exec(sel); if (!m) return false;
    return (!m[1] || n.cls.includes(m[1])) && m[2] in n.attrs;
  }
  return { mk };
}
test("clicks reach their handlers even though the container has data-view", () => {
  const { mk } = fakeDom();
  const handlers = {}; const target = { addEventListener: (t, f) => (handlers[t] = f), querySelector: () => null, innerHTML: "" };
  const prompts = [];
  W.render(target, W.buildModel(pol, txs, tools, NOW, wal), { toChat: (t) => prompts.push(t), call: async () => ({ ok: true, data: wal.data }) });
  const container = mk({ "data-view": "overview" }, ["w3"], null);
  // an agent-action button nested inside the container
  handlers.click({ target: mk({ "data-act": "token_balance" }, ["w3-action"], container) });
  assert.strictEqual(prompts.length, 1);
  assert.match(prompts[0], /token balance of 0x1111/);
  // a real tab button still switches view
  handlers.click({ target: mk({ "data-view": "wallets" }, ["w3-tab"], container) });
  assert.match(target.innerHTML, /data-view="wallets"/);
  // wallet selection is honoured
  handlers.click({ target: mk({ "data-wsel": A2 }, ["w3-wallet"], container) });
  handlers.click({ target: mk({ "data-act": "tx_prepare" }, ["w3-btn"], container) });
  assert.match(prompts[1], /from 0x2222/);
});

// ---- target-layout redesign coverage -------------------------------------
test("dashboard renders every target section for the overview", () => {
  const h = W.html(W.buildModel(pol, txs, tools, NOW, wal), S());
  for (const t of ["Overview", "Wallets", "Assets", "Transactions", "DeFi", "NFTs", "Networks", "Agent Actions"])
    assert.match(h, new RegExp(`data-view="[a-z]+"><i>[^<]*</i>${t}</button>`), "nav " + t);
  for (const s of ["Total Portfolio Value", "Astra Wallets", "Add Wallet", "Active Wallet", "Recent Transactions", "Agent Web3 Actions", "Safety Policy"])
    assert.ok(h.includes(s), s);
  for (const r of ["1D", "1W", "1M", "3M", "1Y"]) assert.ok(h.includes(`data-range="${r}"`), r);
  for (const l of ["Wallets", "Networks", "Tokens", "NFTs"]) assert.ok(h.includes(`<small>${l}</small>`), l);
  assert.ok(!/\$\d/.test(h), "still no invented dollar values");
});

test("views hide non-matching sections instead of leaving blank space", () => {
  const m = W.buildModel(pol, txs, tools, NOW);
  const hid = (h, sec) => new RegExp(`data-sec="${sec}" hidden`).test(h);
  assert.ok(!/data-sec="[^"]*" hidden/.test(W.html(m, S())), "overview shows everything");
  const w = W.html(m, { ...S(), view: "wallets" });
  assert.ok(hid(w, "overview") && !hid(w, "overview wallets"));
  const n = W.html(m, { ...S(), view: "nfts" });
  assert.match(n, /NFT holdings aren’t indexed/);
  assert.match(W.html(m, { ...S(), view: "defi" }), /DeFi positions aren’t indexed/);
});

test("timeframe controls drive a real activity series", () => {
  const list = [{ created_at: "2026-09-29 11:30:00" }, { created_at: "2026-09-29 11:45:00" }, { created_at: "2026-09-20 08:00:00" }];
  const day = W.seriesFor(list, "1D", NOW);
  assert.strictEqual(day.length, 24); assert.strictEqual(day[23].count, 0); assert.strictEqual(day[22].count, 2);
  assert.strictEqual(W.seriesFor(list, "1W", NOW).length, 7);
  assert.strictEqual(W.seriesFor(list, "1M", NOW).length, 30);
  assert.strictEqual(W.seriesFor(list, "1Y", NOW).length, 365);
  assert.match(W.html(W.buildModel(pol, txs, tools, NOW), { ...S(), range: "1M" }), /class="w3-rng on" data-range="1M"/);
});

test("agent action grid: real tools enabled, unsupported ones disabled, capped outside the Agent tab", () => {
  const m = W.buildModel(pol, txs, tools, NOW); // 2 registered tools
  const over = W.html(m, S());
  assert.match(over, /data-act="token_balance"/);
  assert.match(over, /<button class="w3-action" disabled title="Not supported by the backend yet">/);
  assert.strictEqual((over.match(/class="w3-action"/g) || []).length, 6);
  assert.strictEqual((W.html(m, { ...S(), view: "agents" }).match(/class="w3-action"/g) || []).length, 6); // 2 real + 4 unsupported
});

test("assets tab/range clicks repaint without touching backend hooks", () => {
  const { mk } = fakeDom();
  const handlers = {}; const target = { addEventListener: (t, f) => (handlers[t] = f), querySelector: () => null, innerHTML: "" };
  W.render(target, W.buildModel(pol, txs, tools, NOW), {});
  const c = mk({ "data-view": "overview" }, ["w3"], null);
  handlers.click({ target: mk({ "data-range": "3M" }, ["w3-rng"], c) });
  assert.match(target.innerHTML, /class="w3-rng on" data-range="3M"/);
  handlers.click({ target: mk({ "data-atab": "nfts" }, ["w3-pill"], c) });
  assert.match(target.innerHTML, /NFT holdings aren’t indexed/);
});

// ============================================================================
// Wallet registry UI: Import Wallet / Create Wallet / Astra Wallets
// ============================================================================
const fs = require("node:fs");
const SECRET = "0x" + "ab".repeat(32);

// A stateful fake backend + DOM harness so the REAL render()/click/input/change
// wiring runs; only the network and the browser are simulated.
function harness(over = {}) {
  W.resetState();
  const { mk } = fakeDom();
  const handlers = {};
  const target = { addEventListener: (t, f) => (handlers[t] = f), querySelector: () => null, querySelectorAll: () => [], innerHTML: "" };
  const calls = [], gets = [];   // calls = mutations; gets = GET /api/v1/web3/wallets reads
  const state = JSON.parse(JSON.stringify(over.wal || wal.data));
  const snap = () => JSON.parse(JSON.stringify(state));
  const regroup = () => state.groups.forEach((x) => { x.wallet_ids = state.wallets.filter((w) => w.group_ids.includes(x.id)).map((w) => w.id); x.count = x.wallet_ids.length; });
  const backend = over.backend || (async (method, path, body) => {
    if (method === "GET") { gets.push(path); return { ok: true, data: snap() }; }
    calls.push({ method, path, body });
    if (path.endsWith("/import")) {
      const lines = String(body.text).split(/\r?\n/).filter((l) => l.trim());
      const results = lines.map((l, i) => l.startsWith("0x" + "ab") || /^[0-9a-f]{64}$/.test(l)
        ? { line: i + 1, status: "valid", imported: !body.dry_run, address: "0x" + "c" + String(i).padStart(39, "0") }
        : { line: i + 1, status: "invalid", imported: false, reason: "expected a 64-hex-character private key or a 0x address" });
      const okc = results.filter((r) => r.status === "valid").length;
      let group;
      if (!body.dry_run) {
        if (body.group_name && okc >= 2) { group = { id: "g_i" + state.groups.length, name: body.group_name, wallet_ids: [], count: 0 }; state.groups.push(group); }
        results.filter((r) => r.imported).forEach((r) => state.wallets.push({ id: "w_n" + r.line, address: r.address, name: "Imported wallet", source: "imported", can_sign: true, active: false, group_ids: group ? [group.id] : [] }));
        regroup();
      }
      return { ok: true, data: { dry_run: !!body.dry_run, imported: body.dry_run ? 0 : okc, rejected: results.length - okc, valid: okc, results, ...(group ? { group: { ...group } } : {}), ...(body.dry_run ? {} : snap()) } };
    }
    if (path.endsWith("/create")) {
      const w = { id: "w_new", address: "0x" + "d".repeat(40), name: body.name || "Created wallet", source: "created", can_sign: true, active: false, group_ids: [] };
      state.wallets.push(w);
      return { ok: true, data: { wallet: w, reveal: { private_key: SECRET, shown_once: true }, ...snap() } };
    }
    let m;
    if ((m = /\/wallets\/([^/]+)\/select$/.exec(path))) {
      const w = state.wallets.find((x) => x.id === m[1] || x.address === m[1]);
      if (!w) return { ok: false, error: "wallet not found" };
      state.wallets.forEach((x) => { x.active = x === w; }); state.active_address = w.address; state.active_id = w.id;
      return { ok: true, data: snap() };
    }
    if (path.endsWith("/wallet-groups") && method === "POST") {
      const g = { id: "g_" + (state.groups.length + 5), name: body.name, wallet_ids: [], count: 0 };
      state.groups.push(g); return { ok: true, data: { group: g, ...snap() } };
    }
    if ((m = /wallet-groups\/([^/]+)\/(rename|delete|members)$/.exec(path))) {
      const g = state.groups.find((x) => x.id === m[1]);
      if (m[2] === "rename") g.name = body.name;
      if (m[2] === "delete") { state.groups = state.groups.filter((x) => x !== g); state.wallets.forEach((w) => { w.group_ids = w.group_ids.filter((i) => i !== g.id); }); }
      if (m[2] === "members") {
        (body.add || []).forEach((id) => { const w = state.wallets.find((x) => x.id === id); if (!w.group_ids.includes(g.id)) w.group_ids.push(g.id); });
        (body.remove || []).forEach((id) => { const w = state.wallets.find((x) => x.id === id); w.group_ids = w.group_ids.filter((i) => i !== g.id); });
      }
      state.groups.forEach((x) => { x.wallet_ids = state.wallets.filter((w) => w.group_ids.includes(x.id)).map((w) => w.id); x.count = x.wallet_ids.length; });
      return { ok: true, data: snap() };
    }
    if ((m = /\/wallets\/([^/]+)\/move$/.exec(path))) {
      const w = state.wallets.find((x) => x.id === m[1]);
      w.group_ids = w.group_ids.filter((i) => i !== body.from_group).concat(body.to_group);
      state.groups.forEach((x) => { x.wallet_ids = state.wallets.filter((ww) => ww.group_ids.includes(x.id)).map((ww) => ww.id); x.count = x.wallet_ids.length; });
      return { ok: true, data: snap() };
    }
    return { ok: false, error: "not found" };
  });
  W.render(target, W.buildModel(pol, txs, tools, NOW, { ok: true, data: snap() }), { call: backend, toChat() {} });
  const root = mk({ "data-view": "overview" }, ["w3"], null);
  const click = (attrs, cls) => handlers.click({ target: mk(attrs, cls || [], root) });
  const type = (attr, value) => handlers.input({ target: Object.assign(mk({ [attr]: "" }, [], root), { value }) });
  const change = (attr, value, extra = {}) => handlers.change({ target: Object.assign(mk({ [attr]: "", ...extra }, [], root), { value }) });
  return { target, calls, gets, state, click, type, change, handlers, mk, root };
}

test("wallet UI never uses browser storage and never persists secrets client-side", () => {
  const src = fs.readFileSync(require.resolve("../../static/js/web3_center.js"), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");   // code only, not prose
  assert.ok(!/localStorage|sessionStorage|indexedDB|document\.cookie/.test(src));
});

test("registry model maps wallets, groups and the active wallet", () => {
  const m = W.buildModel(pol, txs, tools, NOW, wal);
  assert.deepStrictEqual(m.wallets.map((w) => [w.id, w.groupIds.length, w.active]), [["w_1", 1, true], ["w_2", 0, false]]);
  assert.deepStrictEqual(m.groups.map((g) => [g.name, g.count]), [["Main Wallets", 1], ["Trading", 0]]);
});

test("Astra Wallets: group filter, search, active wallet and empty registry", () => {
  const m = W.buildModel(pol, txs, tools, NOW, wal);
  const inGroup = W.walletsHtml(m, { ...S(), grp: "g_1" });
  assert.ok(inGroup.includes(A1) && !inGroup.includes(A2));
  assert.match(W.walletsHtml(m, { ...S(), grp: "g_2" }), /No wallets match/);
  assert.match(W.html(m, S()), /<option value="g_1">Main Wallets \(1\)<\/option>/);
  assert.match(W.html(m, { ...S(), grp: "g_1" }), /<option value="g_1" selected>/);
  assert.match(W.walletsHtml(m, { ...S(), open: { g_1: true } }), /class="w3-wallet w3-gitem sel" data-wsel="0x1111/);   // backend-active wallet
  const e = W.buildModel(pol, txs, tools, NOW, { ok: true, data: { wallets: [], groups: [], active_address: null } });
  assert.match(W.walletsHtml(e, S()), /No wallets yet/);
  assert.match(W.walletsHtml(e, S()), /data-modal="import"/);
});

test("idle dashboard HTML contains no secret-bearing markup", () => {
  const h = W.html(W.buildModel(pol, txs, tools, NOW, wal), S());
  assert.ok(!/data-msecret|w3-msecret|private key/i.test(h));
});

test("import: paste, validate, import — per-line status, refresh, rejected lines kept", async () => {
  const H = harness();
  await H.click({ "data-modal": "import" }, ["w3-btn"]);
  assert.match(H.target.innerHTML, /role="dialog"[^>]*aria-label="Import Wallet"/);
  H.type("data-mtext", ["ab".repeat(32), "not-a-key", "cd".repeat(32)].join("\n"));
  H.type("data-mgname", "Exchange Wallets");
  await H.click({ "data-mvalidate": "" }, ["w3-btn"]);
  assert.deepStrictEqual(H.calls[0].body.dry_run, true);
  assert.match(H.target.innerHTML, /2 valid · 1 will be rejected/);
  assert.match(H.target.innerHTML, /w3-mrow ok/); assert.match(H.target.innerHTML, /w3-mrow bad/);
  assert.equal(H.state.wallets.length, 2, "validate imports nothing");
  await H.click({ "data-mimport": "" }, ["w3-btn"]);
  assert.strictEqual(H.calls[1].body.dry_run, false);
  assert.match(H.target.innerHTML, /2 wallets imported · 1 rejected/);
  assert.strictEqual(H.calls[1].body.group_name, "Exchange Wallets");
  // Astra Wallets refreshed immediately from GET /wallets: 4 wallets, the 2 new ones inside ONE collapsed group
  assert.equal(H.gets.length, 1);
  assert.match(H.target.innerHTML, /Astra Wallets <span class="w3-dim">\(4\)/);
  assert.equal((H.target.innerHTML.match(/data-gtoggle=/g) || []).length, 3);
  assert.match(H.target.innerHTML, /Group “Exchange Wallets” created/);
  // valid secrets are dropped from the textarea; only the rejected line remains
  const ta = /<textarea[^>]*>([^<]*)<\/textarea>/.exec(H.target.innerHTML)[1];
  assert.equal(ta, "not-a-key");
  assert.ok(!H.target.innerHTML.includes("ab".repeat(32)) && !H.target.innerHTML.includes("cd".repeat(32)));
});

test("import: empty input keeps buttons disabled; backend errors are shown, not thrown", async () => {
  const H = harness({ backend: async () => ({ ok: false, error: "nothing to import" }) });
  await H.click({ "data-modal": "import" });
  assert.match(H.target.innerHTML, /data-mvalidate disabled/);
  H.type("data-mtext", "x");
  await H.click({ "data-mimport": "" });
  assert.match(H.target.innerHTML, /class="w3-merr">nothing to import/);
});

test("create: key shown once, ack required, dropped from DOM after Done", async () => {
  const H = harness();
  await H.click({ "data-modal": "create" });
  H.type("data-mname", "Fresh");
  await H.click({ "data-mcreate": "" });
  assert.deepStrictEqual(H.calls[0].body, { name: "Fresh", group_id: undefined });
  assert.ok(H.target.innerHTML.includes(SECRET), "shown in the one-time view");
  assert.match(H.target.innerHTML, /data-mdone disabled/);
  // closing without acknowledging is refused: the secret stays until it is saved
  await H.click({ "data-mclose": "" });
  assert.ok(H.target.innerHTML.includes(SECRET));
  await H.handlers.keydown({ key: "Escape" });
  assert.ok(H.target.innerHTML.includes(SECRET));
  const ackEl = Object.assign(H.mk({ "data-mack": "" }, [], H.root), { checked: true });
  H.handlers.change({ target: ackEl });
  assert.doesNotMatch(H.target.innerHTML, /data-mdone disabled/);
  await H.click({ "data-mdone": "" });
  assert.ok(!H.target.innerHTML.includes(SECRET), "secret gone after Done");
  assert.ok(!/w3-msecret/.test(H.target.innerHTML));
  // the new wallet is in Astra Wallets straight away (as a standalone wallet) and selectable
  assert.match(H.target.innerHTML, /data-wsel="0xd{40}"/);
});

test("create: new wallet can be made the active wallet", async () => {
  const H = harness();
  await H.click({ "data-modal": "create" });
  await H.click({ "data-mcreate": "" });
  await H.click({ "data-msetactive": "" });
  assert.match(H.calls[1].path, /\/wallets\/w_new\/select$/);
  assert.match(H.target.innerHTML, /Active wallet/);
});

test("selecting a wallet persists via the backend and updates Active Wallet + prompts", async () => {
  const H = harness();
  await H.click({ "data-wsel": A2 }, ["w3-wallet"]);
  assert.match(H.calls[0].path, /\/wallets\/0x2222[^/]*\/select$/);
  assert.match(H.target.innerHTML, /class="w3-wallet sel" data-wsel="0x2222/);
  assert.match(H.target.innerHTML, /Active Wallet<\/h3><div class="w3-active">.*Beta/s);
});

test("selecting a wallet: backend failure reverts the selection", async () => {
  const H = harness({ backend: async () => ({ ok: false, error: "wallet not found" }) });
  await H.click({ "data-wsel": A2 }, ["w3-wallet"]);
  assert.match(H.target.innerHTML, /class="w3-wallet" data-wsel="0x2222/);   // Beta is not selected
  assert.match(H.target.innerHTML, /Active Wallet<\/h3><div class="w3-active">.*Alpha/s);
});

test("groups: create, rename, add, move, remove, delete (confirm), filter", async () => {
  const H = harness();
  await H.click({ "data-modal": "groups" });
  assert.match(H.target.innerHTML, /aria-label="Astra Wallets"/);
  H.type("data-gnew", "DeFi");
  await H.click({ "data-gcreate": "" });
  assert.match(H.calls[0].path, /\/wallet-groups$/);
  assert.match(H.target.innerHTML, /<b>DeFi<\/b><small>0 wallets<\/small>/);
  // the new group is selected; add wallet Beta to it
  await H.click({ "data-gadd": "w_2" });
  assert.deepStrictEqual(H.calls[1].body, { add: ["w_2"] });
  assert.match(H.calls[1].path, /\/wallet-groups\/g_7\/members$/);
  assert.match(H.target.innerHTML, /Wallets in DeFi <span class="w3-dim">\(1\)/);
  // rename
  H.type("data-gname", "DeFi 2");
  await H.click({ "data-grename": "" });
  assert.deepStrictEqual(H.calls[2].body, { name: "DeFi 2" });
  assert.match(H.target.innerHTML, /<b>DeFi 2<\/b>/);
  // move to Trading
  await H.change("data-gmove", "g_2", { "data-gmove": "w_2" });
  const mv = H.calls.find((c) => /\/move$/.test(c.path));
  assert.deepStrictEqual([mv.path.split("/").slice(-2)[0], mv.body.from_group, mv.body.to_group], ["w_2", "g_7", "g_2"]);
  // remove from Trading
  await H.click({ "data-gsel": "g_2" });
  await H.click({ "data-gremove": "w_2" });
  assert.deepStrictEqual(H.calls[H.calls.length - 1].body, { remove: ["w_2"] });
  // delete needs a confirming second click
  const before = H.calls.length;
  await H.click({ "data-gdel": "" });
  assert.equal(H.calls.length, before, "first click only arms the confirmation");
  assert.match(H.target.innerHTML, /Confirm delete/);
  await H.click({ "data-gdel": "" });
  assert.match(H.calls[H.calls.length - 1].path, /\/wallet-groups\/g_2\/delete$/);
  assert.ok(!/<b>Trading<\/b>/.test(H.target.innerHTML));
  assert.equal(H.state.wallets.length, 2, "deleting a group keeps its wallets");
});

test("groups: filtering Astra Wallets by group via the select, and reset when the group is deleted", async () => {
  const H = harness();
  await H.change("data-grp", "g_1");
  const grid = /id="w3-wallets"[^>]*>(.*?)<\/div><\/section>/s.exec(H.target.innerHTML)[1];
  assert.ok(grid.includes(A1) && !grid.includes(A2));
  await H.click({ "data-modal": "groups" });
  await H.click({ "data-gdel": "" }); await H.click({ "data-gdel": "" });   // g_1 is selected first
  await H.click({ "data-mclose": "" });
  assert.match(H.target.innerHTML, new RegExp(A2), "filter cleared once its group no longer exists");
});

test("nothing behind an open dialog reacts to clicks; Escape closes a plain dialog", async () => {
  const H = harness();
  await H.click({ "data-modal": "import" });
  await H.click({ "data-wsel": A2 }, ["w3-wallet"]);
  assert.equal(H.calls.length, 0);
  await H.handlers.keydown({ key: "Escape" });
  assert.ok(!/role="dialog"/.test(H.target.innerHTML));
});

test("dialog content is escaped (names/group names cannot inject markup)", async () => {
  const H = harness();
  await H.click({ "data-modal": "groups" });
  H.type("data-gnew", "<img src=x onerror=alert(1)>");
  await H.click({ "data-gcreate": "" });
  assert.ok(!H.target.innerHTML.includes("<img src=x"));
  assert.match(H.target.innerHTML, /&lt;img src=x/);
});

// ============================================================================
// Astra Wallets: standalone wallets + collapsible, numbered groups
// ============================================================================
const KEY_A = "aa".repeat(32), KEY_B = "bb".repeat(32), KEY_C = "cc".repeat(32);
const ex = (n) => "0x" + String(n).repeat(40);
// registry with Main + 3 exchange wallets in one group + an empty second group
const grouped = () => ({ active_id: "w_1", active_address: ex(1), wallets: [
  { id: "w_1", address: ex(1), name: "Main Wallet", source: "created", can_sign: true, active: true, group_ids: [] },
  { id: "w_2", address: ex(2), name: "Binance", source: "imported", can_sign: true, active: false, group_ids: ["g_x"] },
  { id: "w_3", address: ex(3), name: "Coinbase", source: "imported", can_sign: true, active: false, group_ids: ["g_x"] },
  { id: "w_4", address: ex(4), name: "Bybit", source: "imported", can_sign: true, active: false, group_ids: ["g_x"] },
  { id: "w_5", address: ex(5), name: "Trader", source: "imported", can_sign: true, active: false, group_ids: ["g_y"] }],
  groups: [{ id: "g_x", name: "Exchange Wallets", wallet_ids: ["w_2", "w_3", "w_4"], count: 3 },
           { id: "g_y", name: "Trading Wallets", wallet_ids: ["w_5"], count: 1 }] });
const gmodel = () => W.buildModel(pol, txs, tools, NOW, { ok: true, data: grouped() });
const listOnly = (h) => /id="w3-wallets"[^>]*>(.*?)<\/div><\/section>/s.exec(h)[1];

test("Astra Wallets: standalone wallets show name + short address; groups start collapsed", () => {
  const h = W.walletsHtml(gmodel(), S());
  assert.match(h, /data-wsel="0x1{40}"/);                               // standalone wallet card
  assert.match(h, /Main Wallet<small>0x1111…1111 · Created wallet<\/small>/);   // name + shortened address
  assert.equal((h.match(/data-gtoggle=/g) || []).length, 2);
  assert.match(h, /<b>Exchange Wallets<\/b><small>3 wallets<\/small><i aria-hidden="true">▼<\/i>/);
  assert.match(h, /aria-expanded="false"/);
  for (const n of [2, 3, 4, 5]) assert.ok(!h.includes(ex(n)), "grouped wallets are hidden while collapsed");
});

test("Astra Wallets: an expanded group lists its wallets numbered 1, 2, 3 from render order", () => {
  const h = W.walletsHtml(gmodel(), { ...S(), open: { g_x: true } });
  assert.match(h, /<i aria-hidden="true">▲<\/i>/);
  const nums = [...h.matchAll(/class="w3-gnum">(\d+)\.<\/span>\s*<span class="w3-wname">([^<]*)<small>([^<]*)<\/small>/g)].map((x) => x.slice(1));
  assert.deepStrictEqual(nums, [["1", "Binance", "0x2222…2222"], ["2", "Coinbase", "0x3333…3333"], ["3", "Bybit", "0x4444…4444"]]);
  assert.ok(!h.includes(ex(5)), "the other group stays collapsed");
  assert.ok(!/serial|"number"/.test(JSON.stringify(W.buildModel(pol, txs, tools, NOW, { ok: true, data: grouped() }).wallets)), "numbers are not wallet data");
});

test("group header click expands/collapses; wallet click selects and never toggles a group", async () => {
  const H = harness({ wal: grouped() });
  const grpHtml = () => H.target.innerHTML;
  assert.ok(!grpHtml().includes(ex(2)));
  await H.click({ "data-gtoggle": "g_x" }, ["w3-ghead"]);
  assert.ok(grpHtml().includes(ex(2)) && /aria-expanded="true"/.test(grpHtml()));
  assert.equal(H.calls.length, 0, "toggling is pure UI state");
  await H.click({ "data-wsel": ex(3) }, ["w3-wallet"]);                 // wallet click: selects only
  assert.match(H.calls[0].path, /\/wallets\/0x3{40}\/select$/);
  assert.ok(grpHtml().includes(ex(2)), "the group stayed open after a wallet click");
  await H.click({ "data-gtoggle": "g_x" }, ["w3-ghead"]);              // header again: collapses
  assert.ok(!grpHtml().includes(ex(2)));
  assert.match(grpHtml(), /<b>Exchange Wallets<\/b><small>3 wallets<\/small><i aria-hidden="true">▼/);
});

test("multiple groups expand and collapse independently", async () => {
  const H = harness({ wal: grouped() });
  await H.click({ "data-gtoggle": "g_x" }, ["w3-ghead"]);
  await H.click({ "data-gtoggle": "g_y" }, ["w3-ghead"]);
  assert.ok(H.target.innerHTML.includes(ex(2)) && H.target.innerHTML.includes(ex(5)));
  await H.click({ "data-gtoggle": "g_x" }, ["w3-ghead"]);
  assert.ok(!H.target.innerHTML.includes(ex(2)) && H.target.innerHTML.includes(ex(5)), "closing one leaves the other open");
  assert.equal((H.target.innerHTML.match(/class="w3-gnum">1\./g) || []).length, 1, "numbering restarts per group");
});

test("import of ONE wallet: no group name asked, no group requested, wallet appears standalone", async () => {
  const H = harness({ wal: grouped() });
  await H.click({ "data-modal": "import" }, ["w3-btn"]);
  const one = W.modalHtml(gmodel(), { ...W.IDLE(), kind: "import", text: KEY_A });
  assert.match(one, /data-mgwrap hidden/, "Group Name field is hidden for a single wallet");
  assert.doesNotMatch(one, /data-mimport disabled/);
  H.type("data-mtext", KEY_A);
  await H.click({ "data-mimport": "" }, ["w3-btn"]);
  assert.strictEqual(H.calls[0].body.group_name, undefined);
  assert.strictEqual(H.calls[0].body.group_id, undefined);
  assert.equal(H.state.groups.length, 2, "no group was created");
  assert.equal(H.state.wallets.length, 6);
  assert.match(listOnly(H.target.innerHTML), /data-wsel="0xc0{38}0"/);   // standalone card, visible without expanding
});

test("import of 2+ wallets: Group Name required, ONE group holds them, existing wallets stay", async () => {
  const H = harness({ wal: grouped() });
  await H.click({ "data-modal": "import" }, ["w3-btn"]);
  const text = [KEY_A, KEY_B, KEY_C].join("\n");
  const three = { ...W.IDLE(), kind: "import", text };
  assert.doesNotMatch(W.modalHtml(gmodel(), three), /data-mgwrap hidden/, "Group Name field appears for 2+ wallets");
  assert.match(W.modalHtml(gmodel(), three), /data-mimport disabled/, "cannot import until the group is named");
  assert.doesNotMatch(W.modalHtml(gmodel(), { ...three, impGroup: "Binance Wallets" }), /data-mimport disabled/);
  // live (no repaint) updates while typing: field revealed, Import enabled only once a name is entered
  const wrap = { hidden: true }, btn = { disabled: true };
  H.target.querySelector = (q) => (q === "[data-mgwrap]" ? wrap : null);
  H.target.querySelectorAll = (q) => (q === "[data-mimport]" ? [btn] : []);
  H.type("data-mtext", text);
  assert.equal(wrap.hidden, false); assert.equal(btn.disabled, true);
  H.type("data-mgname", "Binance Wallets");
  assert.equal(btn.disabled, false);
  await H.click({ "data-mimport": "" }, ["w3-btn"]);
  assert.strictEqual(H.calls[0].body.group_name, "Binance Wallets");
  assert.equal(H.state.groups.length, 3, "exactly one new group");
  const g = H.state.groups.find((x) => x.name === "Binance Wallets");
  assert.equal(g.count, 3);
  const html = listOnly(H.target.innerHTML);
  assert.match(html, /<b>Binance Wallets<\/b><small>3 wallets<\/small><i aria-hidden="true">▼/);   // collapsed
  assert.equal((html.match(/data-wsel=/g) || []).length, 1, "only Main Wallet is top-level; the imported wallets are not loose items");
  for (const n of [1]) assert.ok(html.includes(ex(n)), "existing standalone wallet still shown");
  assert.ok(html.includes("Exchange Wallets") && html.includes("Trading Wallets"), "existing groups still shown");
});

test("every create/import/select/group mutation reconciles through GET /api/v1/web3/wallets", async () => {
  const H = harness({ wal: grouped() });
  await H.click({ "data-modal": "create" }, ["w3-btn"]);
  await H.click({ "data-mcreate": "" });
  assert.equal(H.gets.length, 1);
  assert.match(H.target.innerHTML, /data-wsel="0xd{40}"/, "created wallet shows without a reload, as a standalone wallet");
  H.handlers.change({ target: Object.assign(H.mk({ "data-mack": "" }, [], H.root), { checked: true }) });
  await H.click({ "data-mdone": "" });
  await H.click({ "data-wsel": ex(1) }, ["w3-wallet"]);
  assert.equal(H.gets.length, 2);
  await H.click({ "data-modal": "groups" });
  H.type("data-gnew", "Fresh Group");
  await H.click({ "data-gcreate": "" });
  assert.equal(H.gets.length, 3);
  await H.click({ "data-gadd": "w_1" });
  H.type("data-gname", "Renamed");
  await H.click({ "data-grename": "" });
  await H.change("data-gmove", "g_x", { "data-gmove": "w_1" });
  await H.click({ "data-gremove": "w_1" });
  await H.click({ "data-gdel": "" }); await H.click({ "data-gdel": "" });
  assert.equal(H.gets.length, 8, "one GET per successful mutation");
  assert.ok(H.gets.every((p) => p === "/api/v1/web3/wallets"));
});

test("refresh keeps group expansion, keeps existing wallets, and drops state for deleted groups", async () => {
  const H = harness({ wal: grouped() });
  await H.click({ "data-gtoggle": "g_x" }, ["w3-ghead"]);
  await H.click({ "data-modal": "create" }, ["w3-btn"]);
  await H.click({ "data-mcreate": "" });
  H.handlers.change({ target: Object.assign(H.mk({ "data-mack": "" }, [], H.root), { checked: true }) });
  await H.click({ "data-mdone": "" });
  assert.ok(H.target.innerHTML.includes(ex(2)), "open group stayed open across the refresh");
  for (const n of [1, 5]) assert.ok(H.target.innerHTML.includes(n === 5 ? "Trading Wallets" : ex(n)));
  // delete the open group: it disappears, its wallets remain as standalone wallets
  await H.click({ "data-modal": "groups" });
  await H.click({ "data-gsel": "g_x" });
  await H.click({ "data-gdel": "" }); await H.click({ "data-gdel": "" });
  await H.click({ "data-mclose": "" });
  const html = listOnly(H.target.innerHTML);
  assert.ok(!html.includes("Exchange Wallets"));
  for (const n of [2, 3, 4]) assert.ok(html.includes(ex(n)), "wallet " + n + " survives its group");
  assert.equal(H.state.wallets.length, 6);
  assert.equal(W.groupOpen({ q: "", grp: "", open: {} }, "g_x"), false);
});

test("a fresh page load rebuilds the same tree from GET /wallets alone", () => {
  const a = W.walletsHtml(gmodel(), S()), b = W.walletsHtml(W.buildModel(pol, txs, tools, NOW, { ok: true, data: JSON.parse(JSON.stringify(grouped())) }), S());
  assert.equal(a, b);
  assert.match(a, /Exchange Wallets/); assert.match(a, /Trading Wallets/);
});

test("secrets never render: only whitelisted public fields reach the Astra Wallets DOM", () => {
  const d = grouped();
  d.wallets[0].private_key = "0x" + KEY_A; d.wallets[1].seed_phrase = "abandon ability able about";
  d.wallets[2].keystore_name = "ksecret-name"; d.wallets[3].secret = KEY_B; d.reveal = { private_key: "0x" + KEY_C };
  const m = W.buildModel(pol, txs, tools, NOW, { ok: true, data: d });
  const html = W.html(m, { ...S(), open: { g_x: true, g_y: true } });
  for (const bad of [KEY_A, KEY_B, KEY_C, "abandon ability", "ksecret-name", "private_key", "seed"]) assert.ok(!html.includes(bad), bad);
  assert.ok(!JSON.stringify(m).includes(KEY_A) && !JSON.stringify(m).includes(KEY_C));
});
