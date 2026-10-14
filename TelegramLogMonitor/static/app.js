// Dashboard for the Darkweb OSINT Tool API. All bot text is untrusted: render with textContent only.
(() => {
  const $ = (id) => document.getElementById(id);
  const KEY_STORE = "tlm.apiKey";

  const state = {
    config: null,
    sevColor: {},
    botLabel: {},
    view: "feed", // "feed" | "files"
    filters: { bot: "", severities: new Set(), q: "", outgoing: true },
    offset: 0,
    total: 0,
    pageSize: 100,
    shown: new Map(), // message id -> <li>
  };

  // ---------- API helpers ----------
  function apiKey() {
    try { return localStorage.getItem(KEY_STORE) || ""; } catch { return ""; }
  }
  function setApiKey(k) {
    try { localStorage.setItem(KEY_STORE, k); } catch { /* storage unavailable */ }
  }
  function withKey(url) {
    const k = apiKey();
    return k ? url + (url.includes("?") ? "&" : "?") + "key=" + encodeURIComponent(k) : url;
  }
  async function api(path, opts = {}) {
    const headers = { ...(opts.headers || {}) };
    const k = apiKey();
    if (k) headers["X-API-Key"] = k;
    const res = await fetch(path, { ...opts, headers });
    if (res.status === 401) {
      const entered = prompt("API key required (APP_API_KEY from the server's .env):");
      if (entered) { setApiKey(entered.trim()); return api(path, opts); }
      throw new Error("Unauthorized");
    }
    if (!res.ok) {
      const body = await res.text();
      let detail = body;
      try { detail = JSON.parse(body).detail || body; } catch { /* not JSON */ }
      throw new Error(`${res.status} ${detail}`);
    }
    return res.json();
  }

  // ---------- formatting ----------
  const fmtTime = (iso) => {
    const d = new Date(iso);
    const sameDay = d.toDateString() === new Date().toDateString();
    return sameDay
      ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
      : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  };
  const ago = (iso) => {
    if (!iso) return "never";
    const s = Math.max(0, (Date.now() - new Date(iso)) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  };
  const fmtSize = (bytes) => {
    if (bytes == null) return "size unknown";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0, n = bytes;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(n < 10 && i ? 1 : 0)} ${units[i]}`;
  };
  const fileIcon = (m) => {
    const name = (m.media_name || "").toLowerCase();
    if (m.media_type === "photo" || /\.(png|jpe?g|gif|webp)$/.test(name)) return "🖼️";
    if (/\.(zip|gz|tgz|rar|7z)$/.test(name)) return "🗜️";
    if (/\.(pcap|pcapng|cap)$/.test(name)) return "📡";
    if (/\.(csv|xlsx?)$/.test(name)) return "📊";
    if (/\.(pdf)$/.test(name)) return "📕";
    if (/\.(log|txt|json|xml)$/.test(name)) return "📄";
    return "📎";
  };
  let toastTimer;
  function toast(text) {
    const t = $("toast");
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 5000);
  }
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  // ---------- files ----------
  const fileUrl = (m, inline) => withKey(`/api/messages/${m.id}/file${inline ? "?inline=true" : ""}`);

  function triggerDownload(m) {
    const a = document.createElement("a");
    a.href = fileUrl(m, false);
    a.download = m.media_name || "";
    document.body.append(a);
    a.click();
    a.remove();
  }

  // Download button: saved files download straight away; others are fetched from the source first.
  function downloadButton(m) {
    const btn = el("button", "dl");
    btn.type = "button";
    const limit = state.config.media.on_demand_max_bytes;
    if (!m.has_file && m.media_size != null && m.media_size > limit) {
      btn.textContent = "Too large";
      btn.disabled = true;
      btn.title = `Over the ${fmtSize(limit)} on-demand limit (media.on_demand_max_mb in config.yaml)`;
      return btn;
    }
    btn.textContent = m.has_file ? "Download" : "Fetch & download";
    btn.title = m.has_file ? "Saved on the server" : "Not saved yet - will be pulled from Darkweb";
    btn.addEventListener("click", async () => {
      if (m.has_file) return triggerDownload(m);
      btn.disabled = true;
      btn.classList.add("busy");
      btn.textContent = "Fetching from Darkweb…";
      try {
        const updated = await api(`/api/messages/${m.id}/fetch`, { method: "POST" });
        Object.assign(m, updated);
        triggerDownload(m);
        btn.textContent = "Download";
        btn.title = "Saved on the server";
      } catch (err) {
        btn.textContent = "Retry";
        toast(`Could not get ${m.media_name}: ${err.message.replace(/^\d+ /, "")}`);
      } finally {
        btn.disabled = false;
        btn.classList.remove("busy");
      }
    });
    return btn;
  }

  function fileStatus(m) {
    return `${fmtSize(m.media_size)} · ${m.has_file ? "saved" : "on Darkweb"}`;
  }

  function attachment(m) {
    const frag = document.createDocumentFragment();
    const isImage = m.media_type === "photo" || /^image\/(jpeg|png|gif|webp)$/.test(m.media_mime || "");
    if (isImage && m.has_file && state.config.media.preview_photos) {
      const img = el("img", "thumb");
      img.loading = "lazy";
      img.alt = m.media_name || "photo";
      img.src = fileUrl(m, true);
      img.addEventListener("click", () => window.open(fileUrl(m, true), "_blank", "noopener"));
      frag.append(img);
    }
    const box = el("div", "attachment");
    const info = el("div", "info");
    info.append(el("div", "name", m.media_name || "file"), el("div", "detail", fileStatus(m)));
    box.append(el("span", "icon", fileIcon(m)), info, downloadButton(m));
    frag.append(box);
    return frag;
  }

  function renderFileRow(m) {
    const li = el("li", "msg file-row");
    li.dataset.id = m.id;
    const info = el("div");
    const label = state.botLabel[m.bot] || m.bot;
    const detail = el("div", "detail");
    detail.append(el("span", "bot-name", label), ` · ${fmtTime(m.date)} · ${fileStatus(m)}`);
    info.append(el("div", "name", m.media_name || "file"), detail);
    if (m.text) info.append(el("div", "caption", m.text));
    const actions = el("div", "row-actions");
    actions.append(downloadButton(m), deleteButton(m));
    li.append(el("span", "icon", fileIcon(m)), info, actions);
    return li;
  }

  // ---------- delete ----------
  function removeFromView(id) {
    const li = state.shown.get(id);
    if (!li) return;
    li.remove();
    state.shown.delete(id);
    state.total = Math.max(0, state.total - 1);
    renderCount();
  }

  async function deleteOne(id) {
    await api(`/api/messages/${id}`, { method: "DELETE" });
    removeFromView(id);
    refreshStats();
  }

  // Small ✕ on each row; confirms nothing (single row), reversible only by the bot re-sending.
  function deleteButton(m) {
    const btn = el("button", "row-del", "✕");
    btn.type = "button";
    btn.title = state.view === "files" ? "Delete this file" : "Delete this message";
    btn.setAttribute("aria-label", "Delete");
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      btn.disabled = true;
      try {
        await deleteOne(m.id);
      } catch (err) {
        toast(`Delete failed: ${err.message.replace(/^\d+ /, "")}`);
        btn.disabled = false;
      }
    });
    return btn;
  }

  // ---------- rendering ----------
  const render = (m) => (state.view === "files" ? renderFileRow(m) : renderMessage(m));

  function renderMessage(m) {
    const li = $("msg-tpl").content.firstElementChild.cloneNode(true);
    li.dataset.id = m.id;
    if (m.outgoing) li.classList.add("out");
    const t = li.querySelector("time");
    t.dateTime = m.date;
    t.textContent = fmtTime(m.date);
    t.title = new Date(m.date).toString();
    li.querySelector(".bot").textContent = (m.outgoing ? "You → " : "") + (state.botLabel[m.bot] || m.bot);
    const sev = li.querySelector(".sev");
    if (m.outgoing) sev.remove();
    else {
      sev.textContent = m.severity;
      sev.style.background = state.sevColor[m.severity] || "#5f6368";
    }
    li.querySelector(".edited").hidden = !m.edited_at;
    const text = li.querySelector(".text");
    text.textContent = m.text || (m.media_name ? "" : "(empty message)");
    text.title = "Click to expand";
    text.addEventListener("click", () => li.classList.toggle("open"));

    const extras = li.querySelector(".extras");
    for (const [name, values] of Object.entries(m.extracted || {})) {
      for (const v of values) {
        const tag = el("span", "tag");
        tag.append(el("b", "", name), v);
        tag.title = `Filter by ${v}`;
        tag.addEventListener("click", () => { $("f-q").value = v; applyFilters(); });
        extras.append(tag);
      }
    }
    li.querySelector(".meta").append(deleteButton(m));
    if (m.media_type) li.append(attachment(m));
    return li;
  }

  function renderCount() {
    const noun = state.view === "files" ? "file(s)" : "message(s)";
    $("count").textContent = `${state.total.toLocaleString()} ${noun} · showing ${state.shown.size}`;
    $("empty").hidden = state.shown.size > 0;
    $("more").hidden = state.shown.size >= state.total;
  }

  function renderKpis(stats) {
    const k = $("kpis");
    k.replaceChildren();
    const card = (label, value, hint, color, labelCls) => {
      const c = el("div", "kpi");
      const l = el("div", "label");
      if (color) { const s = el("span", "swatch"); s.style.background = color; l.append(s); }
      l.append(labelCls ? el("span", labelCls, label) : label);
      c.append(l, el("div", "value", Number(value).toLocaleString()));
      if (hint) c.append(el("div", "hint", hint));
      k.append(c);
    };
    card("Stored messages", stats.total, "all bots");
    card("Last 24 hours", stats.last_24h, "from bots");
    card("Files", stats.files.total, `${stats.files.saved} saved on server`);
    $("files-badge").textContent = stats.files.total || "";
    for (const s of state.config.severities) {
      card(s.name[0].toUpperCase() + s.name.slice(1), stats.by_severity_24h[s.name] || 0, "last 24h", s.color);
    }
    for (const b of state.config.bots) {
      const info = stats.by_bot[b.username] || { total: 0, last_message_at: null };
      card(b.label, info.total, `last message ${ago(info.last_message_at)}`, null, "bot-name");
    }

    const bars = $("bars");
    bars.replaceChildren();
    const max = Math.max(1, ...stats.hourly_24h.map((h) => h.count));
    for (const h of stats.hourly_24h) {
      const b = el("div");
      b.style.height = `${(h.count / max) * 100}%`;
      b.title = `${new Date(h.hour).toLocaleString([], { hour: "2-digit", minute: "2-digit", day: "numeric", month: "short" })}: ${h.count}`;
      bars.append(b);
    }
    const first = stats.hourly_24h[0];
    $("activity-range").textContent = first ? `${fmtTime(first.hour)} – now` : "";
  }

  async function refreshStatus() {
    try {
      const s = await api("/api/status");
      const pill = $("collector-pill");
      pill.textContent = `Server ${s.connected ? "connected" : "disconnected"}`;
      pill.className = "pill " + (s.connected ? "ok" : "bad");
      const sl = $("server-live");
      if (sl) {
        sl.classList.toggle("offline", !s.connected);
        sl.querySelector(".txt").textContent = s.connected ? "LIVE" : "OFFLINE";
      }
      const hp = $("health-pill");
      if (hp) {
        hp.classList.toggle("bad", !s.connected);
        hp.querySelector(".txt").textContent = s.connected ? "Server Status : Healthy" : "Server Status : Unhealthy";
      }
      const problems = [s.error, ...Object.entries(s.bots || {})
        .filter(([, v]) => !v.ok).map(([k, v]) => `@${k}: ${v.error}`)].filter(Boolean);
      $("error-banner").hidden = !problems.length;
      $("error-banner").textContent = problems.join(" · ");
    } catch (e) { console.warn(e); }
  }

  async function refreshStats() {
    try { renderKpis(await api("/api/stats")); } catch (e) { console.warn(e); }
  }

  // ---------- filtering & paging ----------
  function queryString() {
    const p = new URLSearchParams();
    const f = state.filters;
    if (f.bot) p.append("bot", f.bot);
    for (const s of f.severities) p.append("severity", s);
    if (f.q) p.set("q", f.q);
    if (!f.outgoing) p.set("include_outgoing", "false");
    if (state.view === "files") p.set("media_only", "true");
    p.set("limit", state.pageSize);
    p.set("offset", state.offset);
    return p.toString();
  }

  function matches(m) {
    const f = state.filters;
    if (f.bot && m.bot !== f.bot) return false;
    if (f.severities.size && !f.severities.has(m.severity)) return false;
    if (!f.outgoing && m.outgoing) return false;
    if (state.view === "files" && !m.media_type) return false;
    if (f.q) {
      const hay = (m.text + " " + (m.media_name || "") + " " + JSON.stringify(m.extracted || {})).toLowerCase();
      if (!hay.includes(f.q.toLowerCase())) return false;
    }
    return true;
  }

  async function loadPage(reset) {
    if (reset) {
      state.offset = 0;
      state.shown.clear();
      $("feed").replaceChildren();
    }
    const data = await api(`/api/messages?${queryString()}`);
    state.total = data.total;
    for (const m of data.items) {
      if (state.shown.has(m.id)) continue;
      const li = render(m);
      state.shown.set(m.id, li);
      $("feed").append(li);
    }
    state.offset += data.items.length;
    renderCount();
  }

  let debounce;
  function applyFilters() {
    const f = state.filters;
    f.bot = $("f-bot").value;
    f.q = $("f-q").value.trim();
    f.outgoing = $("f-out").checked;
    clearTimeout(debounce);
    debounce = setTimeout(() => loadPage(true).catch(console.error), 250);
  }

  // ---------- live stream ----------
  function connectStream() {
    const live = $("live-pill");
    const es = new EventSource(withKey("/api/stream"));
    es.onopen = () => { live.className = "pill ok"; };
    es.onerror = () => { live.className = "pill bad"; };
    es.addEventListener("message", (ev) => {
      if ($("f-pause").checked) return;
      const m = JSON.parse(ev.data);
      if (!matches(m)) return;
      const existing = state.shown.get(m.id);
      const li = render(m);
      if (existing) existing.replaceWith(li); // edited message
      else {
        li.classList.add("new");
        $("feed").prepend(li);
        state.total += 1;
        state.offset += 1;
      }
      state.shown.set(m.id, li);
      renderCount();
    });
    // Another client (or this one) deleted messages.
    es.addEventListener("deleted", (ev) => {
      for (const id of JSON.parse(ev.data).ids || []) removeFromView(id);
    });
    es.addEventListener("reload", () => {
      loadPage(true).catch(console.error);
      refreshStats();
    });
  }

  // Bulk delete: removes everything matching the current view's filters, optionally older than N days.
  async function deleteOld() {
    const age = $("del-age").value;
    const f = state.filters;
    const scope = state.view === "files" ? "file(s)" : "message(s)";
    const when = age ? `older than ${age} day(s)` : "shown here";
    if (!confirm(`Delete ${scope} ${when}? This cannot be undone.`)) return;
    const body = {
      bot: f.bot ? [f.bot] : [],
      severity: [...f.severities],
      q: f.q || null,
      include_outgoing: f.outgoing,
      media_only: state.view === "files",
      older_than_days: age ? Number(age) : null,
    };
    const btn = $("del-btn");
    btn.disabled = true;
    try {
      const r = await api("/api/messages/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      toast(`Deleted ${r.deleted} ${scope}.`);
      await loadPage(true);
      refreshStats();
    } catch (err) {
      toast(`Delete failed: ${err.message.replace(/^\d+ /, "")}`);
    } finally {
      btn.disabled = false;
    }
  }

  // ---------- search command: format selector + intelligence-sweep animation ----------
  const SCAN_STEPS = [
    "Establishing anonymised circuit (Tor relay 3/3)…",
    "Querying darkweb index & paste dumps…",
    "Correlating breach corpora and leak sets…",
    "Extracting matching strings & indicators…",
    "Scoring and de-duplicating results…",
  ];

  function searchConfig() {
    return state.config.search || { trigger_commands: [], formats: [], default_format: "" };
  }

  // A command is a "search" if its first word matches a configured trigger command.
  function isSearchCommand(text) {
    const first = (text || "").trim().split(/\s+/)[0].toLowerCase();
    return first ? searchConfig().trigger_commands.includes(first) : false;
  }

  function chosenFormat() {
    return searchConfig().formats.find((f) => f.id === $("s-format").value) || null;
  }

  function updateFormatHint() {
    const f = chosenFormat();
    $("s-format-hint").textContent = f && f.description ? f.description : "";
  }

  // Show the format selector only while a search command is being typed.
  function toggleSearchOpts() {
    const show = searchConfig().formats.length > 0 && isSearchCommand($("s-text").value);
    $("search-opts").hidden = !show;
  }

  let scanTimers = [];
  function stopScan() {
    scanTimers.forEach(clearTimeout);
    scanTimers = [];
    $("scan").hidden = true;
  }

  // Plays the cyber-intelligence sweep. Resolves when `work` settles (min ~2.4s so it is visible).
  async function runScan(work) {
    const steps = $("scan-steps");
    steps.replaceChildren();
    $("scan").hidden = false;
    scanTimers.forEach(clearTimeout);
    scanTimers = SCAN_STEPS.map((text, i) =>
      setTimeout(() => {
        const li = el("li", "scan-step", text);
        requestAnimationFrame(() => li.classList.add("on"));
        steps.append(li);
      }, i * 480),
    );
    const minDelay = new Promise((r) => setTimeout(r, SCAN_STEPS.length * 480));
    try {
      const [result] = await Promise.all([work, minDelay]);
      return result;
    } finally {
      stopScan();
    }
  }

  // ---------- setup ----------
  async function init() {
    const cfg = await api("/api/config");
    state.config = cfg;
    state.pageSize = cfg.ui.page_size || 100;
    for (const s of cfg.severities) state.sevColor[s.name] = s.color;
    for (const b of cfg.bots) state.botLabel[b.username] = b.label;

    for (const b of cfg.bots) {
      $("f-bot").append(new Option(b.label, b.username));
      $("s-bot").append(new Option(b.label, b.username));
    }
    const search = cfg.search || { formats: [], default_format: "" };
    for (const f of search.formats) {
      $("s-format").append(new Option(f.label, f.id, f.id === search.default_format, f.id === search.default_format));
    }
    updateFormatHint();
    $("s-format").addEventListener("change", updateFormatHint);
    $("s-text").addEventListener("input", toggleSearchOpts);
    for (const s of cfg.severities) {
      const chip = el("button", "chip");
      chip.type = "button";
      chip.setAttribute("aria-pressed", "false");
      const sw = el("span", "swatch");
      sw.style.background = s.color;
      chip.append(sw, s.name);
      chip.addEventListener("click", () => {
        const on = chip.getAttribute("aria-pressed") !== "true";
        chip.setAttribute("aria-pressed", String(on));
        on ? state.filters.severities.add(s.name) : state.filters.severities.delete(s.name);
        applyFilters();
      });
      $("f-sev").append(chip);
    }

    for (const [id, view] of [["tab-feed", "feed"], ["tab-files", "files"]]) {
      $(id).addEventListener("click", () => {
        state.view = view;
        $("tab-feed").setAttribute("aria-selected", String(view === "feed"));
        $("tab-files").setAttribute("aria-selected", String(view === "files"));
        $("f-q").placeholder = view === "files" ? "Search file names and captions…" : "Search text, IP, CVE…";
        loadPage(true).catch(console.error);
      });
    }
    $("f-bot").addEventListener("change", applyFilters);
    $("f-q").addEventListener("input", applyFilters);
    $("f-out").addEventListener("change", applyFilters);
    $("more").addEventListener("click", () => loadPage(false).catch(console.error));
    $("del-btn").addEventListener("click", () => deleteOld().catch(console.error));
    $("send-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = $("s-text");
      const btn = e.submitter;
      const bot = encodeURIComponent($("s-bot").value);
      const search = isSearchCommand(input.value);
      btn.disabled = true;
      try {
        if (search) {
          // Full search flow: the server sends the command, clicks the chosen option button in
          // the bot's reply, and fetches the file it sends back. The animation runs until then.
          const f = chosenFormat();
          const work = api(`/api/bots/${bot}/search`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text: input.value, format: f ? f.id : null }),
          });
          const result = await runScan(work);
          input.value = "";
          toggleSearchOpts();
          if (result && result.file) {
            toast(`File received: ${result.file.media_name}`);
            refreshStats();
            if (state.view === "files") loadPage(true).catch(console.error);
          } else {
            toast("Command sent. The file will appear here when the bot sends it.");
          }
        } else {
          await api(`/api/bots/${bot}/send`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text: input.value }),
          });
          input.value = "";
        }
      } catch (err) {
        stopScan();
        toast(`Failed: ${err.message.replace(/^\d+ /, "")}`);
      } finally {
        btn.disabled = false;
      }
    });

    await Promise.all([loadPage(true), refreshStats(), refreshStatus()]);
    connectStream();
    const every = (cfg.ui.refresh_stats_seconds || 30) * 1000;
    setInterval(() => { refreshStats(); refreshStatus(); }, every);
  }

  init().catch((e) => {
    $("error-banner").hidden = false;
    $("error-banner").textContent = `Failed to load: ${e.message}`;
  });
})();
