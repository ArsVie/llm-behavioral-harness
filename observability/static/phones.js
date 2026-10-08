// The away stage's phone body. Each design is a device built in CSS
// (phones.css) around #face: the sprite moves into the device's LCD and the
// status bar, clock and softkeys draw on top of it. "classic" is the plain
// frame in app.css. The choice is per viewer (localStorage), set on /setup;
// an open /chat follows a change made in another tab. Here (together) always
// drops the phone, since the figure fills the page. ?phone=<name> forces a
// design for review and captures, as ?presence= does.

const KEY = "lily.phone";
export const DEFAULT = "sakura";
export const PHONES = {
  sakura: { label: "Sakura flip", note: "pearl-pink clamshell", swatch: "#f6a9c3" },
  noir:   { label: "Noir flip",   note: "gloss black, mint keys", swatch: "#16171c" },
  dphone: { label: "D Phone",     note: "orange enamel, chrome hinge", swatch: "#ff4d12" },
  silver: { label: "Silver bar",  note: "candybar, blue LCD", swatch: "#c7ccd3" },
  classic:{ label: "Classic",     note: "the plain frame", swatch: "#524b5a" },
};

const OVERRIDE = new URLSearchParams(location.search).get("phone");

export function chosenPhone() {
  if (OVERRIDE in PHONES) return OVERRIDE;
  try { const v = localStorage.getItem(KEY); return v in PHONES ? v : DEFAULT; } catch (e) { return DEFAULT; }
}
export function choosePhone(name) {
  try { localStorage.setItem(KEY, name); } catch (e) { /* per-viewer only */ }
}

// -- device parts -------------------------------------------------------------

const KEYS = [["あ", "1", ""], ["か", "2", "ABC"], ["さ", "3", "DEF"], ["た", "4", "GHI"], ["な", "5", "JKL"], ["は", "6", "MNO"],
              ["ま", "7", "PQRS"], ["や", "8", "TUV"], ["ら", "9", "WXYZ"], ["゛", "*", ""], ["わ", "0", "、。"], ["", "#", ""]];
const BLUE = new Set(["2", "5", "6", "9"]);   // the D Phone's blue keys
const pad = (blue) => `<div class="pad">${KEYS.map(([ka, n, la]) =>
  `<div class="k${blue && BLUE.has(n) ? " bl" : ""}"><span class="ka">${ka}</span><span class="num">${n}</span><span class="la">${la}</span></div>`).join("")}</div>`;
const HANDSET = "M6.6 10.8a15 15 0 006.6 6.6l2.2-2.2a1 1 0 011-.25 11 11 0 003.6.6 1 1 0 011 1V20a1 1 0 01-1 1A17 17 0 013 4a1 1 0 011-1h3.5a1 1 0 011 1 11 11 0 00.6 3.6 1 1 0 01-.25 1z";
const handset = (fill, turn = 0) => `<svg viewBox="0 0 24 24" fill="${fill}" style="transform:rotate(${turn}deg)"><path d="${HANDSET}"/></svg>`;
const ENVELOPE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/></svg>`;

const STRAPS = {
  pink:  { cord: "#ff8fb8", beads: ["#ffd1e1", "#ff6fa5", "#fff", "#ffb3cf", "#ff6fa5"], charm: "heart" },
  glass: { cord: "#cfd8dc", beads: ["#7fe3dc", "#5ad1c7", "#b5f1ec", "#3fb8ad", "#7fe3dc", "#2f9e94", "#b5f1ec"], charm: "drop" },
  red:   { cord: "#d6334a", beads: ["#d6334a", "#f2c14e"], charm: "tassel" },
};
function strap(kind, style) {
  const t = STRAPS[kind];
  let g = `<path d="M48 4 C 30 10, 26 24, 30 36" stroke="${t.cord}" stroke-width="2.4" fill="none" stroke-linecap="round"/>`
        + `<path d="M48 4 C 40 14, 36 26, 30 36" stroke="${t.cord}" stroke-width="2.4" fill="none" stroke-linecap="round" opacity=".8"/>`;
  let y = 40;
  t.beads.forEach((c, i) => {
    const r = i % 2 ? 5 : 6.5;
    g += `<circle cx="30" cy="${y + r}" r="${r}" fill="${c}" stroke="rgba(0,0,0,.15)"/><circle cx="${28 - r / 4}" cy="${y + r * 0.6}" r="${r / 3}" fill="#fff" opacity=".8"/>`;
    y += r * 2 + 1;
  });
  if (t.charm === "heart") g += `<path transform="translate(18 ${y})" d="M12 22 C 2 14, 0 8, 4 4 C 7 1, 11 3, 12 6 C 13 3, 17 1, 20 4 C 24 8, 22 14, 12 22z" fill="#ff5a9a" stroke="#c2185b"/><circle cx="25" cy="${y + 7}" r="2" fill="#fff" opacity=".8"/>`;
  if (t.charm === "drop") g += `<path transform="translate(20 ${y})" d="M10 0 C 16 10, 20 14, 20 19 A10 10 0 0 1 0 19 C 0 14, 4 10, 10 0z" fill="#9ff0ea" stroke="#2f9e94" opacity=".95"/><circle cx="25" cy="${y + 16}" r="2.5" fill="#fff" opacity=".9"/>`;
  if (t.charm === "tassel") {
    for (let i = -4; i <= 4; i++) g += `<path d="M30 ${y} Q ${30 + i * 1.5} ${y + 20}, ${30 + i * 2.6} ${y + 40}" stroke="#d6334a" stroke-width="1.6" fill="none"/>`;
    g += `<rect x="25" y="${y - 3}" width="10" height="6" rx="2" fill="#f2c14e"/>`;
  }
  return `<svg class="strap" style="${style}" width="60" height="150" viewBox="0 0 60 150" aria-hidden="true">${g}</svg>`;
}

const flipFn = `<div class="fn"><div class="fk">${ENVELOPE}</div>
  <div class="nav"><i class="u"></i><i class="d"></i><i class="l"></i><i class="r"></i><b>OK</b></div><div class="fk">i</div>
  <div class="fk">MENU</div><div class="fk">CLR</div>
  <div class="fk call">${handset("currentColor")}</div><div class="fk end">${handset("currentColor")}</div></div>`;
const flip = (strapKind) => `
  <div class="top"><div class="ear"></div>
    <div class="bez"><div class="glass"><div class="ph-lcd"></div></div><div class="plate">LILY · 905i</div></div></div>
  <div class="hinge"><div class="led"></div></div>
  <div class="bot">${flipFn}${pad(false)}<div class="mic"></div></div>
  ${strap(strapKind, "left:-34px;bottom:-104px")}`;

const ROD = {
  book: `<svg viewBox="0 0 18 14"><path d="M9 3c-2-1.5-5-2-7-1v10c2-1 5-.5 7 1 2-1.5 5-2 7-1V2c-2-1-5-.5-7 1zM9 3v10"/></svg>`,
  camera: `<svg viewBox="0 0 18 14"><rect x="2" y="2" width="14" height="10" rx="1.5"/><circle cx="9" cy="7" r="2.6"/></svg>`,
  mail: `<svg viewBox="0 0 18 14"><rect x="2" y="2.5" width="14" height="9"/><path d="M2 2.5l7 5.5 7-5.5"/></svg>`,
  info: `<svg viewBox="0 0 18 14"><path d="M9 4.5v7"/><circle cx="9" cy="2.4" r=".6"/></svg>`,
};

const BODIES = {
  sakura: { cls: "ph-flip ph-sakura", keys: ["Mail", "MENU", "Web"], html: flip("pink") },
  noir:   { cls: "ph-flip ph-noir",   keys: ["Mail", "MENU", "Web"], html: flip("glass") },
  dphone: { cls: "ph-dphone", keys: ["Mail", "D-mail", "Menu"], html: `
    <div class="top"><div class="ear"></div><div class="cam"></div>
      <div class="glass"><div class="scr"><div class="ph-lcd"></div></div>
        <div class="band"><i></i><i></i><b></b><i></i><i></i></div></div></div>
    <div class="rods">
      <div class="row a"><div class="seg">${ROD.book}</div><div class="seg">${ROD.camera}</div><div class="seg cap"></div></div>
      <div class="row b"><div class="seg cap"></div><div class="seg">${ROD.mail}</div><div class="seg">${ROD.info}</div></div>
    </div>
    <div class="bot"><div class="deck">
      <div class="fx"><div>—</div><div>—</div><div>—</div><div>${handset("#e9e9e9")}</div><div>—</div><div>${handset("#e9e9e9", 135)}</div></div>
      ${pad(true)}</div><div class="mic"></div></div>` },
  silver: { cls: "ph-silver", keys: ["Mail", "◆", "MENU"], tint: true, html: `
    <div class="ant"></div><div class="ear"></div>
    <div class="bez"><div class="ph-lcd"></div><div class="plate">L I L Y</div></div>
    <div class="fn"><div class="fk">${ENVELOPE}</div><div class="nav"><b></b></div><div class="fk">MENU</div>
      <div class="fk">${handset("#1f9d55")}</div><div class="fk">${handset("#d6334a")}</div></div>
    ${pad(false)}<div class="mic"></div>
    ${strap("red", "left:-26px;bottom:-124px")}` },
};

const STATUS = `<div class="sb"><span class="ant"><i></i><i></i><i></i><i></i></span><span class="ico">3G</span>
  <span class="ico">✉</span><span class="bat"><i></i><i></i><i></i></span></div>`;

// -- mounting -----------------------------------------------------------------

const stage = document.querySelector(".stage");
const face = document.getElementById("face");
const expr = document.getElementById("expr");
let root = null, current = null;

function unmount() {
  if (!root) return;
  stage.prepend(face);
  if (expr) face.after(expr);
  root.remove();
  root = null;
  delete stage.dataset.phone;
}

function mount(name) {
  const b = BODIES[name];
  root = document.createElement("div");
  root.className = "ph " + b.cls;
  root.innerHTML = b.html;
  const lcd = root.querySelector(".ph-lcd");
  lcd.append(face);
  if (expr) lcd.append(expr);
  lcd.insertAdjacentHTML("beforeend", (b.tint ? `<div class="tint"></div>` : "") + STATUS
    + `<div class="clock"><b></b><span></span></div>`
    + `<div class="sk">${b.keys.map((k) => `<span>${k}</span>`).join("")}</div>`);
  stage.prepend(root);
  stage.dataset.phone = name;
  tick();
}

// Fit the device to the stage column and the window height (the chat card
// beside it is 100vh - 190px tall); on narrow screens only the screen half
// shows, above the conversation.
function fit() {
  if (!root) return;
  const compact = innerWidth <= 760;
  root.classList.toggle("ph-compact", compact);
  root.style.zoom = 1;
  const w = root.offsetWidth, h = root.offsetHeight;
  const availH = compact ? innerHeight * 0.48 : Math.max(420, innerHeight - 190);
  root.style.zoom = Math.min(stage.clientWidth / w, availH / h, 1.5).toFixed(3);
}

function tick() {
  if (!root) return;
  const now = new Date();
  root.querySelector(".clock b").textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  root.querySelector(".clock span").textContent = now.toLocaleDateString([], { weekday: "short", month: "numeric", day: "numeric" });
}

function apply() {
  const want = document.body.dataset.presence === "here" ? "classic" : chosenPhone();
  if (want !== current) {
    unmount();
    current = want;
    if (BODIES[want]) mount(want);
  }
  fit();
}

if (stage && face) {
  apply();
  new MutationObserver(apply).observe(document.body, { attributes: true, attributeFilter: ["data-presence"] });
  addEventListener("storage", (e) => { if (e.key === KEY) apply(); });
  addEventListener("resize", fit);
  setInterval(tick, 15000);
}
