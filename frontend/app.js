/* SQL Query Agent — vanilla JS front end (no build step). */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v);
    }
    for (const c of children.flat()) if (c != null) node.append(c.nodeType ? c : document.createTextNode(String(c)));
    return node;
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const ICON = {
    check: '<svg viewBox="0 0 24 24"><path d="M5 12l5 5L20 7"/></svg>',
    x: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    dash: '<svg viewBox="0 0 24 24"><path d="M6 12h12"/></svg>',
    spin: '<svg viewBox="0 0 24 24"><path d="M12 3a9 9 0 1 0 9 9"/></svg>',
    trash: '<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>',
  };

  const EXAMPLES = [
    ["Generate", "Show all employees hired after January 2024."],
    ["Generate", "Top 5 customers by total spend, with their state"],
    ["Follow-up", "Show all customers."],
    ["Aggregate", "Monthly revenue in 2025 from delivered orders"],
    ["Optimize", "Optimize: SELECT * FROM Orders o LEFT JOIN Employees e ON e.EmployeeID = o.EmployeeID WHERE strftime('%Y', o.OrderDate) = '2024'"],
    ["Debug", "Why does this fail? SELECT EmpName, COUNT(*) FROM Employees WHERE HireDate > 2023-01-01"],
    ["Explain", "Explain: SELECT c.State, AVG(o.TotalAmount) FROM Customers c JOIN Orders o ON o.CustomerID = c.CustomerID GROUP BY c.State HAVING COUNT(*) > 50"],
    ["Guardrail", "Delete all cancelled orders"],
  ];

  // ------------------------------------------------------------------ storage (per-browser history)
  const store = {
    get(key, fallback) { try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ } },
  };
  const uid = () => (crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2)).replace(/[^A-Za-z0-9_-]/g, "");

  let threads = store.get("sqlagent.threads", []);
  let current = null;           // current thread object
  let busy = false;
  let shown = null;             // response currently in the output panel
  let llmReady = true;

  const saveThreads = () => store.set("sqlagent.threads", threads.slice(0, 50));

  // ------------------------------------------------------------------ SQL highlighting
  const KW = new Set(("select from where and or not in is null as join left right inner outer full cross on using group by order having limit offset " +
    "union all intersect except distinct case when then else end with asc desc between like exists count sum avg min max " +
    "insert update delete drop alter create table index into values set recursive over partition true false cast glob").split(" "));
  function highlight(sql) {
    const re = /(--[^\n]*)|('(?:[^']|'')*')|("(?:[^"]|"")*")|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)(\s*\()?|([\s\S])/g;
    let out = "", m;
    while ((m = re.exec(sql))) {
      if (m[1]) out += `<span class="tk-cmt">${esc(m[1])}</span>`;
      else if (m[2]) out += `<span class="tk-str">${esc(m[2])}</span>`;
      else if (m[3]) out += esc(m[3]);
      else if (m[4]) out += `<span class="tk-num">${m[4]}</span>`;
      else if (m[5]) {
        const word = m[5], lower = word.toLowerCase();
        if (KW.has(lower)) out += `<span class="tk-kw">${esc(word)}</span>${esc(m[6] || "")}`;
        else if (m[6]) out += `<span class="tk-fn">${esc(word)}</span>${esc(m[6])}`;
        else out += esc(word);
      } else out += esc(m[7]);
    }
    return out;
  }

  // Tiny markdown subset for assistant messages (bold, italics, inline code, bullet lists, paragraphs).
  function md(text) {
    const inline = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/\*([^*]+)\*/g, "<em>$1</em>");
    const blocks = String(text || "").trim().split(/\n{2,}/);
    return blocks.map((b) => {
      const lines = b.split("\n");
      if (lines.every((l) => /^\s*[-*]\s+/.test(l))) return "<ul>" + lines.map((l) => `<li>${inline(l.replace(/^\s*[-*]\s+/, ""))}</li>`).join("") + "</ul>";
      return `<p>${lines.map(inline).join("<br>")}</p>`;
    }).join("");
  }

  // ------------------------------------------------------------------ toast
  let toastTimer;
  function toast(msg) {
    const t = $("#toast"); t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 1800);
  }

  // ------------------------------------------------------------------ history sidebar
  function renderHistory() {
    const ul = $("#history"); ul.innerHTML = "";
    if (!threads.length) { ul.append(el("li", { class: "empty" }, "No conversations yet")); return; }
    for (const t of threads) {
      const li = el("li", { class: current && t.id === current.id ? "active" : "" },
        el("button", { class: "open", type: "button", title: t.title, onclick: () => openThread(t.id) }, t.title),
        el("button", { class: "del", type: "button", "aria-label": "Delete conversation", html: ICON.trash, onclick: (e) => { e.stopPropagation(); deleteThread(t.id); } }));
      ul.append(li);
    }
  }

  function newThread() {
    current = { id: uid(), title: "New conversation", messages: [], updatedAt: Date.now() };
    renderThread();
  }

  function openThread(id) {
    current = threads.find((t) => t.id === id) || null;
    if (!current) return newThread();
    renderThread();
    closeSidebar();
  }

  function deleteThread(id) {
    threads = threads.filter((t) => t.id !== id); saveThreads();
    if (current && current.id === id) newThread(); else renderHistory();
  }

  function persistCurrent() {
    if (!current.messages.length) return;
    current.updatedAt = Date.now();
    threads = [current, ...threads.filter((t) => t.id !== current.id)];
    saveThreads(); renderHistory();
  }

  // ------------------------------------------------------------------ chat rendering
  function renderThread() {
    const box = $("#messages"); box.innerHTML = "";
    $("#chat-title").textContent = current.title;
    if (!current.messages.length) box.append(welcome());
    let lastResponse = null;
    for (const m of current.messages) {
      if (m.role === "user") box.append(userMsg(m.text));
      else { box.append(assistantMsg(m.response)); if (m.response && m.response.sql) lastResponse = m.response; }
    }
    lastResponse ? showOutput(lastResponse) : clearOutput();
    renderHistory();
    scrollDown();
  }

  function welcome() {
    return el("div", { class: "welcome" },
      el("h1", {}, "Ask your database anything"),
      el("p", {}, llmReady
        ? "Plain-English questions become validated, read-only SQL over the Retail Co. sample database — with an explanation, performance checks and live results."
        : "The server has no LLM API key configured yet, so questions can't be answered. Set GROQ_API_KEY (or another provider key) on the server and reload."),
      el("div", { class: "chips" }, EXAMPLES.map(([tag, q]) =>
        el("button", { class: "chip", type: "button", onclick: () => send(q) }, el("small", {}, tag), q.length > 62 ? q.slice(0, 60) + "…" : q))));
  }

  const userMsg = (text) => el("div", { class: "msg user" }, el("div", { class: "bubble" }, text));

  const STATUS_TAG = { ok: "SQL ready", rejected: "Can't help with that", clarify: "Needs clarification", error: "Error", info: "" };

  function assistantMsg(r) {
    const status = r.status || "error";
    const bubble = el("div", { class: "bubble" });
    if (STATUS_TAG[status]) bubble.append(el("div", { class: "tag" }, STATUS_TAG[status] + (status === "ok" && r.mode && r.mode !== "generate" ? ` · ${r.mode}` : "")));
    const text = status === "ok" ? (r.explanation || r.message) : r.message;
    bubble.append(el("div", { html: md(text) }));
    if (status === "ok" && r.sql) {
      bubble.append(el("button", { class: "sql-preview", type: "button", title: "Show this query in the output panel", onclick: () => { showOutput(r); setView("output"); } }, r.sql));
      const meta = [];
      if (r.results && !r.results.error) meta.push(`${r.results.row_count}${r.results.truncated ? "+" : ""} rows`);
      if (r.results && r.results.error) meta.push("execution failed");
      if (r.optimization && r.optimization.cost) meta.push(`cost: ${r.optimization.cost.level}`);
      if (r.attempts > 1) meta.push(`self-corrected in ${r.attempts} attempts`);
      if (meta.length) bubble.append(el("div", { class: "meta-row", style: "margin-top:6px" }, meta.join(" · ")));
    }
    return el("div", { class: `msg assistant ${status}` }, bubble);
  }

  function pendingMsg() {
    const steps = el("ul", { class: "steps" });
    const working = el("li", { class: "pending", html: ICON.spin + "<span>Thinking…</span>" });
    steps.append(working);
    const stream = el("div", { class: "streaming", hidden: true });
    const bubble = el("div", { class: "bubble" }, steps, stream);
    const node = el("div", { class: "msg assistant" }, bubble);
    return {
      node,
      step(label) { steps.insertBefore(el("li", { html: ICON.check + `<span>${esc(label)}</span>` }), working); scrollDown(); },
      token(text) { stream.hidden = false; stream.dataset.text = (stream.dataset.text || "") + text; stream.innerHTML = esc(stream.dataset.text) + '<span class="cursor"></span>'; scrollDown(); },
    };
  }

  const scrollDown = () => { const m = $("#messages"); m.scrollTop = m.scrollHeight; };

  // ------------------------------------------------------------------ sending + SSE
  async function send(text) {
    text = (text || "").trim();
    if (!text || busy) return;
    busy = true; $("#send").disabled = true;
    if (!current.messages.length) { $("#messages").innerHTML = ""; current.title = text.length > 48 ? text.slice(0, 45) + "…" : text; $("#chat-title").textContent = current.title; }
    current.messages.push({ role: "user", text });
    $("#messages").append(userMsg(text));
    persistCurrent();
    const pending = pendingMsg();
    $("#messages").append(pending.node); scrollDown();

    let response = null;
    try {
      const res = await fetch("/api/chat/stream", {
        method: "POST", headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ message: text, thread_id: current.id, execute: $("#execute-toggle").checked }),
      });
      if (!res.ok) {
        let detail = `Request failed (${res.status})`;
        try { const j = await res.json(); detail = typeof j.detail === "string" ? j.detail : (j.detail?.[0]?.msg || detail); } catch {}
        throw new Error(detail);
      }
      response = await readSSE(res, pending);
      if (!response) throw new Error("The server closed the connection before answering.");
    } catch (err) {
      response = { status: "error", message: err.message || String(err) };
    }
    pending.node.replaceWith(assistantMsg(response));
    current.messages.push({ role: "assistant", response });
    persistCurrent();
    if (response.status === "ok" && response.sql) showOutput(response);
    busy = false; $("#send").disabled = false; scrollDown();
    if (window.matchMedia("(min-width: 901px)").matches) $("#input").focus();
  }

  async function readSSE(res, pending) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "", result = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
        let event = "message", data = "";
        for (const line of raw.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).replace(/^ /, "");
        }
        if (!data) continue;
        const payload = JSON.parse(data);
        if (event === "step") pending.step(payload.label);
        else if (event === "token") pending.token(payload.text);
        else if (event === "result") result = payload;
        else if (event === "error") result = { status: "error", message: payload.message };
      }
    }
    return result;
  }

  // ------------------------------------------------------------------ output panel
  function clearOutput() {
    shown = null;
    $("#empty-output").hidden = false;
    for (const id of ["#panel-sql", "#panel-explain", "#panel-results", "#panel-checks"]) $(id).hidden = true;
  }

  function showOutput(r) {
    shown = r;
    $("#empty-output").hidden = true;
    // SQL
    $("#panel-sql").hidden = false;
    $("#mode-pill").textContent = r.mode && r.mode !== "generate" ? r.mode : "";
    renderSQL();
    const showOriginal = r.original_sql && (r.mode === "optimize" || r.mode === "debug");
    $("#original-wrap").hidden = !showOriginal;
    if (showOriginal) $("#original-code").innerHTML = highlight(r.original_sql);

    // Explanation, issues, notes
    $("#panel-explain").hidden = false;
    $("#explanation").innerHTML = md(r.explanation || "");
    const issues = $("#issues"); issues.innerHTML = "";
    for (const i of r.issues || []) issues.append(el("div", { class: "issue" }, el("b", {}, i.problem), i.explanation));
    const notes = $("#notes"); notes.innerHTML = "";
    for (const n of r.notes || []) notes.append(el("li", {}, n));

    // Results
    const res = r.results;
    $("#panel-results").hidden = !res;
    if (res) renderResults(r);

    // Validation & performance
    $("#panel-checks").hidden = false;
    renderChecks(r);
  }

  function renderSQL() {
    if (!shown) return;
    const d = $("#dialect").value;
    const sql = (shown.dialects && shown.dialects[d]) || shown.sql;
    $("#sql-code").innerHTML = highlight(sql);
    const base = (shown.dialects && shown.dialects.sqlite) || shown.sql;
    const names = { sqlite: "SQLite", postgres: "PostgreSQL", mysql: "MySQL" };
    $("#dialect-note").textContent = d === "sqlite"
      ? "Executed against SQLite. Switch dialect to see this query rewritten for PostgreSQL or MySQL."
      : sql === base ? `No changes needed — this query is valid ${names[d]} as written.`
      : `Rewritten for ${names[d]}: identifiers are quoted${d === "postgres" ? " (PostgreSQL folds unquoted names to lower case)" : ""} and dialect-specific functions are translated.`;
  }

  function renderResults(r) {
    const res = r.results, wrap = $("#results-table"); wrap.innerHTML = "";
    const eq = r.equivalence, eqBox = $("#equivalence");
    eqBox.hidden = !eq;
    if (eq) {
      const same = eq.same_results === true;
      eqBox.className = "equivalence " + (same ? "same" : "diff");
      eqBox.textContent = eq.error ? `Couldn't compare with the original query: ${eq.error}`
        : same ? `✓ Optimized query returns the same ${eq.optimized_rows} rows as the original (${eq.original_ms} ms → ${eq.optimized_ms} ms).`
        : `⚠ Result differs from the original — ${eq.original_rows !== eq.optimized_rows ? `${eq.original_rows} vs ${eq.optimized_rows} rows` : eq.original_columns !== eq.optimized_columns ? `${eq.original_columns} vs ${eq.optimized_columns} columns` : "same shape, different values"}. Review before using.`;
    }
    if (res.error) {
      $("#results-meta").textContent = "";
      $("#download-csv").hidden = true;
      wrap.append(el("div", { class: "results-error" }, `Execution failed: ${res.error}`));
      return;
    }
    $("#download-csv").hidden = !res.rows.length;
    $("#results-meta").textContent = `${res.row_count}${res.truncated ? " (truncated)" : ""} rows · ${res.elapsed_ms} ms`;
    if (!res.rows.length) { wrap.append(el("div", { class: "muted", style: "padding:12px" }, "The query ran successfully and returned no rows.")); return; }
    const numeric = res.columns.map((_, i) => res.rows.every((row) => row[i] === null || typeof row[i] === "number"));
    const table = el("table", {},
      el("thead", {}, el("tr", {}, res.columns.map((c) => el("th", { scope: "col" }, c)))),
      el("tbody", {}, res.rows.slice(0, 500).map((row) => el("tr", {}, row.map((v, i) =>
        v === null ? el("td", { class: "null" }, "NULL") : el("td", { class: numeric[i] ? "num" : "" }, typeof v === "number" && !Number.isInteger(v) ? v.toFixed(2) : v))))));
    wrap.append(table);
  }

  const CHECK_LABELS = { syntax: "Syntax", read_only: "Read-only", tables: "Tables exist", columns: "Columns exist", joins: "Joins valid", engine: "SQLite compiles" };

  function renderChecks(r) {
    const v = r.validation || { checks: {} };
    const checks = $("#checks"); checks.innerHTML = "";
    for (const [k, label] of Object.entries(CHECK_LABELS)) {
      const val = v.checks ? v.checks[k] : null;
      checks.append(el("span", { class: "check" + (val === false ? " fail" : val == null ? " skip" : ""), html: (val === false ? ICON.x : val == null ? ICON.dash : ICON.check) + esc(label) }));
    }
    const o = r.optimization || {}, cost = o.cost || {};
    const badge = $("#cost-badge");
    badge.className = "cost " + (cost.level || "");
    badge.textContent = cost.level && cost.level !== "unknown" ? `Est. cost: ${cost.level} (~${cost.estimated_rows_examined} rows)` : "";

    const perf = $("#perf"); perf.innerHTML = "";
    const section = (title, items, cls = "", asCode = false) => {
      if (!items || !items.length) return;
      perf.append(el("div", { class: "perf-section" }, el("h3", {}, title),
        el("ul", { class: cls }, items.map((t) => el("li", {}, asCode ? el("code", {}, t) : t)))));
    };
    section("Warnings", v.warnings, "warn-list");
    section("Performance suggestions", o.hints);
    section("Unnecessary joins", o.unnecessary_joins);
    section("Recommended indexes (advice only — never executed)", o.index_recommendations, "", true);
    if (cost.plan && cost.plan.length) perf.append(el("div", { class: "perf-section" }, el("h3", {}, "SQLite query plan"), el("div", { class: "plan" }, cost.plan.join("\n"))));
  }

  // ------------------------------------------------------------------ copy / download
  function download(name, text, type) {
    const a = el("a", { href: URL.createObjectURL(new Blob([text], { type })), download: name });
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  const currentSQL = () => shown && ((shown.dialects && shown.dialects[$("#dialect").value]) || shown.sql);

  $("#copy-sql").addEventListener("click", async () => {
    const sql = currentSQL(); if (!sql) return;
    try { await navigator.clipboard.writeText(sql); toast("SQL copied to clipboard"); }
    catch { const ta = el("textarea", {}, sql); document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove(); toast("SQL copied"); }
  });
  $("#download-sql").addEventListener("click", () => { const sql = currentSQL(); if (sql) download(`query.${$("#dialect").value}.sql`, sql + ";\n", "application/sql"); });
  $("#download-csv").addEventListener("click", () => {
    const res = shown && shown.results; if (!res || !res.rows.length) return;
    const cell = (v) => { if (v === null) return ""; const s = String(v); return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
    const csv = [res.columns.map(cell).join(","), ...res.rows.map((r) => r.map(cell).join(","))].join("\r\n");
    download("results.csv", "﻿" + csv, "text/csv");
  });
  $("#dialect").addEventListener("change", renderSQL);

  // ------------------------------------------------------------------ schema drawer
  let schemaLoaded = false;
  $("#open-schema").addEventListener("click", async () => {
    $("#schema-drawer").showModal(); closeSidebar();
    if (schemaLoaded) return;
    try {
      const data = await (await fetch("/api/schema")).json();
      const body = $("#schema-body"); body.innerHTML = "";
      body.append(el("p", { class: "muted", style: "margin:0" }, "SQLite · the agent may only use these tables and columns."));
      for (const t of data.tables) {
        const fks = Object.fromEntries(t.foreign_keys.map((f) => [f.column, `${f.ref_table}.${f.ref_column}`]));
        const sample = el("div", { class: "schema-sample" }, el("div", { class: "muted" }, "Loading sample rows…"));
        const details = el("details", { class: "schema-table" },
          el("summary", {}, el("b", {}, t.name), el("span", {}, `${t.row_count} rows · ${t.columns.length} columns`)),
          el("div", { class: "schema-section-label" }, "Columns"),
          el("ul", { class: "schema-cols" }, t.columns.map((c) => el("li", {},
            el("code", {}, c.name), el("span", { class: "type" }, c.type),
            c.primary_key ? el("span", { class: "badge" }, "PK") : null,
            fks[c.name] ? el("span", { class: "badge fk" }, "→ " + fks[c.name]) : null,
            c.values && c.values.length ? el("span", { class: "type values" }, "values: " + c.values.join(", ")) : null))),
          el("div", { class: "schema-section-label" }, `Sample rows (first 5 of ${t.row_count})`),
          sample);
        // Fetch sample rows the first time a table is expanded.
        details.addEventListener("toggle", async () => {
          if (!details.open || details.dataset.loaded) return;
          details.dataset.loaded = "1";
          try {
            const s = await (await fetch(`/api/schema/${encodeURIComponent(t.name)}/sample`)).json();
            sample.innerHTML = "";
            sample.append(el("div", { class: "table-wrap" }, el("table", {},
              el("thead", {}, el("tr", {}, s.columns.map((c) => el("th", {}, c)))),
              el("tbody", {}, s.rows.map((row) => el("tr", {}, row.map((v) =>
                v === null ? el("td", { class: "null" }, "NULL") : el("td", {}, v))))))));
          } catch { sample.textContent = "Couldn't load sample rows."; delete details.dataset.loaded; }
        });
        body.append(details);
      }
      schemaLoaded = true;
    } catch { $("#schema-body").textContent = "Couldn't load the schema."; }
  });
  $("#close-schema").addEventListener("click", () => $("#schema-drawer").close());
  $("#schema-drawer").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });

  // ------------------------------------------------------------------ layout controls
  function setView(view) {
    $("#workspace").dataset.view = view;
    document.querySelectorAll(".seg button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  }
  document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  const closeSidebar = () => $("#app").classList.remove("sidebar-open");
  $("#toggle-sidebar").addEventListener("click", () => $("#app").classList.add("sidebar-open"));
  $("#scrim").addEventListener("click", closeSidebar);
  $("#new-chat").addEventListener("click", () => { newThread(); closeSidebar(); setView("chat"); $("#input").focus(); });

  $("#theme-toggle").addEventListener("click", () => {
    const root = document.documentElement;
    const isDark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = isDark ? "light" : "dark";
    try { localStorage.setItem("sqlagent.theme", root.dataset.theme); } catch {}
  });

  const input = $("#input");
  const autosize = () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 180) + "px"; input.style.overflowY = input.scrollHeight > 180 ? "auto" : "hidden"; };
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); } });
  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); const t = input.value; input.value = ""; autosize(); send(t); });

  // ------------------------------------------------------------------ boot
  fetch("/api/health").then((r) => r.json()).then((h) => {
    llmReady = !!h.llm_configured;
    $("#model-badge").textContent = (h.model || "").replace(/^[^:]+:/, "") + (llmReady ? "" : " · no API key");
    if (!current.messages.length) renderThread();
  }).catch(() => { $("#model-badge").textContent = "server unreachable"; });

  newThread();
  if (threads.length && new URLSearchParams(location.search).has("resume")) openThread(threads[0].id);
})();
