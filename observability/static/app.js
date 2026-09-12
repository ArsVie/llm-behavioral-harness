/* Harness observability front-end. Vanilla JS, no build step.
   Everything renders from /api/* payloads; the only state kept here is the
   selected run, the newest event cursor and the open inspector. */

const state = {
  runs: [],
  runId: null,
  detail: null,
  cursor: 0,
  events: [],
  source: null,
  selectedCall: null,
  // `?nostream=1` renders one static frame: no EventSource, so the page
  // reaches network idle (headless capture, tests, no-SSE environments).
  // `?run=<path>&call=<id>` deep-links a run and opens one call's inspector.
  streaming: typeof EventSource !== "undefined"
    && new URLSearchParams(window.location.search).get("nostream") !== "1",
};

const LINKED = new URLSearchParams(window.location.search);

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------- utilities */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function fmtTokens(value) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (Math.abs(n) < 1000) return String(Math.round(n));
  if (Math.abs(n) < 1e6) return `${(n / 1e3).toFixed(n < 1e4 ? 1 : 0)}K`;
  return `${(n / 1e6).toFixed(2)}M`;
}

function fmtUsd(value) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  return `$${n.toFixed(n < 0.1 ? 4 : 2)}`;
}

function fmtPct(value) {
  return value === null || value === undefined ? "—" : `${Number(value).toFixed(1)}%`;
}

function clockText(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

async function getJSON(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${response.status} ${url}`);
  return response.json();
}

function setStat(id, value, stateName) {
  const node = $(id).querySelector(".stat-value");
  node.textContent = value;
  $(id).dataset.state = stateName || "";
}

/* ---------------------------------------------------------------- header */
function renderRuns(payload) {
  state.runs = payload.runs;
  const picker = $("run-picker");
  picker.replaceChildren();
  if (payload.runs.length === 0) {
    picker.append(el("option", null, "no deployed run found"));
    return;
  }
  for (const run of payload.runs) {
    const mark = run.active ? "■ live" : "□ idle";
    const where = run.deployed || "probe";
    picker.append(el("option", null, `${mark} · ${run.label} · ${run.calls} calls · ${where}`));
  }
  const hidden = (payload.total || 0) - (payload.deployed || 0);
  $("root-line").textContent =
    `${payload.deployed} deployed run${payload.deployed === 1 ? "" : "s"}`
    + (hidden > 0 ? ` · ${hidden} probe/experiment hidden` : "")
    + ` · ${payload.total} databases under ${payload.root}`;
}

function renderHeader(detail) {
  const summary = detail.summary;
  const chip = $("liveness");
  const wrote = summary.idle_s < 90
    ? `wrote ${Math.round(summary.idle_s)}s ago`
    : (summary.idle_s < 5400
      ? `wrote ${Math.round(summary.idle_s / 60)}m ago`
      : `wrote ${(summary.idle_s / 3600).toFixed(1)}h ago`);
  if (summary.active) {
    chip.dataset.state = "live";
    chip.textContent = `live · ${wrote}`;
  } else if (summary.process_up) {
    chip.dataset.state = "idle";
    chip.textContent = `running, quiet · ${wrote}`;
  } else {
    chip.dataset.state = "stale";
    chip.textContent = `process gone · ${wrote}`;
  }
  chip.title = summary.process_up
    ? `pid ${summary.pid} is running; the database is opened read-only`
    : "no live_companion process serves this database";
  const clock = detail.clock;
  const runTime = clock.real_now ? clock.real_now.slice(11, 19) : "—";
  const day = clock.virtual_day === null || clock.virtual_day === undefined ? "?" : clock.virtual_day;
  // Two different clocks, labelled: the bot's own (frozen between actions —
  // it only moves when the run does) and the wall clock, which ticks here in
  // the browser, in the run's timezone, and cannot be stale or unreachable.
  $("clock-chip").textContent = `bot day ${day} · ${runTime}`;
  $("clock-chip").title = `the run's own clock at its last action: `
    + `${clock.real_now || "unknown"} (${clock.tz || "timezone unknown"})`;
  startWallClock(clock.tz);
  $("model-chip").textContent = summary.model || "model unknown";
}

let wallTimer = null;

function startWallClock(timeZone) {
  let formatter = null;
  try {
    formatter = new Intl.DateTimeFormat("en-GB", {
      timeZone: timeZone || undefined, hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  } catch (error) {
    formatter = null;
  }
  if (wallTimer) clearInterval(wallTimer);
  const tick = () => {
    const now = new Date();
    $("wall-chip").textContent = formatter
      ? `${formatter.format(now)} now`
      : `${now.toLocaleTimeString()} now`;
    $("wall-chip").title = `your clock${timeZone ? `, shown in the run's timezone (${timeZone})` : ""}`;
  };
  tick();
  wallTimer = setInterval(tick, 1000);
}

/* ------------------------------------------------------------- statsline */
function renderStats(detail) {
  const usage = detail.usage.totals;
  // Rows written before the usage reconciliation store miss=0, which would
  // read as a 100% cache hit; the prompt-denominated share is the truth there.
  const mismatched = usage.ledger_mismatch_calls > 0;
  const hit = mismatched ? usage.prompt_hit_pct : usage.cache_hit_pct;
  setStat("stat-tokens", `${fmtTokens(usage.cached_tokens)} / ${fmtTokens(usage.prompt_tokens)}`,
    hit !== null && hit < 30 ? "warn" : "");
  setStat("stat-cache", fmtPct(hit),
    hit !== null && hit >= 60 ? "good" : (hit !== null && hit < 30 ? "warn" : ""));
  $("stat-cache").title = mismatched
    ? `${usage.ledger_mismatch_calls} row(s) store a miss count that does not sum to their prompt total (legacy accounting): showing cached ÷ prompt`
    : "provider-reported cached ÷ (cached + fresh)";
  setStat("stat-savings", fmtUsd(usage.savings_usd));
  setStat("stat-calls", `${detail.counters.messages} msg · ${usage.calls} calls`);
  setStat("stat-turns", `${detail.counters.conversation_turns} turns`);
  setStat("stat-events", `${detail.counters.state_events} states`);
  const checks = detail.checks;
  setStat("stat-checks",
    checks.counts.error ? `${checks.counts.error} error` : `${checks.counts.warn} warn`,
    checks.counts.error ? "bad" : (checks.counts.warn ? "warn" : "good"));
  // Say what the number is made of, so a bare "2 warn" is never a mystery.
  const codes = checks.findings.map((finding) => finding.code);
  $("stat-checks").title = codes.length
    ? `click to read the full verdicts: ${codes.join(", ")}`
    : "click to read: all clear";
}

/* ---------------------------------------------------------------- context */
function renderContext(payload) {
  const hint = $("context-call-hint");
  const ring = $("ring-fill");
  const circumference = 2 * Math.PI * 16;
  if (!payload.available) {
    hint.textContent = "no call";
    $("ring-percent").textContent = "—";
    ring.setAttribute("stroke-dasharray", `0 ${circumference}`);
    $("pressure-line").textContent = payload.reason || "context unavailable";
    $("pressure-sub").textContent = "";
    $("context-bar").replaceChildren();
    $("context-legend").replaceChildren();
    $("system-parts").replaceChildren(el("li", "empty", "—"));
    $("tool-list").replaceChildren(el("li", "empty", "—"));
    $("surface").replaceChildren();
    return;
  }

  const pressure = payload.pressure;
  const used = pressure.provider_prompt_tokens || 0;
  const window = payload.context_window || 1;
  const percent = Math.min(100, (used / window) * 100);
  hint.textContent = `call #${payload.call.id} · ${payload.call.role}`;
  $("ring-percent").textContent = `${(used / window * 100).toFixed(1)}%`;
  ring.setAttribute("stroke-dasharray", `${circumference * percent / 100} ${circumference}`);
  $("pressure-line").textContent =
    `${fmtTokens(used)} / ${fmtTokens(window)} tokens on the model`;
  $("pressure-sub").textContent =
    `${fmtTokens(pressure.cached_tokens)} cached · ${fmtTokens(pressure.fresh_tokens)} fresh`
    + ` · ${fmtTokens(pressure.completion_tokens)} out`
    + ` · ${payload.anchor.kind === "none" ? "heuristic prices" : "provider-anchored"}`
    + (pressure.ledger_ok ? "" : " · fresh derived from the prompt total");

  const colors = { system: "seg-system", tools: "seg-tools", messages: "seg-messages" };
  const bar = $("context-bar");
  bar.replaceChildren();
  const legend = $("context-legend");
  legend.replaceChildren();
  const total = payload.buckets.reduce((sum, bucket) => sum + bucket.priced_tokens, 0) || 1;
  for (const bucket of payload.buckets) {
    const share = bucket.priced_tokens / total * 100;
    const segment = el("span", colors[bucket.key] || "");
    segment.style.width = `${share}%`;
    segment.title = `${bucket.label}: ${bucket.priced_tokens} tokens`;
    bar.append(segment);
    const row = el("div");
    const dt = el("dt");
    dt.append(el("span", `swatch ${colors[bucket.key]}`), el("span", null, bucket.label));
    row.append(dt, el("dd", null, `~${fmtTokens(bucket.priced_tokens)}`));
    legend.append(row);
  }

  const parts = $("system-parts");
  parts.replaceChildren();
  for (const part of payload.system_parts) {
    const row = el("li");
    const name = el("span", "name");
    name.append(el("b", null, part.kind));
    const preview = el("span", "preview", part.label);
    const left = el("span", "name");
    left.append(name, preview);
    row.append(left, el("span", "tokens", `~${fmtTokens(part.priced_tokens)}`));
    row.title = part.preview;
    parts.append(row);
  }
  if (payload.system_parts.length === 0) parts.append(el("li", "empty", "empty system prompt"));

  $("tools-source").textContent = payload.tools_source;
  const tools = $("tool-list");
  tools.replaceChildren();
  for (const tool of payload.tools) {
    const row = el("li");
    row.append(el("span", "name", tool.name),
      el("span", "tokens", `~${fmtTokens(tool.tokens)}`));
    tools.append(row);
  }
  if (payload.tools.length === 0) tools.append(el("li", "empty", "none on this call"));

  const surface = $("surface");
  surface.replaceChildren();
  for (const node of payload.surface) {
    const row = el("li");
    row.dataset.index = String(node.index);
    const name = el("span", "name");
    name.append(el("span", `role role-${node.role}`, node.role),
      el("b", null, `#${node.index} ${node.label}`),
      el("span", "preview", node.preview));
    const right = el("span", "name");
    right.append(el("span", node.cached ? "cached" : "fresh", node.cached ? "cached prefix" : "fresh"),
      el("span", "tokens", `~${fmtTokens(node.priced_tokens)}`));
    row.append(name, right);
    row.addEventListener("click", () => showRawPayload(`surface node #${node.index}`, node));
    surface.append(row);
  }
}

/* ------------------------------------------------------------------ calls */
function renderCalls(calls, selectedId) {
  const body = $("calls-table").querySelector("tbody");
  body.replaceChildren();
  if (!calls.length) {
    const row = el("tr");
    const cell = el("td", "empty", "no model calls recorded in this run — "
      + "the table fills as soon as the bot makes one");
    cell.colSpan = 9;
    row.append(cell);
    body.append(row);
    return;
  }
  for (const call of [...calls].reverse()) {
    const row = el("tr");
    row.dataset.call = String(call.id);
    if (call.id === selectedId) row.dataset.selected = "true";
    const leg = el("span", `tag ${String(call.role).startsWith("chat") ? "tag-chat" : "tag-decide"}`,
      call.role);
    const hitClass = call.cache_hit_pct !== null && call.cache_hit_pct >= 60 ? "ok" : "";
    row.append(
      el("td", null, `#${call.id}`),
      el("td", null, clockText(call.real)),
      (() => { const cell = el("td", "leg"); cell.append(leg,
        el("span", null, ` ${call.lane || ""}`)); return cell; })(),
      el("td", null, fmtTokens(call.prompt_tokens)),
      el("td", null, fmtTokens(call.cached_tokens)),
      el("td", hitClass, fmtPct(call.cache_hit_pct)),
      el("td", null, fmtPct(call.prefix_share_pct)),
      el("td", null, fmtTokens(call.completion_tokens)),
      el("td", "note", call.verdict
        || (call.system_stable === false ? "stable prefix changed" : "")),
    );
    row.addEventListener("click", () => selectCall(call.id));
    body.append(row);
  }
}

/* ----------------------------------------------------------------- stream */
function eventRow(event) {
  const row = el("li");
  row.dataset.severity = event.severity;
  row.dataset.seq = String(event.seq);
  row.append(el("time", null, event.real ? event.real.slice(11, 19) : `t${event.t_h}`));
  const line = el("div");
  const head = el("div", "line");
  head.append(el("span", "kind", event.kind), el("span", null, event.label));
  line.append(head, el("div", "detail", event.detail || ""));
  row.append(line);
  row.addEventListener("click", () => showRawPayload(`${event.kind} · ${event.label}`, event.raw));
  return row;
}

function renderStream(events, replace) {
  const list = $("stream");
  if (replace) list.replaceChildren();
  for (const event of events) list.append(eventRow(event));
  while (list.children.length > 400) list.removeChild(list.firstElementChild);
  list.scrollTop = list.scrollHeight;
}

/* ----------------------------------------------------------------- drawer */
function showDrawer(title, nodes) {
  $("scrim").hidden = false;
  $("drawer").hidden = false;
  $("drawer-title").textContent = title;
  const body = $("drawer-body");
  body.replaceChildren();
  for (const node of nodes) body.append(node);
}

function closeDrawer() {
  $("drawer").hidden = true;
  $("scrim").hidden = true;
  state.selectedCall = null;
}

function stableText(value) {
  if (value === null || value === undefined) return "not compared (first call)";
  return value ? "yes" : "NO — system changed";
}

function sharedText(value) {
  if (value === null || value === undefined) return "not comparable (no previous envelope)";
  return fmtPct(value);
}

function verdictText(value) {
  return value || "no cache anomaly flagged";
}

function kvList(pairs) {
  const list = el("dl", "kv");
  const wide = new Set(["model", "when"]);
  for (const [key, value] of pairs) {
    const row = el("div", wide.has(key) ? "wide" : null);
    row.append(el("dt", null, key), el("dd", null, value));
    list.append(row);
  }
  return list;
}

function showRawPayload(title, payload) {
  showDrawer(title, [el("pre", null, JSON.stringify(payload, null, 2))]);
}

function envelopeNodes(detail) {
  const nodes = [];
  const call = detail.call;
  nodes.push(kvList([
    ["call", `#${call.id} · ${call.role} · ${call.lane}`],
    ["when", `${call.real || ""} (t_h ${call.t_h})`],
    ["model", call.model || "—"],
    ["prompt / cached", `${call.prompt_tokens} / ${call.cached_tokens}`],
    ["cache hit", fmtPct(call.cache_hit_pct)],
    ["shared prefix", sharedText(call.prefix_share_pct)],
    ["system stable", stableText(call.system_stable)],
    ["verdict", verdictText(call.verdict)],
  ]));
  if (!detail.envelope_available) {
    nodes.push(el("pre", null, "No persisted request envelope for this call\n(hash-only row, invariant 19)."));
    return nodes;
  }
  const envelope = detail.envelope;
  nodes.push(el("h3", null, "System prompt"));
  nodes.push(el("pre", null, envelope.system || ""));
  nodes.push(el("h3", null, `Tool schemas — ${detail.tools_source}`));
  // Two shapes reach here: the OpenAI {type, function} wrapper when the
  // envelope persisted them, and the plain Hermes schema ({name, ...}) when
  // they are rebuilt from harness.tools. Reading only `.function` threw for
  // every rebuilt call and left the drawer stuck on "loading…".
  const tools = detail.tools || [];
  nodes.push(el("pre", null, tools.length
    ? tools.map((tool) => (tool.function || tool).name || "(unnamed)").join("\n")
    : "none"));
  nodes.push(el("h3", null, `Messages (${(envelope.messages || []).length})`));
  let index = 0;
  for (const message of envelope.messages || []) {
    const bubble = el("div", `bubble role-${message.role}`);
    const meta = el("div", "meta");
    const text = typeof message.content === "string"
      ? message.content : JSON.stringify(message.content);
    meta.append(el("span", null, `#${index} ${message.role}`),
      el("span", null, `${(text || "").length} chars`));
    bubble.append(meta, el("div", null, text || "(tool call)"));
    if (message.tool_calls) bubble.append(el("pre", null, JSON.stringify(message.tool_calls, null, 2)));
    nodes.push(bubble);
    index += 1;
  }
  nodes.push(el("h3", null, "Response"));
  nodes.push(el("pre", null, detail.response || "(empty)"));
  if (detail.reasoning) {
    nodes.push(el("h3", null, "Reasoning"));
    nodes.push(el("pre", null, detail.reasoning));
  }
  return nodes;
}

async function selectCall(callId) {
  state.selectedCall = callId;
  if (state.detail) renderCalls(state.detail.calls, callId);
  showDrawer(`Call #${callId}`, [el("p", "sub", "loading…")]);
  const detail = await getJSON(`/api/run/call?id=${encodeURIComponent(state.runId)}&call=${callId}`);
  showDrawer(`Call #${callId} — what the model saw`, envelopeNodes(detail));
}

/* ------------------------------------------------------------------- boot */
async function loadRun(runId) {
  state.runId = runId;
  const detail = await getJSON(`/api/run?id=${encodeURIComponent(runId)}`);
  state.detail = detail;
  renderHeader(detail);
  renderStats(detail);
  renderContext(detail.context);
  renderCalls(detail.calls, state.selectedCall);
  renderStream(detail.events.events, true);
  state.cursor = detail.events.latest_seq;
  if (state.streaming) openStream(runId);
}

function openStream(runId) {
  if (state.source) state.source.close();
  state.source = new EventSource(`/api/run/stream?id=${encodeURIComponent(runId)}`);
  state.source.onmessage = async (message) => {
    const probe = JSON.parse(message.data);
    // A reconnect or a long quiet spell must not leave the chip looking dead.
    const summary = state.detail && state.detail.summary;
    if (summary) {
      $("liveness").dataset.state = summary.active ? "live"
        : (summary.process_up ? "idle" : "stale");
    }
    try {
      const fresh = await getJSON(
        `/api/run/events?id=${encodeURIComponent(runId)}&after=${state.cursor}`);
      if (fresh.events.length) {
        renderStream(fresh.events, false);
        state.cursor = fresh.latest_seq;
      }
      if (probe.call_id !== (state.detail.summary.calls || 0)) {
        const detail = await getJSON(`/api/run?id=${encodeURIComponent(runId)}`);
        state.detail = detail;
        renderHeader(detail);
        renderStats(detail);
        renderCalls(detail.calls, state.selectedCall);
        if (!state.selectedCall) renderContext(detail.context);
      }
    } catch (error) {
      console.warn("refresh failed", error);
    }
  };
  state.source.onerror = () => { $("liveness").dataset.state = "stale"; };
}

async function loadRuns() {
  const query = $("show-all").checked ? "?all=1" : "";
  const payload = await getJSON(`/api/runs${query}`);
  renderRuns(payload);
  return payload;
}

async function boot() {
  let payload = await loadRuns();
  $("show-all").addEventListener("change", async () => {
    const next = await loadRuns();
    const keep = next.runs.find((run) => run.path === state.runId) || next.runs[0];
    if (keep) {
      $("run-picker").selectedIndex = next.runs.indexOf(keep);
      await loadRun(keep.path);
    }
  });
  $("run-picker").addEventListener("change", (event) => {
    const index = event.target.selectedIndex;
    loadRun(payload.runs[index].path);
  });
  $("refresh").addEventListener("click", () => loadRun(state.runId));
  $("drawer-close").addEventListener("click", closeDrawer);
  $("scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeDrawer();
  });
  $("stat-checks").addEventListener("click", () => {
    if (!state.detail) return;
    const findings = state.detail.checks.findings;
    showDrawer("Invariant checks", [
      el("p", "sub", "same verdicts as: python -m harness.trace checks"),
      findings.length
        ? el("pre", null, findings.map((f) => `[${f.severity}] ${f.code}\n  ${f.message}`).join("\n\n"))
        : el("p", null, "all clear"),
    ]);
  });
  // A deep link wins over the newest run; an unknown path falls back to it.
  // A link to a probe run widens the list rather than silently ignoring it.
  const wanted = LINKED.get("run");
  if (wanted && !payload.runs.some((run) => run.path === wanted)) {
    $("show-all").checked = true;
    payload = await loadRuns();
  }
  const index = payload.runs.findIndex((run) => run.path === wanted);
  const initial = index >= 0 ? payload.runs[index] : payload.runs[0];
  if (!initial) return;
  $("run-picker").selectedIndex = index >= 0 ? index : 0;
  await loadRun(initial.path);
  const linked = Number(LINKED.get("call"));
  if (Number.isInteger(linked) && linked > 0) await selectCall(linked);
}

boot().catch((error) => {
  $("root-line").textContent = `failed to load: ${error}`;
  console.error(error);
});
