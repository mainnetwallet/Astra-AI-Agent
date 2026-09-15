/* Astra plugin: Airdrops — renders the airdrop/task/wallet manager into its
 * own tab and contributes extra dashboard blocks. Self-registers via
 * Astra.register(slug, {...}). */
"use strict";

Astra.register("airdrop", {
  title: "Airdrops",

  /* extended dashboard block: deadlines + overdue */
  dashboard(data) {
    const dl = data.deadlines || [];
    const od = data.overdue || [];
    return `
      <div class="grid2">
        <div class="panel">
          <h3>⏰ Upcoming deadlines (7d)</h3>
          <div class="list">${
            dl.length
              ? dl.map((a) => `<div class="rowitem"><a ${deadlineText(a.deadline)}>
                    <b>${esc(a.name)}</b> — ${esc(a.deadline)} (${deadlineTitle(a.deadline)})</a></div>`).join("")
              : `<div class="empty">Kono deadline nai 7 diner moddhe 🎉</div>`
          }</div>
        </div>
        <div class="panel">
          <h3>🚨 Overdue</h3>
          <div class="list">${
            od.length
              ? od.map((n) => `<div class="rowitem"><a class="hl-overdue"><b>${esc(n)}</b></a></div>`).join("")
              : `<div class="empty">Kono overdue nai 👍</div>`
          }</div>
        </div>
      </div>`;
  },

  /* main tab content */
  render(root) {
    root.innerHTML = `
      <div class="panel">
        <h3>➕ Add airdrop</h3>
        <form id="ad-airdrop-form" class="rowform">
          <input name="name" placeholder="Name*" required>
          <input name="project" placeholder="Project/token">
          <select name="status">
            ${["active", "new", "farming", "claimable", "done", "dropped"]
              .map((s) => `<option>${s}</option>`).join("")}
          </select>
          <input name="deadline" type="date" placeholder="deadline">
          <input name="network" placeholder="Network (ETH/SOL/TON)">
          <input name="reward_type" placeholder="Reward (token/points)">
          <input name="estimated_value" placeholder="Est. value ($)">
          <input name="link" placeholder="Link (url)">
          <input name="phase" placeholder="Phase">
          <button type="submit" class="btn primary">Add</button>
        </form>
      </div>
      <div class="panel">
        <div class="panel-head">
          <h3>📦 Airdrops</h3>
          <div class="filters"><select id="ad-status-filter">
            <option value="">all status</option>
            ${["new", "active", "farming", "claimable", "done", "dropped"]
              .map((s) => `<option>${s}</option>`).join("")}
          </select></div>
        </div>
        <div id="ad-airdrop-list" class="list"></div>
      </div>
      <div class="grid2">
        <div class="panel">
          <h3>✅ Tasks <span class="muted small">(sob airdrop)</span></h3>
          <div id="ad-task-list" class="list"></div>
        </div>
        <div class="panel">
          <h3>👛 Wallets</h3>
          <div id="ad-wallet-list" class="list"></div>
        </div>
      </div>`;

    // ---- airdrops ----
    const reload = async () => {
      await loadAirdrops($("#ad-status-filter")?.value || "");
      loadTasks();
      loadWallets();
      loaders.dashboard && loaders.dashboard();
    };
    $("#ad-airdrop-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const body = Object.fromEntries(fd.entries());
      if (!body.name) return;
      await post("/api/airdrops", body);
      e.target.reset();
      reload();
    });
    $("#ad-status-filter").addEventListener("change", () =>
      loadAirdrops($("#ad-status-filter").value));

    root.addEventListener("click", async (e) => {
      const d = e.target.closest("button[data-deltask]");
      if (d) { await del(`/api/tasks/${d.dataset.deltask}`); loadTasks(); reload(); return; }
      const dw = e.target.closest("button[data-delwallet]");
      if (dw) {
        if (!confirm("Wallet delete korben?")) return;
        await del(`/api/wallets/${dw.dataset.delwallet}`); loadWallets(); reload(); return;
      }
      const da = e.target.closest("button[data-delairdrop]");
      if (da) {
        if (!confirm("Airdrop delete? Sob tasks o delete hobe.")) return;
        await del(`/api/airdrops/${da.dataset.delairdrop}`); reload(); return;
      }
    });
    root.addEventListener("submit", async (e) => {
      if (e.target.classList.contains("ad-miniform")) {
        e.preventDefault();
        const title = e.target.title.value.trim(); if (!title) return;
        await post("/api/tasks", { airdrop_id: +e.target.dataset.aid, title,
                                   category: e.target.category.value });
        e.target.reset();
        loadTasks(); reload();
      }
    });
    root.addEventListener("change", async (e) => {
      const c = e.target.closest("input[data-task]");
      if (c) {
        await patch(`/api/tasks/${c.dataset.task}`, { status: c.checked ? "done" : "pending" });
        loadTasks(); reload();
      }
      const statusSel = e.target.closest("select[data-status]");
      if (statusSel) {
        await patch(`/api/airdrops/${statusSel.dataset.status}`, { status: statusSel.value });
        loaders.dashboard && loaders.dashboard();
      }
      // live wallet address validation hint
      if (e.target.matches("#ad-wallet-addr")) {
        const r = await api("/api/wallet/validate?address=" + encodeURIComponent(e.target.value));
        $("#ad-wallet-valid").textContent = r.valid ? "✅ valid address" : "⚠️ address format check korun";
      }
    });

    reload();

    // ---- internal list renderers ----
    async function loadAirdrops(status = "") {
      const r = await api("/api/airdrops?status=" + status);
      if (!r.ok) return;
      $("#ad-airdrop-list").innerHTML = r.data.length
        ? r.data.map((a) => `
          <div class="rowitem airdrop" data-id="${a.id}">
            <div class="rowmain">
              <div class="rowtitle"><b>${esc(a.name)}</b>
                ${a.project ? `<span class="muted">· ${esc(a.project)}</span>` : ""}
                ${a.estimated_value ? `<span class="tag">≈ $${esc(a.estimated_value)}</span>` : ""}
                ${a.network && a.network !== "TBD" ? `<span class="tag">⛓ ${esc(a.network)}</span>` : ""}
              </div>
              <div class="rowsub">
                ${a.deadline ? `<a ${deadlineText(a.deadline)}>${esc(a.deadline)}${deadlineTitle(a.deadline) ? ` <span class="tinytag">${deadlineTitle(a.deadline)}</span>` : ""}</a>` : `<span class="muted">—</span>`}
                ${a.phase ? ` · ${esc(a.phase)}` : ""}
                ${a.link ? ` · <a href="${esc(a.link)}" target="_blank" rel="noopener">🔗</a>` : ""}
              </div>
              <div class="ad-strip" id="ad-strip-${a.id}"></div>
            </div>
            <div class="rowacts">
              <select class="status-select" data-status="${a.id}">
                ${["new", "active", "farming", "claimable", "done", "dropped"]
                  .map((s) => `<option ${s === a.status ? "selected" : ""}>${s}</option>`).join("")}
              </select>
              <button class="btn mini danger" data-delairdrop="${a.id}">🗑</button>
            </div>
          </div>`).join("")
        : `<div class="empty">Airdrop nai. Upore form theke add korun! 🚀</div>`;

      // per-airdrop mini task strip (lazy)
      for (const el of $$(".ad-strip", root)) {
        const aid = +el.id.replace("ad-strip-", "");
        const tr = await api(`/api/airdrops/${aid}/tasks`);
        if (!tr.ok) continue;
        const name = r.data.find((x) => x.id === aid)?.name || "";
        el.innerHTML = `
          <form class="ad-miniform" data-aid="${aid}">
            <input name="title" placeholder="Task for ${esc(name)} (join tg / follow X)">
            <select name="category">
              ${["social", "onchain", "wallet", "other"].map((c) => `<option>${c}</option>`).join("")}
            </select>
            <button class="btn primary">➕</button>
          </form>` +
          (tr.data.length ? tr.data.map((t) => `
            <div class="minirow ${t.status === "done" ? "done" : ""}">
              <label class="check"><input type="checkbox" data-task="${t.id}" ${t.status === "done" ? "checked" : ""}>
                <span>${esc(t.title)}</span></label>
              <span class="muted">${esc(t.category)}</span>
              <button class="btn mini danger" data-deltask="${t.id}">✕</button>
            </div>`).join("") : `<div class="muted small">Task nai.</div>`);
      }
    }

    async function loadTasks() {
      const r = await api("/api/tasks");
      if (!r.ok) return;
      $("#ad-task-list").innerHTML = r.data.length
        ? r.data.map((t) => `
          <div class="rowitem ${t.status === "done" ? "done" : ""}">
            <label class="check"><input type="checkbox" data-task="${t.id}" ${t.status === "done" ? "checked" : ""}>
            <span><b>${esc(t.title)}</b></span></label>
            <span class="muted">in ${esc(t.airdrop_name)} · ${esc(t.category)}</span>
          </div>`).join("")
        : `<div class="empty">Task nai.</div>`;
    }

    async function loadWallets() {
      const r = await api("/api/wallets");
      if (!r.ok) return;
      $("#ad-wallet-list").innerHTML = r.data.length
        ? r.data.map((w) => `
          <div class="rowitem">
            <div class="rowmain">
              <b>${esc(w.label || "(no label)")}</b> <span class="tag">⛓ ${esc(w.network)}</span>
              <div class="mono small">${esc(w.address)}</div>
              ${w.note ? `<div class="muted small">📝 ${esc(w.note)}</div>` : ""}
            </div>
            <button class="btn mini danger" data-delwallet="${w.id}">🗑</button>
          </div>`).join("")
        : `<div class="empty">Wallet nai. Chat e add korun: 'add wallet <address> label <nick>'.</div>`;
    }
  },
});