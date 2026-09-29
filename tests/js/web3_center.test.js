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
const txs = { ok: true, data: { stats: { by_status: { PREPARED: 2, CONFIRMED: 1 } }, transactions: [
  { tx_id: "tx_a", status: "CONFIRMED", to: "0x3333333333333333333333333333333333333333", value_wei: "50000000000000000", chain_id: 1, created_at: "2026-09-29 10:00:00" },
  { tx_id: "tx_b", status: "PREPARED", to: "0x4444444444444444444444444444444444444444", value_wei: "0", chain_id: 8453, created_at: "2026-09-28 09:00:00", error: "<img src=x onerror=alert(1)>" } ] } };
const tools = { ok: true, data: { tools: [
  { name: "token_balance", category: "web3" }, { name: "tx_prepare", category: "web3" }, { name: "browser_open", category: "browser" }] } };

test("model uses only real payload data", () => {
  const m = W.buildModel(pol, txs, tools, NOW);
  assert.strictEqual(m.wallets.length, 2);
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
  assert.match(h, /No wallets are registered/);
  assert.match(h, /Web3 unavailable/);
  assert.match(h, /No transactions yet/);
  assert.match(h, /No Web3 tools are registered/);
  assert.ok(!/\$\d/.test(h), "no dollar amounts may appear without backend data");
  assert.ok(!/Trading Wallet|DeFi Wallet|NFT Wallet|Main Wallet/.test(h));
});

test("unsupported features are disabled, supported ones are enabled", () => {
  const h = W.html(W.buildModel(pol, txs, tools, NOW), S());
  for (const l of ["Import Wallet", "Create Wallet", "Manage Groups", "Swap", "Bridge", "Buy", "Sell", "Stake"])
    assert.match(h, new RegExp(`<button class="w3-btn [^"]*" disabled title="[^"]*">${l}</button>`), l);
  assert.match(h, /data-act="tx_prepare">Send/);
  assert.match(h, /data-copy="0x1111[^"]*">Receive/);
});

test("all backend strings are escaped", () => {
  const h = W.html(W.buildModel(pol, txs, tools, NOW), S());
  assert.ok(!h.includes("<img src=x"), "raw tx.error must not reach the DOM");
  assert.match(h, /&lt;img src=x/);
});

test("wallet search filters and network filter applies to transactions", () => {
  const m = W.buildModel(pol, txs, tools, NOW);
  assert.ok(!W.walletsHtml(m, { ...S(), q: "2222" }).includes("0x1111"));
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
  W.render(target, W.buildModel(pol, txs, tools, NOW), { toChat: (t) => prompts.push(t) });
  const container = mk({ "data-view": "overview" }, ["w3"], null);
  // an agent-action button nested inside the container
  handlers.click({ target: mk({ "data-act": "token_balance" }, ["w3-action"], container) });
  assert.strictEqual(prompts.length, 1);
  assert.match(prompts[0], /token balance of 0x1111/);
  // a real tab button still switches view
  handlers.click({ target: mk({ "data-view": "wallets" }, ["w3-tab"], container) });
  assert.match(target.innerHTML, /data-view="wallets"/);
  // wallet selection is honoured
  handlers.click({ target: mk({ "data-wsel": "0x2222222222222222222222222222222222222222" }, ["w3-wallet"], container) });
  handlers.click({ target: mk({ "data-act": "tx_prepare" }, ["w3-btn"], container) });
  assert.match(prompts[1], /from 0x2222/);
});
