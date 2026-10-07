// /chat: the live conversation (the same one Telegram carries), her sprite,
// and the call button. History comes from the database (/api/chat/history);
// what he types and the call go to the bot's web channel (/api/chat/send,
// /api/chat/command), which hands them to the runtime like any message.
import { LiveFace } from "/face/live-face.js";

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const RUN = params.get("run") || "/sprites/runs/krea-v4s1/";
const REST = "neutral";            // her face between lines
const POLL_MS = 1200;
const GAP_MIN = 30;                // a time label before a message after this many quiet minutes

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
async function applyScene(name) {
  if (!face) return;
  if (!scenes) {
    try { scenes = (await (await fetch(SCENES + "manifest.json")).json()).scenes || {}; }
    catch (e) { scenes = {}; }
  }
  const file = scenes[name];
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

const rows = [];                   // every user/assistant row seen, id order
const shown = new Set();
let cursor = 0;                    // newest id fetched
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

function showRow(row, live) {
  shown.add(row.id);
  if (row.who === "him") {
    const i = pending.findIndex((p) => p.text === row.text);
    if (i >= 0) { pending[i].els.forEach((e) => e.remove()); pending.splice(i, 1); }
  }
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
    if (shown.has(row.id)) continue;
    if (row.who === "her" && hold !== null && row.id > hold) break;   // keep the order
    showRow(row, loaded);
  }
}

// -- polling -----------------------------------------------------------------

let noteSeq = null;
let online = null;

async function pollHistory() {
  const r = await fetch(`/api/chat/history?after=${cursor}`);
  if (!r.ok) { status(false, (await r.json()).error || "no history"); return; }
  const data = await r.json();
  for (const m of data.messages) { rows.push(m); cursor = Math.max(cursor, m.id); }
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
