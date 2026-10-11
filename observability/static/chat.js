// /chat: the live conversation (the same one Telegram carries), her sprite,
// and the call button. History comes from the database (/api/chat/history):
// his and her lines plus the call cards — a call's start opens one, the
// lines between start and end fold inside it, the end fills the duration;
// missed calls are cards of their own. What he types and the call go to the
// bot's web channel (/api/chat/send, /api/chat/command), which hands them
// to the runtime like any message.
import { LiveFace } from "/face/live-face.js";

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const RUN = params.get("run") || "/sprites/runs/krea-v4s1/";
const REST = "neutral";            // her face between lines
const POLL_MS = 1200;
const GAP_MIN = 30;                // a time label before an entry after this many quiet minutes

// Presence: "together" (he is here) fills the page with the scene and drops
// the frame; "apart" (away) keeps the framed stage, dressed as a phone. The
// ?presence=here|away query forces a state for review and captures.
const PRESENCE_OVERRIDE = params.get("presence");
function applyPresence(mode) {
  const m = PRESENCE_OVERRIDE || mode;
  const here = m === "here" || m === "together";
  document.body.dataset.presence = here ? "here" : "away";
  face?.setVariant(here ? "plain" : "framed");   // full figure in the room, bust crop in the phone
}

// -- face ------------------------------------------------------------------

let face = null;
try {
  const manifest = await (await fetch(RUN + "manifest.json")).json();
  face = new LiveFace($("face"), manifest, { baseUrl: RUN, speakingMs: 260 });
  await face.ready;
  face.setExpression(REST, 0);
} catch (e) {
  $("face").textContent = "No sprite set at " + RUN;
}

// The expression classifier (amadeus expression/server.py), when it is up.
// Without it she keeps her resting face and still talks.
const CLASSIFIER = "http://127.0.0.1:8766";
let classifier = false;
try {
  const r = await fetch(CLASSIFIER + "/labels", { signal: AbortSignal.timeout(800) });
  classifier = r.ok;
} catch (e) { /* not running */ }

// Scene backdrops (display-only for now): a [bg:name] tag on a row switches
// the layer behind the sprite. The list lives at runs/scenes/manifest.json.
const SCENES = RUN.replace(/[^/]+\/$/, "scenes/");
let scenes = null;
async function loadScenes() {
  if (!scenes) {
    try { scenes = (await (await fetch(SCENES + "manifest.json")).json()).scenes || {}; }
    catch (e) { scenes = {}; }
  }
  return scenes;
}
// The page's default backdrop is the office scene, via the same manifest
// (the CSS falls back to the old gradient when this fails).
loadScenes().then((s) => {
  if (s.office) document.documentElement.style.setProperty("--scene-office", `url("${SCENES}${s.office}")`);
});
async function applyScene(name) {
  if (!face) return;
  const s = await loadScenes();
  const file = s[name];
  face.setScene(file ? SCENES + file : null);
}

async function faceFor(text, context) {
  if (!classifier || !face) return REST;
  try {
    const r = await (await fetch(CLASSIFIER + "/classify", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, context }) })).json();
    return r.expression || REST;
  } catch (e) { return REST; }
}

// -- rendering ---------------------------------------------------------------

const rows = [];                   // every entry seen, in order (lines + call marks)
const shown = new Set();
let cursor = 0;                    // newest message id fetched
let cursorEv = 0;                  // newest state-event id fetched (missed-call marks)
let loaded = false;                // the first history load is not animated
let hold = null;                   // her rows past this id wait (she is still "typing")
let lastAt = null;
const pending = [];                // his messages sent but not saved yet
const queue = [];                  // her live lines waiting to be spoken
let speaking = false;

function escape(s) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
// Her *actions* are italic, as on Telegram.
function render(s) { return escape(s).replace(/\*([^*\n]+)\*/g, "<em>$1</em>"); }
// One bubble per paragraph, as Telegram sends them.
const paragraphs = (t) => t.split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean);

function stick(fn) {
  const log = $("log");
  const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  fn();
  if (atEnd) log.scrollTop = log.scrollHeight;
}

function timeLabel(at) {
  if (!at) return;
  const t = new Date(at);
  if (lastAt && t - lastAt < GAP_MIN * 60000) { lastAt = t; return; }
  lastAt = t;
  const el = document.createElement("div");
  el.className = "when";
  const today = new Date().toDateString() === t.toDateString();
  el.textContent = (today ? "" : t.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }) + ", ")
    + t.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  $("log").append(el);
}

function bubble(who, html, first) {
  const el = document.createElement("div");
  el.className = "msg" + (who === "him" ? " him" : "") + (first ? " first" : "");
  el.innerHTML = html;
  $("log").append(el);
  return el;
}

function photoBubble(who, name) {
  const el = bubble(who, "", true);
  el.classList.add("photo");
  const img = document.createElement("img");
  img.src = "/api/chat/photo/" + encodeURIComponent(name);
  img.alt = "photo";
  img.loading = "lazy";
  el.append(img);
}

// His optimistic bubble (if any) gives way to the saved row.
function dropPending(row) {
  const i = pending.findIndex((p) => p.text === row.text);
  if (i >= 0) { pending[i].els.forEach((e) => e.remove()); pending.splice(i, 1); }
}

function showRow(row, live) {
  shown.add(row.src + ":" + row.id);
  if (row.kind) { callmark(row); return; }
  if (row.call) {                    // a line inside a call: folds into its card
    if (row.who === "him") dropPending(row);
    callItem(row);
    return;
  }
  if (row.who === "him") dropPending(row);
  stick(() => {
    timeLabel(row.at);
    const parts = paragraphs(row.text);
    for (const name of row.photos || []) photoBubble(row.who, name);
    const els = parts.map((p, i) => bubble(row.who, live && row.who === "her" ? "" : render(p), i === 0));
    if (live && row.who === "her") queue.push({ row, parts, els });
  });
  if (live && row.who === "her") notify(row);
  if (queue.length && !speaking) speakNext();
}

function clock(d) {
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

// "8:46–8:51 PM": one meridiem when both ends share it.
function timeRange(from, to) {
  const a = clock(new Date(from)), b = clock(to);
  return /[AP]M$/.test(b) && a.endsWith(b.slice(-2)) ? a.slice(0, -3) + "–" + b : a + "–" + b;
}

// -- call cards --------------------------------------------------------------
// A call reads as ONE entry: the card opens when the call starts, the lines
// between start and end fold inside it (collapsed — click to read), and the
// end fills in the duration. A missed call is a card of its own. Rows carry
// the call's start id as `call`; cards are keyed by it.
const cards = new Map();
const PHONE_ICON = "M6.6 10.8a15.1 15.1 0 0 0 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1A17 17 0 0 1 3 4c0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.3 0 .7-.2 1l-2.3 2.2Z";

const minutesText = (m) => (m === 1 ? "1 minute" : m + " minutes");

function makeCard(key) {
  const el = document.createElement("div");
  el.className = "callcard";
  el.dataset.call = key;
  el._count = 0;                       // folded lines so far
  el._sub = "";                        // "in progress" / "7 minutes" / …
  const head = document.createElement("button");
  head.type = "button";
  head.className = "cc-head";
  head.setAttribute("aria-disabled", "true");
  head.setAttribute("aria-expanded", "false");
  head.innerHTML = `<span class="cc-icon"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="${PHONE_ICON}"/></svg></span>`
    + `<span class="cc-main"><span class="cc-title">Voice call</span><span class="cc-sub"></span></span>`
    + `<span class="cc-time"></span><span class="cc-chev" hidden>▾</span>`;
  head.onclick = () => {
    if (!el._count) return;
    const open = !el.hasAttribute("data-open");
    el.toggleAttribute("data-open", open);
    head.setAttribute("aria-expanded", String(open));
    el.querySelector(".cc-items").hidden = !open;
    if (open) el.scrollIntoView({ block: "nearest" });  // keep the transcript in view
  };
  const items = document.createElement("div");
  items.className = "cc-items";
  items.hidden = true;
  el.append(head, items);
  return el;
}

function paintCard(el) {
  const bits = [];
  if (el._sub) bits.push(el._sub);
  if (el._count) bits.push(el._count + (el._count === 1 ? " message" : " messages"));
  el.querySelector(".cc-sub").textContent = bits.join(" · ");
  el.querySelector(".cc-chev").hidden = !el._count;
  el.querySelector(".cc-head").setAttribute("aria-disabled", String(!el._count));
}

function cardFor(key) {
  let el = cards.get(key);
  if (!el) { el = makeCard(key); cards.set(key, el); $("log").append(el); }
  return el;
}

// The call opened: the card appears at its start, "in progress" until the end.
function openCall(row) {
  stick(() => {
    timeLabel(row.at);               // a card can open a quiet-time block too
    const el = cardFor(String(row.call ?? row.id));
    el._sub = "in progress";
    if (row.at) el.querySelector(".cc-time").textContent = clock(new Date(row.at));
    paintCard(el);
  });
}

// The call ended: the card takes the duration, the row's own times in its tooltip.
function endCall(row) {
  stick(() => {
    let el = row.call != null ? cards.get(String(row.call)) : null;
    if (!el) {
      timeLabel(row.at);             // a lone end card opens its block too
      const key = row.call != null ? String(row.call) : "end:" + row.id;
      el = makeCard(key);
      if (row.call != null) cards.set(key, el);
      $("log").append(el);
    }
    el.dataset.state = "done";
    const end = row.at ? new Date(row.at) : null;
    if (end && row.minutes != null) {
      // the row's times are wall-clock text; show the call as instants in
      // the viewer's clock, like every other time on the page
      const from = new Date(end.getTime() - row.minutes * 60000);
      el.querySelector(".cc-time").textContent = clock(from);
      el.title = timeRange(from, end) + " · " + row.minutes + " min";
      el._sub = minutesText(row.minutes);
    } else if (row.start && row.end) {
      el._sub = row.start + "–" + row.end;
    } else {
      el._sub = "";
    }
    paintCard(el);
  });
}

// A missed call: a card of its own, no transcript.
function missedCard(row) {
  stick(() => {
    timeLabel(row.at);               // the missed mark can open a quiet-time block
    const el = makeCard(row.src + row.id);
    el.dataset.state = "missed";
    el.querySelector(".cc-title").textContent = "Missed call";
    el._sub = row.sub || "";
    if (row.at) el.querySelector(".cc-time").textContent = clock(new Date(row.at));
    paintCard(el);
    $("log").append(el);
  });
}

// A line inside a call: folds into the card instead of the flow.
function callItem(row) {
  stick(() => {
    const el = cardFor(String(row.call));
    const item = document.createElement("div");
    item.className = "cc-item " + (row.who === "him" ? "him" : "her");
    item.innerHTML = render(row.text);
    el.querySelector(".cc-items").append(item);
    el._count += 1;
    paintCard(el);
  });
}

function callmark(row) {
  if (row.kind === "call_started") openCall(row);
  else if (row.kind === "call_ended") endCall(row);
  else missedCard(row);
}

async function speakNext() {
  speaking = true;
  while (queue.length) {
    const { row, parts, els } = queue.shift();
    const idx = rows.indexOf(row);
    const context = rows.slice(Math.max(0, idx - 3), idx)
      .map((r) => (r.who === "him" ? "Him: " : "Lily: ") + r.text);
    // her own tag when the row has one (harness.faces), the classifier otherwise
    const expr = row.face || await faceFor(row.text, context);
    if (face) {
      face.setBlush(row.blush || null);
      if (row.scene) await applyScene(row.scene);
      face.setExpression(expr, 180);
      await new Promise((ok) => setTimeout(ok, 220));
    }
    showExpr(expr);
    for (let i = 0; i < parts.length; i++) {
      if (!face) { els[i].innerHTML = render(parts[i]); continue; }
      // a later paragraph can carry a face of its own (chat.history)
      const own = (row.faces || [])[i];
      if (i > 0 && own && own !== (row.faces[i - 1] || expr)) { face.setExpression(own, 180); showExpr(own); }
      await face.say(parts[i], { onStep: (s) => { stick(() => { els[i].innerHTML = render(parts[i].slice(0, s.end)); }); } });
      els[i].innerHTML = render(parts[i]);
    }
  }
  speaking = false;
  setTimeout(() => { if (!speaking && face) { face.setBlush(null); face.setExpression(REST, 400); showExpr(null); } }, 6000);
}

function showExpr(expr) {
  const el = $("expr");
  el.hidden = !expr || expr === REST;
  el.textContent = expr || "";
}

function note(text) {
  stick(() => {
    const el = document.createElement("div");
    el.className = "note";
    el.textContent = text;
    $("log").append(el);
  });
}

function flush() {
  for (const row of rows) {
    if (shown.has(row.src + ":" + row.id)) continue;
    if (row.who === "her" && hold !== null && row.id > hold) break;   // keep the order
    showRow(row, loaded);
  }
}

// -- polling -----------------------------------------------------------------

let noteSeq = null;
let online = null;

async function pollHistory() {
  const r = await fetch(`/api/chat/history?after=${cursor}&after_event=${cursorEv}`);
  if (!r.ok) { status(false, (await r.json()).error || "no history"); return; }
  const data = await r.json();
  for (const m of data.messages) {
    rows.push(m);
    if (m.src === "e") cursorEv = Math.max(cursorEv, m.id);
    else cursor = Math.max(cursor, m.id);
  }
  flush();
  if (!loaded) { loaded = true; $("log").scrollTop = $("log").scrollHeight; }
}

async function pollState() {
  let s = null;
  try {
    const r = await fetch("/api/chat/state");
    if (r.ok) s = await r.json();
  } catch (e) { /* offline */ }
  if (!s) { hold = null; status(false, "the bot is not running; history only"); $("typing").textContent = ""; return; }
  status(true, "bot running");
  applyPresence(s.presence);
  hold = s.typing ? s.hold_after : null;
  $("typing").textContent = s.typing ? "Lily is typing…" : "";
  const seqs = s.notes.map((n) => n.seq);
  if (noteSeq === null) noteSeq = Math.max(0, ...seqs);      // older notes are not replayed
  for (const n of s.notes) if (n.seq > noteSeq) { note(n.text); noteSeq = n.seq; }
}

function status(ok, text) {
  if (online === ok && $("status").textContent === text) return;
  online = ok;
  $("status").dataset.state = ok ? "live" : "stale";
  $("status").textContent = text;
  $("send").disabled = !ok;
  $("call").disabled = !ok;
}

async function tick() {
  try { await pollState(); await pollHistory(); } catch (e) { status(false, "connection lost"); }
  setTimeout(tick, POLL_MS);
}

// -- browser notifications ---------------------------------------------------
// Her new messages (replies and reach-outs alike) raise a notification while
// the tab is not in front. Browsers allow this on http://127.0.0.1 and
// localhost only (a secure context), and only after he opts in with a click.

const NOTIFY_KEY = "lily.notify";
const canNotify = "Notification" in window && window.isSecureContext;
function wantNotify() {
  try { return localStorage.getItem(NOTIFY_KEY) !== "off"; } catch (e) { return true; }
}
function notifyOn() { return canNotify && Notification.permission === "granted" && wantNotify(); }
function paintNotify() {
  const b = $("notify");
  if (!canNotify) { b.textContent = "Notifications unavailable"; b.disabled = true;
    b.title = "Browsers only allow notifications on http://127.0.0.1 or localhost"; return; }
  if (Notification.permission === "denied") { b.textContent = "Notifications blocked"; b.disabled = true;
    b.title = "Allow notifications for this site in the browser's site settings"; return; }
  b.textContent = notifyOn() ? "Notifications on" : "Notifications off";
  b.setAttribute("aria-pressed", String(notifyOn()));
}
$("notify").onclick = async () => {
  if (Notification.permission !== "granted") {
    await Notification.requestPermission();
    try { localStorage.removeItem(NOTIFY_KEY); } catch (e) { /* per-viewer only */ }
  } else {
    try { localStorage.setItem(NOTIFY_KEY, notifyOn() ? "off" : "on"); } catch (e) { /* per-viewer only */ }
  }
  paintNotify();
};
function notify(row) {
  if (!notifyOn() || (document.visibilityState === "visible" && document.hasFocus())) return;
  const n = new Notification("Lily", { body: row.text.replace(/\*([^*\n]+)\*/g, "$1"), icon: "/static/lily.png",
                                       tag: "lily-" + row.id });
  n.onclick = () => { window.focus(); n.close(); };
}
paintNotify();

// -- sending -----------------------------------------------------------------

async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
}

// Photos ride the next send: picked, pasted, or dropped on the conversation.
// The bot stores and shrinks them (harness.images); four per message at most.
const PHOTOS_MAX = 4;
let attached = [];          // { file, url }

function paintTray() {
  const tray = $("tray");
  tray.replaceChildren(...attached.map((a, i) => {
    const t = document.createElement("div");
    t.className = "thumb";
    const img = document.createElement("img");
    img.src = a.url; img.alt = "photo to send";
    const x = document.createElement("button");
    x.type = "button"; x.textContent = "×"; x.title = "Remove"; x.setAttribute("aria-label", "Remove photo");
    x.onclick = () => { URL.revokeObjectURL(a.url); attached.splice(i, 1); paintTray(); };
    t.append(img, x);
    return t;
  }));
  tray.hidden = attached.length === 0;
}

function attach(files) {
  const images = [...files].filter((f) => f.type.startsWith("image/"));
  const room = PHOTOS_MAX - attached.length;
  if (images.length > room) note(`At most ${PHOTOS_MAX} photos per message.`);
  for (const file of images.slice(0, Math.max(0, room))) attached.push({ file, url: URL.createObjectURL(file) });
  paintTray();
}

function dataUrl(file) {
  return new Promise((ok, fail) => {
    const r = new FileReader();
    r.onload = () => ok(r.result);
    r.onerror = () => fail(r.error);
    r.readAsDataURL(file);
  });
}

$("attach").onclick = () => $("file").click();
$("file").onchange = () => { attach($("file").files); $("file").value = ""; };
$("text").addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.some((f) => f.type.startsWith("image/"))) { e.preventDefault(); attach(files); }
});
const card = document.querySelector(".chat-card");
card.addEventListener("dragover", (e) => {
  if ([...e.dataTransfer.types].includes("Files")) { e.preventDefault(); card.classList.add("dropping"); }
});
card.addEventListener("dragleave", (e) => { if (!card.contains(e.relatedTarget)) card.classList.remove("dropping"); });
card.addEventListener("drop", (e) => { e.preventDefault(); card.classList.remove("dropping"); attach(e.dataTransfer.files); });

$("form").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("text").value.trim();
  const sending = attached;
  if (!text && !sending.length) return;
  if (text.startsWith("/")) {
    // Owner commands go through the same seam the Setup page uses; the
    // reply lands as a note on /api/chat/state and shows up below.
    const [name, ...rest] = text.slice(1).split(/\s+/).filter(Boolean);
    if (!name) { note("A command looks like /here or /mute 4."); return; }
    $("text").value = "";
    try { await post("/api/chat/command", { name, args: rest.join(" ") }); }
    catch (err) { $("text").value = text; note("/" + name + " failed: " + err.message); }
    return;
  }
  $("text").value = "";
  attached = [];
  paintTray();
  let els = [];
  stick(() => {
    els = sending.map((a) => { const el = bubble("him", "", true); el.classList.add("photo");
      const img = document.createElement("img"); img.src = a.url; img.alt = "photo"; el.append(img); return el; });
    els.push(...paragraphs(text).map((p, i) => bubble("him", render(p), i === 0 && !sending.length)));
    els.forEach((x) => (x.style.opacity = ".55"));
  });
  pending.push({ text, els });
  try {
    const photos = await Promise.all(sending.map((a) => dataUrl(a.file)));
    await post("/api/chat/send", photos.length ? { text, photos } : { text });
    sending.forEach((a) => setTimeout(() => URL.revokeObjectURL(a.url), 60_000));
  } catch (err) {
    els.forEach((x) => x.remove()); pending.splice(pending.findIndex((p) => p.els === els), 1);
    $("text").value = text; attached = sending; paintTray(); note("Not sent: " + err.message);
  }
};
$("text").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("form").requestSubmit(); } };

$("call").onclick = async () => {
  $("call").disabled = true;
  try { await post("/api/chat/command", { name: "call" }); note("Calling her… the call window opens on the desktop if she picks up."); }
  catch (err) { note("Call failed: " + err.message); }
  setTimeout(() => { $("call").disabled = !online; }, 4000);
};

window.lilyChat = { rows, get face() { return face; } };
tick();
