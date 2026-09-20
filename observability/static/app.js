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
  //: "ledger" | "surface" — the ledger of calls, or one call's messages.
  view: "ledger",
  //: The call the fine-grained views are about; null follows the newest.
  focus: null,
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

// "~" says "estimate"; a part with no price on record says "—" and nothing else.
function fmtEstimate(value) {
  return value === null || value === undefined ? "—" : `~${fmtTokens(value)}`;
}

function fmtUsd(value) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  return `$${n.toFixed(n < 0.1 ? 4 : 2)}`;
}

function fmtPct(value) {
  return value === null || value === undefined ? "—" : `${Number(value).toFixed(1)}%`;
}

async function getJSON(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${response.status} ${url}`);
  return response.json();
}

function setStat(id, value, stateName, foot) {
  const card = $(id);
  card.querySelector(".stat-value").textContent = value;
  card.querySelector(".stat-foot").textContent = foot || "";
  card.dataset.state = stateName || "";
}

/* Dollars small enough that two decimals round them to zero keep four
   decimals: a spend of a tenth of a cent reads as $0.0013, not $0.00. */
function fmtMoney(value) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (n === 0) return "$0";
  if (n < 0.01) return `$${Number(n.toFixed(4))}`;
  if (n < 1) return `$${n.toFixed(3)}`;
  return `$${n.toFixed(2)}`;
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
    // The label alone: the closed select is a pill, and a count cut off
    // mid-digit reads as a rendering fault. It rides in the tooltip.
    const option = el("option", null, run.label);
    // "live" keys off the PROCESS that serves the database, not the last
    // write: a quiet bot still has a pid behind it, and telling the reader
    // "idle" while its process is up reads as dead. Write freshness is the
    // chip's job (live / running, quiet / process gone) once the run is open.
    option.title = `${run.process_up ? "live" : "idle"} · `
      + `${run.deployed || "probe"} · ${run.calls} calls`;
    picker.append(option);
  }
  const hidden = (payload.total || 0) - (payload.deployed || 0);
  const scripts = [...new Set(
    payload.runs.map((run) => run.deployed_by).filter(Boolean))];
  const line = $("root-line");
  line.textContent = `${payload.deployed} deployed run${payload.deployed === 1 ? "" : "s"}`
    + (hidden > 0 ? ` · ${hidden} hidden` : "");
  // "deployed" and "hidden" without their objects say nothing: name both, and
  // the launcher scripts that do the deploying.
  line.title = `${payload.total} databases under ${payload.root}`
    + (scripts.length ? `; deployed by ${scripts.join(", ")}` : "")
    + (hidden > 0
      ? ` — ${hidden} probe/experiment runs hidden (tick "probe runs" to list them)`
      : "");
}

/* The database has not been written to since this moment. Held as a local
   instant rather than the server's `idle_s`, so the age ticks on its own and
   survives a clock that disagrees with the server's. */
const liveness = { anchor: 0, processUp: false, pid: null };

//: Seconds of quiet after which a run is no longer "live" (db.ACTIVE_WINDOW_S).
const ACTIVE_WINDOW_S = 180;

function ageText(seconds) {
  if (seconds < 60) return `wrote ${Math.max(0, Math.round(seconds))}s ago`;
  if (seconds < 5400) return `wrote ${Math.round(seconds / 60)}m ago`;
  return `wrote ${(seconds / 3600).toFixed(1)}h ago`;
}

/* The chip said "wrote 0s ago" for as long as the page stayed open: the text
   was rendered once from the payload and only ever rewritten when a NEW call
   landed, so on a quiet run it froze at whatever the age was at load. It is
   a clock now, and it counts up like one. */
function tickLiveness() {
  if (!liveness.anchor) return;
  const chip = $("liveness");
  const idle = (Date.now() - liveness.anchor) / 1000;
  const wrote = ageText(idle);
  if (idle <= ACTIVE_WINDOW_S && liveness.processUp) {
    chip.dataset.state = "live";
    chip.textContent = `live · ${wrote}`;
  } else if (liveness.processUp) {
    chip.dataset.state = "idle";
    chip.textContent = `running, quiet · ${wrote}`;
  } else {
    chip.dataset.state = "stale";
    chip.textContent = `process gone · ${wrote}`;
  }
  chip.title = liveness.processUp
    ? `pid ${liveness.pid} is running; the database is opened read-only`
    : "no live_companion process serves this database";
}

setInterval(tickLiveness, 1000);

function renderHeader(detail) {
  const summary = detail.summary;
  liveness.anchor = Date.now() - (Number(summary.idle_s) || 0) * 1000;
  liveness.processUp = Boolean(summary.process_up);
  liveness.pid = summary.pid;
  tickLiveness();
  // No wall clock here: the reader has one, and the run's own times are on
  // every event and call row already. What the header owes is the DAY, which
  // is a counter into the run and appears nowhere else.
  const clock = detail.clock;
  const day = clock.virtual_day === null || clock.virtual_day === undefined ? "?" : clock.virtual_day;
  $("day-chip").textContent = `day ${day}`;
  $("day-chip").title = "the run's own day counter; its last action was at "
    + `${clock.real_now || "unknown"} (${clock.tz || "timezone unknown"})`;
  // "deepseek/deepseek-v4-flash" names one model; the leading segment is the
  // provider route, which the chip is not about.
  const model = String(summary.model || "");
  $("model-chip").textContent = model ? model.slice(model.lastIndexOf("/") + 1)
    : "model unknown";
  $("model-chip").title = model || "model unknown";
}

/* ------------------------------------------------------------- statsline */
function renderStats(detail) {
  const usage = detail.usage.totals;
  // Rows written before the usage reconciliation store miss=0, which would
  // read as a 100% cache hit; the prompt-denominated share is the truth there.
  const mismatched = usage.ledger_mismatch_calls > 0;
  const hit = mismatched ? usage.prompt_hit_pct : usage.cache_hit_pct;

  // What the run has actually cost, at the tiered rates in harness/pricing.py
  // (cached input, fresh input and output are billed at three prices). An
  // unpriced call is a model with no rate entry, so say so instead of
  // quietly reporting a total that is missing calls.
  const unpriced = usage.unpriced_calls || 0;
  // The bill and the cache hit are one fact read twice: the hit is WHY the
  // bill is what it is, and the saving is only what the bill would otherwise
  // have been. One tile, with the figures it is derived from in the tooltip.
  setStat("stat-spend", `${fmtMoney(usage.cost_usd)} · ${fmtPct(hit)}`,
    hit !== null && hit >= 60 ? "good" : (hit !== null && hit < 30 ? "warn" : ""),
    unpriced ? `${unpriced} unpriced`
      : `${fmtTokens(usage.cached_tokens)} of ${fmtTokens(usage.prompt_tokens)} cached`);
  $("stat-spend").title = "tiered spend: cached input + fresh input + output, "
    + "at the $/1M rates in harness/pricing.py. The prefix cache took it from "
    + `${fmtMoney(usage.uncached_cost_usd)} to ${fmtMoney(usage.cost_usd)}, `
    + `a ${fmtMoney(usage.savings_usd)} saving. Hit rate is `
    + (mismatched
      ? `cached ÷ prompt (${usage.ledger_mismatch_calls} row(s) store a miss count that does not sum to their prompt total)`
      : "provider-reported cached ÷ (cached + fresh)")
    + `: ${fmtTokens(usage.cached_tokens)} of ${fmtTokens(usage.prompt_tokens)} `
    + `prompt tokens over ${usage.calls} calls, ${usage.completion_tokens} out`
    + (unpriced ? ` — ${unpriced} call(s) have no rate for their model` : "");

  // Calls and turns count the same activity at two grains — a turn costs
  // several calls — so they belong in one tile rather than two that invite
  // being read as unrelated totals; "messages" is the rows the run's messages
  // table holds, the same rows the stream lists under kind "message".
  setStat("stat-calls", String(usage.calls), "",
    `${detail.counters.conversation_turns} turns · ${detail.counters.messages} messages`);
  $("stat-calls").title = "model calls on the ledger below; a single "
    + "conversation turn can spend several of them (decide legs, then the reply)";
  // "events" is the state_events table — the engine's own log — NOT the whole
  // Event flow card below, which mixes every kind. Name the rows they are.
  setStat("stat-events", String(detail.counters.state_events), "", "state rows");
  $("stat-events").title = "rows in the run's state_events table — the engine's "
    + "own log (day rollovers, boundary checks, settles, cache and parse notes); "
    + "the Event flow card below adds messages, calls, steers, decisions and more";
  const checks = detail.checks;
  const codes = checks.findings.map((finding) => finding.code);
  setStat("stat-checks",
    checks.counts.error ? `${checks.counts.error} error` : `${checks.counts.warn} warn`,
    checks.counts.error ? "bad" : (checks.counts.warn ? "warn" : "good"),
    codes.join(" · "));
  // Say what the number is made of, so a bare "2 warn" is never a mystery.
  $("stat-checks").title = codes.length
    ? `click to read the full verdicts: ${codes.join(", ")}`
    : "click to read: all clear";
}

/* ---------------------------------------------------------------- context */
function renderContext(payload) {
  const ring = $("ring-fill");
  const circumference = 2 * Math.PI * 16;
  if (!payload.available) {
    $("ring-percent").textContent = "—";
    ring.setAttribute("stroke-dasharray", `0 ${circumference}`);
    $("pressure-line").textContent = payload.reason || "context unavailable";
    $("pressure-sub").textContent = "";
    // No composition, no provenance: clear the tooltips with the figures.
    $("pressure-line").title = "";
    $("pressure-sub").title = "";
    document.querySelector(".ring").title = "";
    $("context-bar").replaceChildren();
    $("context-legend").replaceChildren();
    $("system-parts").replaceChildren(el("li", "empty", "—"));
    $("tool-list").replaceChildren(el("li", "empty", "—"));
    $("surface").replaceChildren();
    $("surface-foot").hidden = true;
    return;
  }

  const pressure = payload.pressure;
  const used = pressure.provider_prompt_tokens || 0;
  const window = payload.context_window || 1;
  const percent = Math.min(100, (used / window) * 100);
  $("ring-percent").textContent = `${(used / window * 100).toFixed(1)}%`;
  ring.setAttribute("stroke-dasharray", `${circumference * percent / 100} ${circumference}`);
  $("pressure-line").textContent =
    `${fmtTokens(used)} / ${fmtTokens(window)} tokens on the model`;
  // The window is the server's assumption, not the model's spec: say which,
  // with the flag that sets it, so "1.00M" never reads as a model fact.
  const windowNote = `window ${fmtTokens(window)} = the server's --context-window `
    + "assumption (default 1,000,000); the model's own capacity is not on file";
  $("pressure-line").title = windowNote;
  document.querySelector(".ring").title = windowNote;
  const anchor = payload.anchor;
  // "provider-anchored" alone is jargon; the numbers behind it are the point:
  // the heuristic estimate, the scale, and the provider's own prompt total.
  $("pressure-sub").title = (anchor.kind === "none"
    ? "no provider usage on this row: the prices are the heuristic estimate alone (~)"
    : `~ = heuristic estimate, scaled ×${anchor.scale} to the provider's own `
      + `${anchor.provider_tokens.toLocaleString()} prompt tokens `
      + `(estimate ${anchor.heuristic_tokens.toLocaleString()})`)
    + (pressure.ledger_ok ? ""
      : " · fresh = prompt − cached, derived because the row's split did not sum");
  $("pressure-sub").textContent =
    `${fmtTokens(pressure.cached_tokens)} cached · ${fmtTokens(pressure.fresh_tokens)} fresh`
    + ` · ${fmtTokens(pressure.completion_tokens)} out`
    + ` · ${anchor.kind === "none" ? "heuristic prices" : "provider-anchored"}`
    + (pressure.ledger_ok ? "" : " · fresh derived from the prompt total");

  const colors = { system: "seg-system", tools: "seg-tools", messages: "seg-messages" };
  const bar = $("context-bar");
  bar.replaceChildren();
  const legend = $("context-legend");
  legend.replaceChildren();
  // A bucket holding a part with no price on record — a tool menu this build
  // does not hold — carries no number: it shows "—" and takes no width in the
  // bar, because a guessed width would read as a measured share.
  const total = payload.buckets.reduce(
    (sum, bucket) => sum + (bucket.priced_tokens || 0), 0) || 1;
  for (const bucket of payload.buckets) {
    const price = bucket.priced_tokens ?? null;
    const segment = el("span", colors[bucket.key] || "");
    segment.style.width = `${price === null ? 0 : price / total * 100}%`;
    segment.title = price === null
      ? `${bucket.label}: price unknown`
      : `${bucket.label}: ${price} tokens`;
    bar.append(segment);
    const row = el("div");
    const dt = el("dt");
    dt.append(el("span", `swatch ${colors[bucket.key]}`), el("span", null, bucket.label));
    row.append(dt, el("dd", null, fmtEstimate(price)));
    legend.append(row);
  }

  // A system block is prose, and a clipped first line inside a tooltip is not
  // a way to read prose. The row names the block; clicking it opens the whole
  // text, the same way a surface node does.
  const parts = $("system-parts");
  parts.replaceChildren();
  payload.system_parts.forEach((part, index) => {
    const row = el("li", "clickable");
    const left = el("span", "name");
    left.append(el("b", "fixed", part.kind), el("span", "preview", part.label));
    row.append(left, el("span", "tokens", fmtEstimate(part.priced_tokens)));
    row.title = "click to read the whole block";
    row.addEventListener("click", () => {
      const { text, preview, ...rest } = part;
      showRawPayload(`system block #${index + 1} · ${part.kind}`,
        { ...rest, content: text || preview || "" });
    });
    parts.append(row);
  });
  if (payload.system_parts.length === 0) parts.append(el("li", "empty", "empty system prompt"));

  $("tools-source").textContent = payload.tools_source;
  const tools = $("tool-list");
  tools.replaceChildren();
  for (const tool of payload.tools) {
    // A section titled "Tool schemas" that shows names and prices only is the
    // title lying: the schema is what the call actually sent, so the row
    // opens it the same way a system block opens its text. A row whose
    // schema this build does not hold says so instead of pretending.
    const row = el("li", tool.schema ? "clickable" : null);
    row.append(el("span", "name", tool.name),
      el("span", "tokens", fmtEstimate(tool.tokens)));
    if (tool.schema) {
      row.title = "click to read the full schema";
      const schema = tool.schema.function || tool.schema;
      row.addEventListener("click", () => showRawPayload(
        `tool schema · ${schema.name || tool.name}`, schema));
    } else if (tool.tokens === null) {
      row.title = "the schema body is not in this build — the call's record kept the name only";
    }
    tools.append(row);
  }
  if (payload.tools.length === 0) tools.append(el("li", "empty", "none on this call"));

  const surface = $("surface");
  surface.replaceChildren();
  const cachedNodes = payload.surface.filter((node) => node.cached).length;
  $("surface-foot").hidden = payload.surface.length === 0;
  $("surface-foot").textContent = `${payload.surface.length} messages · `
    + `${cachedNodes} cached · ${payload.surface.length - cachedNodes} fresh`;
  for (const node of payload.surface) {
    const row = el("li");
    row.dataset.index = String(node.index);
    const name = el("span", "name");
    name.append(el("span", `role role-${node.role}`, node.role),
      el("b", "fixed", `#${node.index} ${node.label}`),
      el("span", "preview", node.preview));
    const right = el("span", "name");
    // "cached prefix" is the byte-proven shared prefix, not the billed hit;
    // the mark says which side of that story it is.
    const mark = el("span", node.cached ? "cached" : "fresh",
      node.cached ? "cached prefix" : "fresh");
    mark.title = node.cached
      ? "byte-identical to the call before: the measured shared prefix"
      : "not byte-identical to the call before";
    right.append(mark, el("span", "tokens", fmtEstimate(node.priced_tokens)));
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
    const cell = el("td", "empty", "no calls yet");
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
    // Date AND clock, exactly like a stream row: a bare clock repeats every
    // day of a multi-day run (calls #1 and #15 both read 00:00:00).
    const when = call.real
      ? `${call.real.slice(5, 10)} ${call.real.slice(11, 19)}`
      : `d${call.day} t${Number(call.t_h).toFixed(2)}`;
    const whenCell = el("td", "when", when);
    whenCell.title = call.real ? call.real.replace("T", " ")
      : `day ${call.day} · t_h ${call.t_h}`;
    row.append(
      el("td", null, `#${call.id}`),
      whenCell,
      (() => {
        const cell = el("td", "leg");
        cell.append(leg);
        // The lane rides the hover, not a second line: the tag alone names
        // the call's leg.
        if (call.lane) cell.title = call.lane;
        return cell;
      })(),
      el("td", null, fmtTokens(call.prompt_tokens)),
      el("td", null, fmtTokens(call.cached_tokens)),
      el("td", hitClass, fmtPct(call.cache_hit_pct)),
      el("td", null, fmtPct(call.prefix_share_pct)),
      el("td", null, fmtTokens(call.completion_tokens)),
      el("td", "note", call.verdict
        || (call.system_stable === false ? "stable prefix changed" : "")),
    );
    // Reading the call is what a row click is for; the neighbouring cards
    // follow it, so the other zooms land on the same request.
    row.addEventListener("click", () => { focusCall(call.id); openEnvelope(call.id); });
    body.append(row);
  }
  renderLedgerFoot();
}

/* A dump with no call row is a real thing — an earlier run, or the bounded
   re-ask, which goes on the wire but records nothing in llm_calls. The dump
   ledger used to say so; with the bytes moved into each call's drawer, the
   count says it here instead of vanishing. */
function renderLedgerFoot() {
  const orphans = wireState.files.filter((file) => !file.call_id).length;
  const foot = $("ledger-foot");
  foot.hidden = false;
  foot.textContent = `${callsList().length} calls · click one to read what the model saw`
    + (orphans ? ` · ${orphans} of ${wireState.files.length} wire dumps have no call row`
      : "");
  foot.title = orphans
    ? "an unmatched dump is an earlier run, or the bounded re-ask: it goes on "
      + "the wire but records no llm_calls row of its own"
    : "";
}

/* ------------------------------------------------------------ granularity */
/* The ledger, the ordered surface and the bytes are the SAME calls at three
   resolutions, so they are one card with one focused call between them rather
   than three cards repeating each other's subject. The context card beside it
   reads that same focus: both cards are always about one request. */
function callsList() {
  return (state.detail && state.detail.calls) || [];
}

function focusedCall() {
  const calls = callsList();
  if (!calls.length) return null;
  if (state.focus === null) return calls[calls.length - 1];
  return calls.find((call) => call.id === state.focus) || calls[calls.length - 1];
}

function setView(view) {
  state.view = view;
  for (const button of document.querySelectorAll("#viewswitch .vbtn")) {
    button.setAttribute("aria-pressed", String(button.dataset.view === view));
  }
  for (const name of ["ledger", "surface"]) {
    $(`view-${name}`).hidden = name !== view;
  }
  renderFocus();
}

/* Point both cards at whatever `state.focus` now names. */
function applyFocus() {
  renderFocus();
  loadFocusContext();
  renderCalls(callsList(), focusedCall() && focusedCall().id);
}

function focusCall(callId, view) {
  const calls = callsList();
  const newest = calls.length ? calls[calls.length - 1].id : null;
  state.focus = callId === newest ? null : callId;
  if (view) setView(view);
  applyFocus();
}

/* No picker of its own: the ledger IS the picker, and every view that shows
   one call says which one. */
function renderFocus() {
  const call = focusedCall();
  const name = call ? `call #${call.id} · ${call.role}` : "no calls yet";
  $("context-call").textContent = name;
  $("surface-note").textContent = call
    ? `${name} — every message it carried, oldest → newest` : name;
}

async function loadFocusContext() {
  const call = focusedCall();
  if (!call) return;
  const payload = await getJSON(
    `/api/run/context?id=${encodeURIComponent(state.runId)}&call=${call.id}`);
  renderContext(payload);
}

/* ------------------------------------------------------------------- flow */
let flowBuffer = [];

/* A mechanism row that repeats tells you nothing the first one did not: the
   same check firing four times is ONE fact. Collapsing runs is what makes the
   rows that do not repeat -- a verdict, a close, a refusal -- findable. */
function collapseRuns(events) {
  const runs = [];
  for (const event of events) {
    const last = runs[runs.length - 1];
    const key = `${event.kind}|${event.label}|${event.detail || ""}`;
    if (last && last.key === key) { last.count += 1; last.event = event; }
    else runs.push({ key, count: 1, event });
  }
  return runs;
}

/* Decision details arrive as a Python repr: {'initiate': True, ...}. The
   verdict is the reason the row is interesting, so it is read out of the
   string and shown as fields instead of one quoted blob. */
function verdictChips(detail) {
  const chips = [];
  const pattern = /'([a-z_]+)':\s*('([^']*)'|True|False|None|-?\d+(?:\.\d+)?)/g;
  let match;
  while ((match = pattern.exec(detail || "")) !== null) {
    let value = match[3] !== undefined ? match[3] : match[2];
    if (value === "True") value = "yes";
    if (value === "False") value = "no";
    if (value === "None") continue;
    if (value.length > 44) value = `${value.slice(0, 42)}…`;
    chips.push(`${match[1]}=${value}`);
  }
  return chips;
}

/* One row of flow as a card: the vertical stream's row anatomy (stamp,
   kind, label, chips, detail) stood on the other axis, newest at the right
   edge where the tail lands. */
function flowCard(event, count) {
  const card = el("article", "flow-slide");
  card.dataset.severity = event.severity;
  card.dataset.seq = String(event.seq);
  // The kind drives the card's badge colour (see the legend), aligned in the
  // right of the head so the stamp column reads straight down the strip.
  card.dataset.kind = event.kind;
  if (count > 1) card.dataset.grouped = "1";
  card.tabIndex = 0;
  // The date is part of the card: a bare clock repeats every day of a run,
  // and an unanchored run stamps "d2 t50.00" rather than a clock at all.
  const stamp = el("time", null,
    event.stamp || (event.real ? event.real.slice(11, 19) : `t${event.t_h}`));
  stamp.title = `day ${event.day} · t_h ${event.t_h}` + (event.real ? ` · ${event.real}` : "");
  const when = el("div", "flow-when");
  when.append(stamp, el("span", "kind", event.kind));
  const label = el("div", "flow-label", event.label);
  if (count > 1) label.append(el("span", "repeat", `×${count}`));
  const raw = event.raw || {};
  const chips = el("div", "flow-chips");
  // The agenda item the card is about, where the row has one: "decision" and
  // the call it made are otherwise only distinguishable by their detail text.
  if (raw.event_label) chips.append(el("span", "chip-item", String(raw.event_label)));
  if (event.kind === "decision") {
    // Two chips, no more: the strip card is one line, and the drawer holds
    // the whole verdict for reading.
    for (const chip of verdictChips(event.detail).slice(0, 2)) {
      chips.append(el("span", "chip-verdict", chip));
    }
  }
  card.append(when, label);
  if (chips.childElementCount) card.append(chips);
  card.append(el("div", "flow-detail", event.detail || ""));
  card.addEventListener("click", () => showRawPayload(`${event.kind} · ${event.label}`, event.raw));
  return card;
}

/* A slim gutter between the cards where the virtual day turns. */
function flowNodes(events) {
  const nodes = [];
  let day = null;
  for (const run of collapseRuns(events)) {
    const event = run.event;
    if (event.day !== day) {
      day = event.day;
      const marker = el("div", "flow-day");
      marker.append(el("strong", null, `day ${day}`),
        el("span", null, event.real ? event.real.slice(5, 10) : ""));
      nodes.push(marker);
    }
    nodes.push(flowCard(event, run.count));
  }
  return nodes;
}

/* Kinds the reader has switched off. Thirty bookkeeping rows can bury the
   three that decided something, so the legend is also the filter. */
const hiddenKinds = new Set();

function renderKindLegend(events) {
  const counts = new Map();
  for (const event of events) {
    counts.set(event.kind, (counts.get(event.kind) || 0) + 1);
  }
  const bar = $("kind-legend");
  bar.replaceChildren();
  for (const [kind, n] of [...counts].sort((a, b) => b[1] - a[1])) {
    const toggle = el("button", "kind-toggle");
    toggle.type = "button";
    toggle.dataset.kind = kind;
    if (hiddenKinds.has(kind)) toggle.dataset.off = "1";
    toggle.append(el("span", "sq"), el("span", null, kind), el("span", "n", n));
    toggle.title = hiddenKinds.has(kind)
      ? `${kind} rows are hidden — click to show them`
      : `${n} ${kind} row(s) — click to hide them`;
    toggle.addEventListener("click", () => {
      if (hiddenKinds.has(kind)) hiddenKinds.delete(kind);
      else hiddenKinds.add(kind);
      renderFlow([], false);
    });
    bar.append(toggle);
  }
}

/* The strip is the run's story left to right; the follow rule is the old
   vertical stream's, on the other axis: follow the tail only when the
   reader was already at the edge they were reading toward. */
function renderFlow(events, replace) {
  const track = $("flow-track");
  if (replace) flowBuffer = [];
  flowBuffer = flowBuffer.concat(events);
  if (flowBuffer.length > 400) flowBuffer = flowBuffer.slice(-400);
  renderKindLegend(flowBuffer);
  const shown = flowBuffer.filter((event) => !hiddenKinds.has(event.kind));
  const atEdge = track.scrollWidth - track.scrollLeft - track.clientWidth < 40;
  track.replaceChildren();
  for (const node of flowNodes(shown)) track.append(node);
  if (!shown.length) {
    track.append(el("p", "flow-empty",
      hiddenKinds.size ? "every kind hidden" : "no events yet"));
  }
  const foot = $("flow-foot");
  foot.hidden = !hiddenKinds.size;
  foot.textContent = foot.hidden
    ? ""
    : `showing ${shown.length} of ${flowBuffer.length} · hiding ${[...hiddenKinds].join(", ")}`;
  // The newest card lives at the right edge, which is where the tail lands.
  if (replace || atEdge) track.scrollLeft = track.scrollWidth;
  updateFlowNav();
}

/* One press, one card (plus the gap). */
function flowStep() {
  const card = $("flow-track").querySelector(".flow-slide");
  return card ? card.getBoundingClientRect().width + 10 : 320;
}

/* The ends disable rather than wrap: the strip has a first card and a
   last one, and the buttons should say which end you are at. */
function updateFlowNav() {
  const track = $("flow-track");
  $("flow-older").disabled = track.scrollLeft <= 1;
  $("flow-newer").disabled =
    track.scrollWidth - track.clientWidth - track.scrollLeft <= 1;
}

function setTailChip(on) {
  const chip = $("tail-chip");
  chip.dataset.state = on ? "on" : "off";
  chip.textContent = on ? "live tail on" : "tail off";
  chip.title = on
    ? "live: events appear as the run records them; the strip follows the newest card while you are at its right edge"
    : "paused — press Refresh to catch up";
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
}

/* The head of a call's drawer. Eight label/value rows in a wrapping grid put
   a long sentence like "no previous envelope" in a cell sized for "89.3%",
   pushed every label away from its own value, and repeated the id the title
   already carries. What identifies the call is a sentence; what measures it
   is four figures. */
/* A decide leg's answer in four words, for the head strip. */
function verdictSummary(verdict) {
  if (!verdict) return "";
  const parts = [];
  if (verdict.reply === true) parts.push("reply");
  if (verdict.reply === false) parts.push("no reply");
  if (verdict.initiate === true) parts.push("initiate");
  if (verdict.initiate === false) parts.push("hold");
  if (verdict.action) parts.push(String(verdict.action));
  if (verdict.terminate_event) parts.push("end event");
  if (!parts.length && verdict.message) parts.push("message");
  return parts.join(" · ") || "see below";
}

function callHeadNodes(detail) {
  const call = detail.call;
  const when = String(call.real || "").replace("T", " ").slice(0, 19);
  const model = String(call.model || "");
  const nodes = [el("p", "drawer-sub", [call.role, call.lane, when,
    model.slice(model.lastIndexOf("/") + 1)].filter(Boolean).join(" · "))];

  const strip = el("div", "factstrip");
  const tile = (label, value, foot, state) => {
    const node = el("div", "stat");
    if (state) node.dataset.state = state;
    node.append(el("span", "stat-label", label), el("span", "stat-value", value));
    if (foot) node.append(el("span", "stat-foot", foot));
    strip.append(node);
  };
  const hit = call.cache_hit_pct;
  // Exact here: the ledger rounds to 3.7K because it is a column of nine, and
  // this drawer is where the reader came for the number itself.
  const exact = (value) => (value === null || value === undefined ? "—"
    : Number(value).toLocaleString());
  tile("prompt", exact(call.prompt_tokens), `${exact(call.cached_tokens)} cached`);
  tile("cache hit", fmtPct(hit), "",
    hit !== null && hit >= 60 ? "good" : (hit !== null && hit < 30 ? "warn" : ""));
  tile("shared prefix", call.prefix_share_pct === null
    ? "first call" : fmtPct(call.prefix_share_pct));
  // A changed stable prefix costs the whole cache, so it is a finding, not a
  // field: it appears only when there is something to say.
  tile("output", exact(call.completion_tokens));
  // What a decide leg ANSWERED. Its `response` column is empty by design, so
  // without this the head said nothing about the only thing the call did.
  if (detail.decision) {
    tile("verdict", verdictSummary(detail.decision.verdict), detail.decision.state_label);
  }
  // `call.verdict` is the CACHE verdict, not the model's: it is set only when
  // the prefix did something worth naming.
  if (call.system_stable === false) tile("system", "prefix changed", "", "bad");
  else if (call.verdict) tile("cache", call.verdict, "", "warn");
  nodes.push(strip);
  return nodes;
}

function showRawPayload(title, payload) {
  const nodes = [];
  if (payload && typeof payload.content === "string" && payload.content) {
    // The row in the list carries a CLIPPED preview; this is the whole node,
    // which is what the drawer is for. A JSON dump alone showed the ellipsis
    // and nothing else, so the reply could not be read anywhere in the app.
    nodes.push(el("h3", null, `Full text (${payload.content.length} chars)`));
    nodes.push(el("div", "full-text", payload.content));
    const rest = { ...payload };
    delete rest.content;
    nodes.push(el("h3", null, "Node"));
    nodes.push(el("pre", null, JSON.stringify(rest, null, 2)));
  } else {
    nodes.push(el("pre", null, JSON.stringify(payload, null, 2)));
  }
  showDrawer(title, nodes);
}

function envelopeNodes(detail) {
  const call = detail.call;
  const nodes = callHeadNodes(detail);
  if (!detail.envelope_available) {
    nodes.push(el("pre", null, "no persisted envelope (hash-only row)"));
    return nodes;
  }
  const envelope = detail.envelope;
  nodes.push(el("h3", null, "System prompt"));
  nodes.push(el("pre", null, envelope.system || ""));
  nodes.push(el("h3", null, `Tool schemas — ${detail.tools_source}`));
  // Two shapes reach here: the OpenAI {type, function} wrapper when the
  // envelope persisted them, and the plain Hermes schema ({name, ...}) when
  // they are rebuilt from harness.tools. The names alone listed WHICH tools
  // the call carried, never what they are — the section's whole subject — so
  // each schema renders as sent.
  const tools = detail.tools || [];
  if (!tools.length) {
    nodes.push(el("pre", null, "none"));
  }
  for (const tool of tools) {
    const schema = tool.function || tool;
    // A recorded name whose schemas this build does not hold has no body to
    // render — say that instead of drawing a degenerate one-key object.
    const bodyless = schema.description === undefined && schema.parameters === undefined;
    nodes.push(el("h3", null, `Schema · ${schema.name || "(unnamed)"}`));
    nodes.push(el("pre", null, bodyless
      ? "the schema body is not in this build — the call's record kept the name only"
      : JSON.stringify(schema, null, 2)));
  }
  nodes.push(el("h3", null, `Messages (${(envelope.messages || []).length})`));
  let index = 0;
  for (const message of envelope.messages || []) {
    const bubble = el("div", `bubble role-${message.role}`);
    const meta = el("div", "meta");
    const text = typeof message.content === "string"
      ? message.content : JSON.stringify(message.content);
    meta.append(el("span", null, `#${index} ${message.role}`),
      el("span", null, `${(text || "").length} chars`));
    bubble.append(meta, el("div", "bubble-text", text || "(tool call)"));
    if (message.tool_calls) bubble.append(el("pre", null, JSON.stringify(message.tool_calls, null, 2)));
    nodes.push(bubble);
    index += 1;
  }
  // Reasoning comes BEFORE the response: the model thought, then answered.
  // Rendering it last read as if it were fed back after the reply, which it
  // never is — it is response-side metadata off the call row
  // (llm_calls.meta), never a member of `messages`.
  if (detail.reasoning) {
    nodes.push(el("h3", null, "Reasoning"));
    nodes.push(el("pre", "aside", detail.reasoning));
  }
  // A decide leg's answer LEAVES as a tool call, so `response` is empty by
  // design; rendering "(empty)" read as a dropped reply. The verdict it
  // actually returned is a decision_records row the server pairs to it.
  const decision = detail.decision;
  if (decision) {
    nodes.push(el("h3", null, ["Verdict", decision.kind, decision.event_label,
      decision.state_label].filter(Boolean).join(" — ")));
    nodes.push(el("pre", null, decision.verdict
      ? JSON.stringify(decision.verdict, null, 2)
      : "the record kept no parsed verdict"));
    nodes.push(el("p", "sub",
      `answered as a ${decision.transport || "?"} tool call`
      + (decision.reask ? " · after a bounded re-ask" : "")
      + " — its tool_call and result appear in the NEXT call's messages, "
      + "which is where the model reads them back"));
  }
  if (detail.response || !decision) {
    nodes.push(el("h3", null, "Response"));
    nodes.push(el("pre", null, detail.response || "(empty)"));
  }
  return nodes;
}

/* One call, read two ways: the envelope as the model reads it, and the exact
   bytes that carried it. They are the same request, so they are one drawer
   with a switch — the dumps were never a subject of their own. */
const drawerCall = { id: null, detail: null, dump: null, mode: "rendered" };

async function openEnvelope(callId) {
  const call = callId || (focusedCall() && focusedCall().id);
  if (!call) return;
  if (drawerCall.id !== call) {
    drawerCall.id = call;
    drawerCall.detail = null;
    drawerCall.dump = null;
  }
  drawerCall.mode = "rendered";
  renderCallDrawer();
  if (!drawerCall.detail) {
    const detail = await getJSON(
      `/api/run/call?id=${encodeURIComponent(state.runId)}&call=${call}`);
    if (drawerCall.id !== call) return;
    drawerCall.detail = detail;
    renderCallDrawer();
  }
}

/* The dump this call's bytes were written to, if one was kept. */
function dumpFor(callId) {
  return wireState.files.find((file) => file.call_id === callId) || null;
}

async function showCallBytes() {
  drawerCall.mode = "bytes";
  const file = dumpFor(drawerCall.id);
  renderCallDrawer();
  if (file && drawerCall.dump === null) {
    const id = drawerCall.id;
    const text = await fetchWireText(file.name);
    if (drawerCall.id !== id) return;
    drawerCall.dump = text;
    renderCallDrawer();
  }
}

function modeSwitch() {
  const row = el("div", "viewswitch drawer-modes");
  for (const [mode, label] of [["rendered", "Rendered"], ["bytes", "Raw bytes"]]) {
    const button = el("button", "vbtn", label);
    button.type = "button";
    button.setAttribute("aria-pressed", String(drawerCall.mode === mode));
    button.addEventListener("click", () => {
      if (mode === "bytes") showCallBytes();
      else { drawerCall.mode = "rendered"; renderCallDrawer(); }
    });
    row.append(button);
  }
  return row;
}

function bytesNodes() {
  const file = dumpFor(drawerCall.id);
  if (!file) {
    return [el("p", "sub", "no wire dump kept for this call — "
      + "HARNESS_WIRE_DUMP was off, or the directory was cleared")];
  }
  const nodes = [el("p", "sub", `${file.name} · ${Math.round(file.size / 1024)} KB`
    + " — the request body exactly as it went out")];
  // Adjacent calls share almost everything, so the diff that says something
  // is against the call before this one.
  const index = wireState.files.indexOf(file);
  const previous = index > 0 ? wireState.files[index - 1] : null;
  if (previous) {
    const button = el("button", "btn btn-small",
      `Diff against ${wireLabel(previous.name)}`);
    button.type = "button";
    button.addEventListener("click", () => openWireDiff(previous.name, file.name));
    const actions = el("div", "drawer-actions");
    actions.append(button);
    nodes.push(actions);
  }
  nodes.push(drawerCall.dump === null ? el("p", "sub", "loading…")
    : el("pre", null, drawerCall.dump));
  return nodes;
}

function renderCallDrawer() {
  const nodes = [modeSwitch()];
  if (drawerCall.mode === "bytes") nodes.push(...bytesNodes());
  else if (drawerCall.detail) nodes.push(...envelopeNodes(drawerCall.detail));
  else nodes.push(el("p", "sub", "loading…"));
  showDrawer(`Call #${drawerCall.id} — what the model saw`, nodes);
}

/* -------------------------------------------------------------------- wire */
const wireState = { files: [] };

/* Which dump belongs to which call, so a drawer can offer the bytes. The
   server matches them by content: the wire directory outlives a reset and
   several decide legs share one virtual second, so neither position nor
   timestamp identifies a dump. */
async function loadWire() {
  const payload = await getJSON(`/api/run/wire?id=${encodeURIComponent(state.runId)}`);
  wireState.files = payload.files || [];
  if (state.detail) renderLedgerFoot();
}

/* `00031_20260913-195144.json` is a filename, not a fact: the number is the
   dump's place in the sequence and the rest is its timestamp. */
function wireLabel(name) {
  const match = /^(\d+)_/.exec(name);
  return match ? `#${Number(match[1])}` : name.replace(/\.json$/, "");
}

async function fetchWireText(name) {
  const url = `/api/run/wire/file?id=${encodeURIComponent(state.runId)}`
    + `&name=${encodeURIComponent(name)}`;
  const response = await fetch(url);
  if (!response.ok) throw new Error(`wire dump ${name}: HTTP ${response.status}`);
  return response.text();
}

async function openWireDiff(a, b) {
  const title = `Wire diff · ${wireLabel(a)} → ${wireLabel(b)}`;
  showDrawer(title, [el("p", "sub", "loading…")]);
  const [aText, bText] = await Promise.all([fetchWireText(a), fetchWireText(b)]);
  showDrawer(title, wireDiffNodes(a, b, aText, bText));
}

/* Line diff by common prefix/suffix strip: adjacent calls share almost
   everything, and what changed in the middle reads as straight -/+ lines. */
function wireDiffNodes(aName, bName, aText, bText) {
  const a = aText.split("\n");
  const b = bText.split("\n");
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start += 1;
  let endA = a.length - 1;
  let endB = b.length - 1;
  while (endA >= start && endB >= start && a[endA] === b[endB]) { endA -= 1; endB -= 1; }
  const lines = [];
  const above = Math.min(start, 4);
  for (let i = start - above; i < start; i += 1) lines.push(`  ${a[i]}`);
  if (start - above > 0) lines.push(`@@ ${start} identical lines above @@`);
  for (let i = start; i <= endA; i += 1) lines.push(`- ${a[i]}`);
  for (let i = start; i <= endB; i += 1) lines.push(`+ ${b[i]}`);
  const below = Math.min(a.length - 1 - endA, 4);
  for (let i = a.length - below; i < a.length; i += 1) lines.push(`  ${a[i]}`);
  if (a.length - 1 - endA > below) {
    lines.push(`@@ ${a.length - 1 - endA - below} identical lines below @@`);
  }
  return [
    el("p", "sub", `${a.length} vs ${b.length} lines · `
      + `−${endA - start + 1} +${endB - start + 1}`),
    el("pre", null, lines.join("\n")),
  ];
}

/* ----------------------------------------------------------------- study */
/* Two cards under the live rows: the CALENDAR (left) and the STUDY panels
   (right) — the same pair the context window and the model calls make above.
   Everything is HTML plus text-free SVG geometry on the app's own type scale:
   a chart's labels must not grow because the window did.
   The panels mark the run's events ON its state functions — reach-outs on the
   energy curve, on the mood line, one mark per message — and leave counting
   to the day the run is long enough to earn it. */
const study = {
  runId: null,
  day: null,          // null follows the run's newest day
  cal: "day",         // "month" | "week" | "day"
  panel: "mood",      // "mood" | "rhythm" | "contact"
  view: null,
};

function hourText(hour) {
  const h = Math.max(0, Math.floor(hour));
  const m = Math.round((hour - h) * 60);
  return `${String(h % 24).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

// A day's last stripe is 24:00, not a second 00:00.
function hourLabel(hour) {
  return hour === 24 ? "24:00" : hourText(hour);
}

function plural(count, noun) {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

const STATUS_COLOR = {
  completed: ["var(--mint-soft)", "var(--mint)"],
  planned: ["var(--lav-soft)", "var(--lav)"],
  in_progress: ["var(--sun-soft)", "var(--sun)"],
  shifted: ["var(--clay-soft)", "var(--clay)"],
  skipped: ["var(--grey-soft)", "var(--grey)"],
};

function statusColors(status) {
  return STATUS_COLOR[status] || ["var(--paper-2)", "var(--grey)"];
}

const PANELS = [
  { key: "mood", label: "Mood" },
  { key: "rhythm", label: "Rhythm" },
  { key: "contact", label: "Contact" },
];

const WEEKDAYS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];

//: A run this long turns the contact panel's marks into a density grid —
//: counts earn their place only once a month of days makes a shape.
const CONTACT_DENSE_DAYS = 21;

/* Cycle phases in the app's own palette: pink is the cycle's colour and also
   the app's "meaning" colour, so the phase tones stay soft. */
const PHASE_TONE = {
  menstrual: ["var(--rose-soft)", "var(--rose)"],
  follicular: ["var(--mint-soft)", "var(--mint)"],
  ovulatory: ["var(--sun-soft)", "var(--sun)"],
  luteal_early: ["var(--lav-soft)", "var(--lav)"],
  luteal_late: ["var(--plum-soft)", "var(--plum)"],
};

function phaseTone(label) {
  return PHASE_TONE[label] || ["var(--grey-soft)", "var(--grey)"];
}

function svgNode(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

function titled(node, text) {
  node.append(svgNode("title"));
  node.lastChild.textContent = text;
  return node;
}

function shortened(text, limit) {
  const value = String(text || "");
  return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
}

/* A run day's date as "Sat 19" — a calendar needs dates, not ordinals. */
function dayLabel(day) {
  const stamp = study.view && study.view.dates ? study.view.dates[day] : null;
  if (!stamp) return `day ${day}`;
  return `${stamp.weekday} ${Number(String(stamp.date).slice(8, 10))}`;
}

function isoOf(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

async function loadStudy(runId) {
  if (study.runId !== runId) {
    study.runId = runId;
    study.day = null;      // a day number from another run means nothing here
  }
  const query = study.day === null ? "" : `&day=${encodeURIComponent(study.day)}`;
  study.view = await getJSON(
    `/api/study/day?id=${encodeURIComponent(runId)}${query}`);
  renderStudy();
}

function renderStudy() {
  if (!study.view) return;
  renderCalendar();
  renderPanel();
}

/* One panel at a time, in the order the questions come: what does she feel,
   what does her body clock say, and when did the two of them talk. */
function renderPanel() {
  const host = $("study-panel");
  host.textContent = "";
  for (const button of $("lever-switch").querySelectorAll(".vbtn")) {
    button.setAttribute("aria-pressed",
      String(button.dataset.panel === study.panel));
  }
  if (!study.view) return;
  if (study.panel === "rhythm") host.append(rhythmChart(study.view));
  else if (study.panel === "contact") host.append(contactChart(study.view));
  else host.append(moodChart(study.view));
}

/* ---------------------------------------------------------- the calendar */
function quietBands([from, to]) {
  return from > to ? [[from, 24], [0, to]] : [[from, to]];
}

/* Blocks as bare boxes: lanes exist only where two blocks would overlap. */
function laidOut(items, day) {
  const placed = items.map((item) => ({
    ...item,
    hour: Number(item.start_t_h) - day * 24,
    end: Number(item.end_t_h) - day * 24,
  })).sort((a, b) => a.hour - b.hour);
  let group = [];
  let groupEnd = -Infinity;
  const flush = () => {
    const lanes = [];
    for (const item of group) {
      let lane = lanes.findIndex((last) => last <= item.hour);
      if (lane === -1) { lanes.push(item.end); lane = lanes.length - 1; }
      lanes[lane] = item.end;
      item.lane = lane;
    }
    for (const item of group) item.lanes = lanes.length;
    group = [];
  };
  for (const item of placed) {
    if (group.length && item.hour >= groupEnd) { flush(); groupEnd = -Infinity; }
    group.push(item);
    groupEnd = Math.max(groupEnd, item.end);
  }
  flush();
  return placed;
}

function hourGutter(futureAfter) {
  const box = el("div", "cal-hours");
  for (let hour = 0; hour <= 24; hour += 2) {
    const label = el("span", null, hourLabel(hour));
    label.style.top = `${(hour / 24) * 100}%`;
    if (futureAfter !== null && futureAfter !== undefined && hour > futureAfter) {
      label.classList.add("is-future");
    }
    box.append(label);
  }
  return box;
}

function renderCalendar() {
  const host = $("cal-panel");
  const title = $("cal-title");
  host.textContent = "";
  const view = study.view;
  for (const button of $("cal-switch").querySelectorAll(".vbtn")) {
    button.setAttribute("aria-pressed", String(button.dataset.cal === study.cal));
  }
  if (!view.span.length && !view.agenda.length) {
    title.textContent = "";
    host.append(el("p", "cal-empty", "this run has nothing recorded yet"));
    return;
  }
  if (study.cal === "day") {
    title.textContent = `day ${view.day} · ${dayLabel(view.day)}`;
    host.append(dayChart(view));
  } else if (study.cal === "week") {
    title.textContent = "this week";
    host.append(weekChart(view));
  } else {
    title.textContent = monthTitle(view);
    host.append(monthChart(view));
  }
}

function dayChart(view) {
  const chart = el("div", "cal-chart");
  const now = view.now_hour;
  const marks = el("div", "cal-marks");
  marks.append(el("span", "cal-lane", "msg"));
  // A burst of messages is one mark per quarter hour, not five overlapping
  // ticks: the count rides on the mark, the detail on its tooltip.
  const buckets = new Map();
  for (const message of view.messages) {
    const key = Math.floor(message.hour * 4);
    const bucket = buckets.get(key) || { hour: key / 4, count: 0, users: 0 };
    bucket.count += 1;
    if (message.role === "user") bucket.users += 1;
    buckets.set(key, bucket);
  }
  for (const bucket of buckets.values()) {
    const tick = el("span", "cal-tick");
    tick.style.top = `calc(${(bucket.hour / 24) * 100}% - 3.5px)`;
    tick.style.height = `${Math.min(12, 5 + bucket.count)}px`;
    tick.style.background = bucket.users && bucket.users < bucket.count
      ? "var(--sky)" : bucket.users ? "var(--ink)" : "var(--sky)";
    if (bucket.users && bucket.users < bucket.count) {
      tick.style.borderLeft = "2px solid var(--ink)";
    }
    tick.title = `${hourText(bucket.hour)}–${hourText(bucket.hour + 0.25)} · ` +
      `${plural(bucket.count, "message")} (you ${bucket.users}, she ` +
      `${bucket.count - bucket.users})`;
    marks.append(tick);
  }

  const column = el("div", "cal-col");
  for (const [from, to] of quietBands(view.quiet_hours)) {
    const band = el("div", "cal-quiet");
    band.style.top = `${(from / 24) * 100}%`;
    band.style.height = `${((to - from) / 24) * 100}%`;
    band.title = `quiet hours ${hourText(from)}–${hourText(to)}`;
    column.append(band);
  }
  if (now !== null && now < 24) {
    const ahead = el("div", "cal-ahead");
    ahead.style.top = `${(now / 24) * 100}%`;
    column.append(ahead);
  }

  for (const item of laidOut(view.agenda, view.day)) {
    const block = el("div", "cal-block");
    const [fill, edge] = statusColors(item.status);
    const future = now !== null && item.hour > now;
    const tall = ((item.end - item.hour) / 24) * 460 >= 30;
    if (future) block.classList.add("is-future");
    if (item.status === "in_progress") block.classList.add("is-active");
    block.style.top = `calc(${(item.hour / 24) * 100}% + 1px)`;
    block.style.height = `calc(${((item.end - item.hour) / 24) * 100}% - 2px)`;
    block.style.minHeight = "16px";
    block.style.left = `${(item.lane / item.lanes) * 100}%`;
    block.style.width = `calc(${100 / item.lanes}% - 6px)`;
    block.style.background = future ? "var(--paper-2)" : fill;
    block.style.borderColor = future ? "var(--line)" : edge;
    if (tall) {
      block.append(el("span", "cal-when",
        `${hourText(item.hour)}–${hourText(item.end)}`));
    }
    block.append(el("span", "cal-what", item.activity));
    block.title = `${hourText(item.hour)}–${hourText(item.end)} · ${item.activity}\n` +
      [item.status, item.source_type,
       item.salience === null ? "" : `salience ${Number(item.salience).toFixed(2)}`,
       item.outcome].filter(Boolean).join(" · ");
    column.append(block);
  }

  if (now !== null) {
    const line = el("div", "cal-now");
    line.style.top = `${(now / 24) * 100}%`;
    line.append(el("b", null, `now ${hourText(now)}`));
    column.append(line);
  }

  const flags = el("div", "cal-flags");
  for (const reach of view.proactive) {
    const flag = el("span", `cal-flag${reach.fired ? "" : " held"}`);
    flag.style.top = `calc(${(reach.hour / 24) * 100}% - 5px)`;
    flag.title = `${reach.fired ? "reached out" : "held back"} · ` +
      hourText(reach.hour) + (reach.reason ? `\n${reach.reason}` : "") +
      (reach.hook ? `\n${reach.hook}` : "");
    flags.append(flag);
  }

  chart.append(hourGutter(now), marks, column, flags);
  return chart;
}

/* Week: Sunday to Saturday of the run's current week, the whole day laid
   out, days and hours that have not happened yet left grey. */
function weekChart(view) {
  const current = view.today === null || view.today === undefined
    ? view.day : view.today;
  const stamp = view.dates ? view.dates[current] : null;
  const chart = el("div", "cal-week");
  const columns = el("div", "cal-days");
  const byDate = {};
  for (const entry of view.span) if (entry.date) byDate[entry.date] = entry;

  const week = [];
  if (stamp) {
    const base = new Date(`${stamp.date}T00:00:00`);
    const start = new Date(base);
    start.setDate(start.getDate() - start.getDay());
    for (let i = 0; i < 7; i++) {
      const date = new Date(start);
      date.setDate(start.getDate() + i);
      week.push(date);
    }
  } else {
    for (const entry of view.span.slice(-7)) week.push(null);
  }

  week.forEach((date, index) => {
    const holder = el("div");
    const entry = date ? byDate[isoOf(date)] : view.span[index];
    const head = el("h4", null, date
      ? `${WEEKDAYS[date.getDay()]} ${date.getDate()}` : `day ${entry ? entry.day : index}`);
    holder.append(head);
    const column = el("div", "cal-col");
    if (entry && entry.day === current) column.classList.add("is-today");
    if (entry && entry.day > current) column.classList.add("is-future-day");
    if (!entry) column.classList.add("is-empty");
    if (entry) {
      column.dataset.day = entry.day;
      column.title = `day ${entry.day}${entry.M === null ? "" : ` · mood M ${entry.M}`}`;
      for (const item of laidOut(entry.agenda.map((block) => ({
        ...block,
        start_t_h: block.hour + entry.day * 24,
        end_t_h: block.end + entry.day * 24,
      })), entry.day)) {
        const [fill, edge] = statusColors(item.status);
        const block = el("div", "cal-block");
        const future = entry.day > current ||
          (entry.day === current &&
            view.now_hour !== null && item.hour > view.now_hour);
        block.style.top = `calc(${(item.hour / 24) * 100}% + 1px)`;
        block.style.height = `calc(${((item.end - item.hour) / 24) * 100}% - 2px)`;
        block.style.minHeight = "7px";
        block.style.left = "2px";
        block.style.width = "calc(100% - 4px)";
        block.style.background = future ? "var(--paper-2)" : fill;
        block.style.borderColor = future ? "var(--line)" : edge;
        block.title = `${hourText(item.hour)}–${hourText(item.end)} · ${item.status}`;
        column.append(block);
      }
      for (const reach of entry.reach_outs) {
        const flag = el("span", `cal-flag aside${reach.fired ? "" : " held"}`);
        flag.style.top = `calc(${(reach.hour / 24) * 100}% - 5px)`;
        flag.title = `${reach.fired ? "reached out" : "held back"} · ` +
          hourText(reach.hour);
        column.append(flag);
      }
    }
    holder.append(column);
    columns.append(holder);
  });

  chart.append(hourGutter(), columns);
  return chart;
}

function monthTitle(view) {
  const current = view.today === null || view.today === undefined
    ? view.day : view.today;
  const stamp = view.dates ? view.dates[current] : null;
  if (!stamp) return "every day so far";
  const date = new Date(`${stamp.date}T00:00:00`);
  return date.toLocaleString(undefined, { month: "long", year: "numeric" });
}

/* Month: a real month grid — weekday columns, dates — with the cycle marked
   on the dates the run lived and its days along a capsule strip below. */
function monthChart(view) {
  const wrap = el("div", "cal-month");
  const current = view.today === null || view.today === undefined
    ? view.day : view.today;
  const stamp = view.dates ? view.dates[current] : null;
  const head = el("div", "cal-weekdays");
  for (const name of WEEKDAYS) head.append(el("span", null, name));
  const grid = el("div", "cal-grid");
  const byDate = {};
  for (const entry of view.span) if (entry.date) byDate[entry.date] = entry;
  const todayIso = stamp ? stamp.date : null;

  if (stamp) {
    const date = new Date(`${stamp.date}T00:00:00`);
    const year = date.getFullYear();
    const month = date.getMonth();
    const first = new Date(year, month, 1);
    const length = new Date(year, month + 1, 0).getDate();
    for (let blank = 0; blank < first.getDay(); blank++) {
      grid.append(el("div", "cal-blank"));
    }
    for (let day = 1; day <= length; day++) {
      const iso = isoOf(new Date(year, month, day));
      const cell = el("div", "cal-date");
      cell.append(el("span", "cal-date-n", day));
      const entry = byDate[iso];
      if (entry) {
        const [tone, edge] = phaseTone(entry.phase_label);
        const dot = el("span", "cal-date-dot");
        dot.style.background = edge;
        dot.style.boxShadow = `0 0 0 3px ${tone}`;
        cell.append(dot);
        cell.classList.add("has-run");
        cell.dataset.day = entry.day;
        cell.title = `day ${entry.day} · ` +
          `${String(entry.phase_label || "no engine row").replace("_", " ")}` +
          `${entry.M === null ? "" : ` · mood M ${entry.M}`}`;
        if (entry.day === current) cell.classList.add("is-today");
        else if (entry.day > current) cell.classList.add("is-future");
      } else if (todayIso && iso > todayIso) {
        cell.classList.add("is-future");
      }
      grid.append(cell);
    }
  }
  wrap.append(head, grid);

  // The cycle band is a second grid over the same weekday columns, so a
  // capsule sits under the date it belongs to — a wrapped grid cannot be
  // reached by a proportional timeline.
  const strip = el("div", "cal-cycle");
  if (stamp) {
    const date = new Date(`${stamp.date}T00:00:00`);
    const year = date.getFullYear();
    const month = date.getMonth();
    const first = new Date(year, month, 1);
    const length = new Date(year, month + 1, 0).getDate();
    for (let blank = 0; blank < first.getDay(); blank++) {
      strip.append(el("span", "cal-cap-blank"));
    }
    for (let day = 1; day <= length; day++) {
      const entry = byDate[isoOf(new Date(year, month, day))];
      const cell = el("span", "cal-cap");
      if (entry) {
        const [, edge] = phaseTone(entry.phase_label);
        if (entry.phase_label) cell.style.background = edge;
        cell.title = `day ${entry.day} (${dayLabel(entry.day)}) · ` +
          `${String(entry.phase_label || "no engine row").replace("_", " ")}` +
          `${entry.cycle_day === null ? "" : ` · day ${entry.cycle_day}`}`;
        if (entry.day === current) cell.classList.add("is-today");
      }
      strip.append(cell);
    }
  } else {
    view.span.forEach((entry) => {
      const [, edge] = phaseTone(entry.phase_label);
      const cell = el("span", "cal-cap");
      cell.style.background = edge;
      cell.style.opacity = entry.phase_label ? "1" : "0.25";
      strip.append(cell);
    });
  }
  wrap.append(strip);
  return wrap;
}

/* ----------------------------------------------------------- the panels */
/* A panel is a plot box on the app's type scale: HTML for everything with
   text, one text-free SVG under the marks for the geometry. */
function panelFrame({ yTicks = [], xLabels = [] }) {
  const box = el("div", "panel-chart");
  const axis = el("div", "panel-y");
  for (const [label, at] of yTicks) {
    const span = el("span", null, label);
    span.style.top = `${at}%`;
    axis.append(span);
  }
  const events = el("div", "panel-events");
  const plot = el("div", "panel-plot");
  const lines = svgNode("svg", {
    class: "panel-lines", viewBox: "0 0 100 100",
    preserveAspectRatio: "none", "aria-hidden": "true",
  });
  plot.append(lines, el("div", "panel-marks"));
  const xAxis = el("div", "panel-x");
  for (const label of xLabels) {
    const span = el("span", null, label.text);
    span.style.left = `${label.at}%`;
    if (label.at <= 0.5) span.style.transform = "none";
    if (label.at >= 99.5) span.style.transform = "translateX(-100%)";
    xAxis.append(span);
  }
  box.append(axis, events, plot, xAxis);
  return { box, plot, lines, marks: plot.lastChild, events };
}

function nearestValue(points, hour) {
  let best = points.length ? points[0][1] : 0;
  let distance = Infinity;
  for (const [h, value] of points) {
    const gap = Math.abs(h - hour);
    if (gap < distance) { distance = gap; best = value; }
  }
  return best;
}

/* Mood: the run's line, the trend it deviates from, phase bands behind it,
   and every reach-out marked on the day it happened. */
function moodChart(view) {
  const series = view.mood_series;
  const days = series.days;
  const frame = panelFrame({
    yTicks: [["10", 0], ["5", 50], ["0", 100]],
    xLabels: days.map((day, index) => ({
      text: dayLabel(day), at: ((index + 0.5) / days.length) * 100,
    })),
  });
  const n = days.length;
  if (!n) {
    frame.plot.append(el("p", "cal-empty", "no engine rows yet"));
    return frame.box;
  }
  const x = (index) => ((index + 0.5) / n) * 100;
  const y = (value) => (1 - Math.max(0, Math.min(10, Number(value))) / 10) * 100;

  const bands = [];
  series.phase.forEach((phase, index) => {
    const [tone] = phaseTone(phase);
    frame.lines.append(titled(svgNode("rect", {
      x: `${(index / n) * 100}`, y: "0", width: `${100 / n}`, height: "100",
      style: `fill:${tone}`, opacity: "0.5",
    }), String(phase || "no engine row").replace("_", " ")));
    const run = bands[bands.length - 1];
    if (run && run.phase === phase) run.length += 1;
    else bands.push({ phase, start: index, length: 1 });
  });
  for (const band of bands) {
    if (!band.phase || (band.length * 100) / n < 11) continue;
    const tag = el("span", "panel-tag", String(band.phase).replace("_", " "));
    tag.style.left = `${((band.start + band.length / 2) / n) * 100}%`;
    tag.style.top = "2px";
    tag.style.color = phaseTone(band.phase)[1];
    frame.marks.append(tag);
  }
  const points = series.M.map((value, index) => [x(index), y(value)]);
  frame.lines.append(svgNode("polygon", {
    points: [[x(0), 100], ...points, [x(n - 1), 100]]
      .map((pair) => pair.join(" ")).join(" "),
    style: "fill:var(--peach)", opacity: "0.13",
  }));
  frame.lines.append(svgNode("polyline", {
    points: points.map((pair) => pair.join(" ")).join(" "),
    fill: "none", "vector-effect": "non-scaling-stroke",
    style: "stroke:var(--peach)", "stroke-width": "2",
  }));
  if (series.expected && series.expected.length === n) {
    frame.lines.append(svgNode("polyline", {
      points: series.expected
        .map((value, index) => [x(index), y(value)].join(" ")).join(" "),
      fill: "none", "vector-effect": "non-scaling-stroke",
      style: "stroke:var(--sky)", "stroke-width": "1.6",
      "stroke-dasharray": "5 4",
    }));
  }

  const name = el("span", "panel-series", "mood");
  name.style.color = "var(--peach)";
  name.style.left = `${x(n - 1)}%`;
  name.style.top = `${y(series.M[n - 1])}%`;
  frame.marks.append(name);
  if (series.expected && series.expected.length === n) {
    const trend = el("span", "panel-series", "trend");
    trend.style.color = "var(--sky)";
    trend.style.left = `${x(n - 1)}%`;
    trend.style.top = `${y(series.expected[n - 1])}%`;
    frame.marks.append(trend);
  }

  for (const event of view.panels.proactive) {
    const index = days.indexOf(event.day);
    if (index < 0) continue;
    if (event.fired) {
      frame.lines.append(titled(svgNode("line", {
        x1: x(index), y1: "0", x2: x(index), y2: y(series.M[index]),
        "vector-effect": "non-scaling-stroke",
        style: "stroke:var(--rose)", "stroke-width": "1",
        "stroke-dasharray": "3 3", opacity: "0.55",
      }), `reached out ${hourText(event.hour)}`));
    }
    const mark = el("span", `ev${event.fired ? "" : " held"}`);
    mark.style.left = `${x(index)}%`;
    mark.title = `day ${event.day} · ${hourText(event.hour)} — ` +
      `${event.fired ? "reached out" : "held back"}` +
      (event.reason ? `\n${event.reason}` : "") +
      (event.hook ? `\n${event.hook}` : "");
    frame.events.append(mark);
    if (event.fired && event.hook) {
      const label = el("span", "ev-label", shortened(event.hook, 24));
      label.style.left = `${x(index)}%`;
      label.title = event.hook;
      frame.events.append(label);
    }
  }
  return frame.box;
}

/* Rhythm: the engine's energy curve for each phase the run has lived, with
   every reach-out decision placed at its own hour on that day's curve. */
function rhythmChart(view) {
  const { curves, current } = view.rhythm;
  const frame = panelFrame({
    yTicks: [["1", 0], ["", 50], ["0", 100]],
    xLabels: [0, 6, 12, 18, 24].map((hour) => ({
      text: String(hour), at: (hour / 24) * 100,
    })),
  });
  if (!curves.length) {
    frame.plot.append(el("p", "cal-empty", "no engine rows yet"));
    return frame.box;
  }
  const quiet = quietBands(view.quiet_hours);
  const widest = quiet.reduce((best, band) =>
    (band[1] - band[0] > best[1] - best[0] ? band : best), quiet[0]);
  for (const [from, to] of quiet) {
    const band = titled(svgNode("rect", {
      x: `${(from / 24) * 100}`, y: "0",
      width: `${((to - from) / 24) * 100}`, height: "100",
      style: "fill:var(--grey-soft)", opacity: "0.45",
    }), `quiet hours ${hourText(from)}–${hourText(to)}`);
    frame.lines.append(band);
    if (from === widest[0]) {
      const tag = el("span", "panel-tag", "quiet");
      tag.style.left = `${(from / 24) * 100 + 1}%`;
      tag.style.top = "2px";
      frame.marks.append(tag);
    }
  }
  for (const curve of curves) {
    const [, edge] = phaseTone(curve.phase);
    const isCurrent = curve.phase === current;
    frame.lines.append(svgNode("polyline", {
      points: curve.points
        .map(([hour, value]) => [
          (hour / 24) * 100, (1 - value) * 100].join(" ")).join(" "),
      fill: "none", "vector-effect": "non-scaling-stroke",
      style: `stroke:${edge}`,
      "stroke-width": isCurrent ? "2" : "1.1",
      opacity: isCurrent ? "0.95" : "0.45",
    }));
  }
  const phases = {};
  for (const entry of view.span) phases[entry.day] = entry.phase_label;
  for (const event of view.panels.proactive) {
    const phase = phases[event.day];
    const curve = curves.find((item) => item.phase === phase);
    if (!curve) continue;
    const value = nearestValue(curve.points, event.hour);
    const dot = el("span", `rh-dot${event.fired ? "" : " held"}`);
    dot.style.left = `${(event.hour / 24) * 100}%`;
    dot.style.top = `${(1 - value) * 100}%`;
    dot.style.borderColor = phaseTone(phase)[1];
    dot.title = `day ${event.day} · ${hourText(event.hour)} · energy ` +
      `${value.toFixed(2)} — ${event.fired ? "reached out" : "held back"}` +
      (event.reason ? `\n${event.reason}` : "") +
      (event.hook ? `\n${event.hook}` : "");
    frame.marks.append(dot);
  }
  return frame.box;
}

/* Contact: one mark per message — who spoke, and at what hour — until the
   run is long enough for a density grid to say something. */
function contactChart(view) {
  const days = view.panels.contact_days;
  const grid = view.panels.contact;
  const dense = days.length >= CONTACT_DENSE_DAYS;
  const frame = panelFrame({
    yTicks: [],
    xLabels: [0, 6, 12, 18, 24].map((hour) => ({
      text: String(hour), at: (hour / 24) * 100,
    })),
  });
  if (!days.length) {
    frame.plot.append(el("p", "cal-empty", "no messages yet"));
    return frame.box;
  }
  const byDay = {};
  for (const entry of view.span) byDay[entry.day] = entry;
  const peak = Math.max(1, ...grid.flat().map((cell) =>
    cell[0] + cell[1] + cell[2]));
  days.forEach((day, index) => {
    const entry = byDay[day] || {};
    const label = dayLabel(day);
    const caption = el("span", null, label);
    caption.style.top = `${((index + 0.5) / days.length) * 100}%`;
    const [, edge] = phaseTone(entry.phase_label || "");
    const chip = el("span", "ct-chip");
    chip.style.background = edge;
    chip.style.opacity = entry.phase_label ? "1" : "0.3";
    caption.prepend(chip);
    frame.box.querySelector(".panel-y").append(caption);

    const row = el("div", "ct-row");
    row.style.top = `${(index / days.length) * 100}%`;
    row.style.height = `${100 / days.length}%`;
    grid[index].forEach((cell, hour) => {
      const total = cell[0] + cell[1] + cell[2];
      if (!total) return;
      const left = (hour / 24) * 100;
      const width = 100 / 24;
      if (dense) {
        const block = el("span", "ct-cell");
        block.style.left = `${left}%`;
        block.style.width = `${width}%`;
        block.style.opacity = String(0.15 + 0.75 * (total / peak));
        block.title = `${label} · ${hourText(hour)} — ` +
          `${plural(total, "message")}`;
        row.append(block);
        return;
      }
      const marks = cell[0] + cell[1] + cell[2];
      const perRow = 4;
      const lines = Math.ceil(marks / perRow);
      for (let i = 0; i < marks; i++) {
        const mark = el("span", "ct-mark");
        const column = i % perRow;
        const line = Math.floor(i / perRow);
        mark.style.left = `calc(${left}% + ${(column + 0.5) * (width / perRow)}%)`;
        mark.style.top = `calc(50% + ${((line - (lines - 1) / 2) * 9).toFixed(1)}px - 2px)`;
        mark.style.background = i < cell[0] ? "var(--ink)"
          : i < cell[0] + cell[1] ? "var(--sky)" : "var(--rose)";
        row.append(mark);
      }
      const tip = el("span", "ct-hit");
      tip.style.left = `${left}%`;
      tip.style.width = `${width}%`;
      tip.title = `${label} · ${hourText(hour)}–${hourText(hour + 1)} — ` +
        `${cell[0]} you, ${cell[1]} her, ${cell[2]} reached out`;
      row.append(tip);
    });
    frame.plot.append(row);
  });
  return frame.box;
}

/* --------------------------------------------------------------- events */
function buildSwitches() {
  const host = $("lever-switch");
  host.textContent = "";
  for (const panel of PANELS) {
    const button = el("button", "vbtn", panel.label);
    button.type = "button";
    button.dataset.panel = panel.key;
    button.setAttribute("aria-pressed", String(panel.key === study.panel));
    button.addEventListener("click", () => {
      study.panel = panel.key;
      for (const other of host.querySelectorAll(".vbtn")) {
        other.setAttribute("aria-pressed",
          String(other.dataset.panel === study.panel));
      }
      renderPanel();
    });
    host.append(button);
  }
  for (const button of $("cal-switch").querySelectorAll(".vbtn")) {
    button.addEventListener("click", () => {
      study.cal = button.dataset.cal;
      renderCalendar();
    });
  }
  $("cal-panel").addEventListener("click", (event) => {
    const hit = event.target.closest("[data-day]");
    if (!hit) return;
    study.day = Number(hit.dataset.day);
    study.cal = "day";
    loadStudy(study.runId).catch((error) => console.warn("study failed", error));
  });
}

/* ------------------------------------------------------------------- boot */
async function loadRun(runId) {
  state.runId = runId;
  const detail = await getJSON(`/api/run?id=${encodeURIComponent(runId)}`);
  state.detail = detail;
  renderHeader(detail);
  renderStats(detail);
  renderContext(detail.context);
  renderCalls(detail.calls, focusedCall() && focusedCall().id);
  renderFlow(detail.events.events, true);
  await loadStudy(runId);
  await loadWire();
  setView(state.view);
  state.cursor = detail.events.latest_seq;
  setTailChip(false);
  if (state.streaming) openStream(runId);
}

function openStream(runId) {
  if (state.source) state.source.close();
  state.source = new EventSource(`/api/run/stream?id=${encodeURIComponent(runId)}`);
  state.source.onopen = () => setTailChip(true);
  state.source.onmessage = async (message) => {
    const probe = JSON.parse(message.data);
    setTailChip(true);
    try {
      const fresh = await getJSON(
        `/api/run/events?id=${encodeURIComponent(runId)}&after=${state.cursor}`);
      if (fresh.events.length) {
        renderFlow(fresh.events, false);
        state.cursor = fresh.latest_seq;
        // A recorded event is a write to the database, so the "wrote N ago"
        // age restarts here — not only when a whole new call lands.
        liveness.anchor = Date.now();
        liveness.processUp = true;
        tickLiveness();
      }
      if (probe.call_id !== (state.detail.summary.calls || 0)) {
        const detail = await getJSON(`/api/run?id=${encodeURIComponent(runId)}`);
        state.detail = detail;
        renderHeader(detail);
        renderStats(detail);
        renderCalls(detail.calls, focusedCall() && focusedCall().id);
        loadStudy(runId).catch((error) => console.warn("study refresh failed", error));
        // Following the newest call means following it into the fine views.
        if (state.focus === null) {
          renderContext(detail.context);
          renderFocus();
          await loadWire();
        }
      }
    } catch (error) {
      console.warn("refresh failed", error);
    }
  };
  state.source.onerror = () => {
    setTailChip(false);
    liveness.processUp = false;
    tickLiveness();
  };
}

async function loadRuns() {
  const query = $("show-all").checked ? "?all=1" : "";
  const payload = await getJSON(`/api/runs${query}`);
  renderRuns(payload);
  return payload;
}

async function boot() {
  // The deep-link tail below still reads this payload; the picker's change
  // handler deliberately does NOT (it maps through state.runs, the list the
  // options were rendered from — index 1 of two is not index 1 of sixty).
  let payload = await loadRuns();
  $("show-all").addEventListener("change", async () => {
    const next = await loadRuns();
    const keep = next.runs.find((run) => run.path === state.runId) || next.runs[0];
    if (keep) {
      $("run-picker").selectedIndex = next.runs.indexOf(keep);
      await loadRun(keep.path);
    }
  });
  // The picker maps through state.runs, never through boot's first payload:
  // with "show all" the option list grows to every database, and a stale
  // list silently loads the wrong run (index 1 of two is not index 1 of
  // sixty).
  $("run-picker").addEventListener("change", (event) => {
    const run = state.runs[event.target.selectedIndex];
    if (run) loadRun(run.path);
  });
  $("refresh").addEventListener("click", () => loadRun(state.runId));
  // The strip's own controls: a page per press, and the ends disable rather
  // than wrap.
  $("flow-older").addEventListener("click", () =>
    $("flow-track").scrollBy({ left: -flowStep(), behavior: "smooth" }));
  $("flow-newer").addEventListener("click", () =>
    $("flow-track").scrollBy({ left: flowStep(), behavior: "smooth" }));
  $("flow-track").addEventListener("scroll", updateFlowNav);
  for (const button of $("viewswitch").querySelectorAll(".vbtn")) {
    button.addEventListener("click", () => setView(button.dataset.view));
  }
  buildSwitches();
  $("drawer-close").addEventListener("click", closeDrawer);
  $("scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeDrawer();
  });
  $("stat-checks").addEventListener("click", () => {
    if (!state.detail) return;
    const findings = state.detail.checks.findings;
    showDrawer("Invariant checks", [
      el("p", "sub", "python -m harness.trace checks"),
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
  const view = LINKED.get("view");
  if (["ledger", "surface"].includes(view)) setView(view);
  // A link to a call opens it the way clicking its row does.
  const linked = Number(LINKED.get("call"));
  if (Number.isInteger(linked) && linked > 0) {
    focusCall(linked);
    await openEnvelope(linked);
    if (LINKED.get("read") === "bytes") await showCallBytes();
  }
}

boot().catch((error) => {
  $("root-line").textContent = `failed to load: ${error}`;
  console.error(error);
});
