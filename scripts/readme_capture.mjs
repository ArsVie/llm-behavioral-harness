// Headless capture of the run viewer's /chat page for the README hero.
// Driven by scripts/readme_hero.py, which passes a JSON config on stdin:
//   url, ids, unprompted, labels, panel, clock, footer, out, gif_dir
// The page is the real /chat page served by `python -m observability`.
// The black "noir" phone on a dark page. Three things are stubbed so the
// capture is reproducible and touches no running bot: /api/chat/state (no
// bot), the expression classifier (her face stays at rest) and the browser
// clock. The phone's lock-screen clock is hidden: the excerpt spans a day,
// so no single time would be right.
// History is filtered to the excerpt's message ids; a time label is added
// before the lines the page would label, in run days because a simulated run
// has no calendar date.
import { createRequire } from "node:module";
import { mkdirSync, readFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_CORE || "playwright-core");
const cfg = JSON.parse(readFileSync(0, "utf8"));

const CSS = `
  body { background: radial-gradient(900px 520px at 30% 0%, #1d2427, transparent 70%), #101315 !important; }
  .page-narrow { max-width: none; padding-top: 14px; }
  .chat-grid { grid-template-columns: 250px minmax(0, 1fr) 360px !important; gap: 22px; }
  #status, .clock { visibility: hidden !important; }
  .composer, .tray { display: none !important; }
  body:not(.gif) #typing { display: none !important; }
  #typing { font-size: 14px; padding: 2px 18px 10px; color: #8fc9c3; }
  body[data-phone] .chat-card { height: 652px !important; }
  #log { padding: 8px 18px 14px; }
  #log .msg { font-size: 17px; line-height: 1.5; }
  #log .when.hero { font-size: 13px; margin: 8px 0 2px; }
  #log .msg.unprompted { box-shadow: 0 0 0 2px #55e6dc, 0 0 14px #55e6dc55; }
  #log .tag { align-self: flex-start; margin: 4px 0 -2px; padding: 3px 12px;
    border-radius: 999px; background: #55e6dc; color: #062421; font-size: 13px; font-weight: 600; }
  .hero-state { padding: 16px 18px 14px; font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    color: #dbe5e3; font-size: 15px; line-height: 1.45; align-self: start;
    background: #181d20 !important; border: 1px solid #2c3539 !important; box-shadow: 0 10px 30px #0008 !important; }
  .hs-kicker { font-size: 18px; font-weight: 700; margin-bottom: 8px; color: #f1f6f5; }
  .hs-row { display: flex; align-items: center; gap: 10px; margin: 6px 0; }
  .hs-k { color: #8fb3ae; font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; min-width: 92px; }
  .hs-block { margin-top: 12px; }
  .hs-v { font-weight: 600; color: #f1f6f5; }
  .hs-mood { display: inline-flex; gap: 3px; }
  .hs-mood i { width: 11px; height: 16px; border-radius: 3px; background: #2b3538; }
  .hs-mood i.on { background: #55e6dc; }
  .hs-agenda { list-style: none; padding: 0; margin: 6px 0 0; }
  .hs-agenda li { display: grid; grid-template-columns: 80px 1fr; gap: 6px; margin: 3px 0; font-size: 14px; }
  .hs-agenda li.d { display: block; margin: 8px 0 2px; font-weight: 700; font-size: 13px; color: #f1f6f5; }
  .hs-agenda li.src > span { color: #7fe3dc; font-weight: 600; }
  .hs-agenda b { font-weight: 600; color: #a9bdba; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .st { font-size: 12px; padding: 1px 8px; border-radius: 999px; margin-left: 4px; white-space: nowrap; font-weight: 500; }
  .st.done { background: #163a33; color: #7fe3c0; }
  .st.skipped { background: #2a2d30; color: #a3a7ab; }
  .st.moved { background: #3a3218; color: #e8c872; }
  .st.later { background: #1b2c3d; color: #8fbfec; }
  .why { margin: 6px 0 0; }
  .why .hook { background: #11403b; color: #7fe3dc; padding: 1px 7px; border-radius: 6px; font-weight: 600; }
  .why-sub { margin: 6px 0 0; color: #a9bdba; }
  .hs-foot { margin: 14px 0 0; padding-top: 10px; border-top: 1px dashed #36444a; color: #94a6a3; font-size: 13px; }
  .hero-foot { position: fixed; left: 0; right: 0; bottom: 8px; text-align: center; font: 12px system-ui, sans-serif; color: #8b9a98; }
`;

const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
const page = await browser.newPage({ viewport: { width: 1180, height: 712 }, deviceScaleFactor: 2 });
page.on("pageerror", (e) => console.error("pageerror:", e.message));
const [hh, mm] = cfg.clock;
await page.clock.setFixedTime(new Date(2026, 0, 1, hh, mm));
await page.addInitScript((css) => {
  addEventListener("DOMContentLoaded", () => {
    const s = document.createElement("style"); s.textContent = css; document.head.append(s);
  });
}, CSS);
await page.route("http://127.0.0.1:8766/**", (r) => r.abort());

// What the page may see: in GIF mode the excerpt is released row by row.
let released = cfg.gif_dir ? new Set(cfg.ids.filter((id) => id < cfg.unprompted)) : new Set(cfg.ids);
let typing = false;
await page.route("**/api/chat/state", (r) => cfg.gif_dir
  ? r.fulfill({ json: { presence: "away", typing, hold_after: null, notes: [] } })
  : r.fulfill({ status: 503, json: {} }));
let all = null;
await page.route("**/api/chat/history**", async (r) => {
  if (!all) all = (await (await r.fetch()).json()).messages.filter((m) => m.src === "m" && cfg.ids.includes(m.id));
  const after = Number(new URL(r.request().url()).searchParams.get("after") || 0);
  const messages = all.filter((m) => released.has(m.id) && m.id > after);
  r.fulfill({ json: { messages, latest: Math.max(0, ...messages.map((m) => m.id)) } });
});

await page.goto(cfg.url + "/chat?phone=noir&presence=away", { waitUntil: "networkidle" });
await page.waitForTimeout(1500);

// The state card, the footer, and labels on the lines already shown.
await page.evaluate(({ panel, footer, gif }) => {
  if (gif) { document.body.classList.add("gif"); return; }
  document.querySelector(".chat-grid").insertAdjacentHTML("beforeend", panel);
  document.body.insertAdjacentHTML("beforeend", `<div class="hero-foot">${footer}</div>`);
  dispatchEvent(new Event("resize"));
}, { panel: cfg.panel, footer: cfg.footer, gif: !!cfg.gif_dir });

const decorate = () => page.evaluate(({ ids, labels, unprompted }) => {
  const msgs = [...document.querySelectorAll("#log > .msg.first")];
  msgs.forEach((el, i) => {
    const id = ids[i];
    if (el.dataset.decorated) return;
    el.dataset.decorated = "1";
    if (labels[id]) {
      const d = document.createElement("div"); d.className = "when hero"; d.textContent = labels[id]; el.before(d);
    }
    if (id === unprompted) {
      el.classList.add("unprompted");
      const t = document.createElement("span"); t.className = "tag"; t.textContent = "she writes first"; el.before(t);
    }
  });
  const log = document.getElementById("log"); log.scrollTop = log.scrollHeight;
}, { ids: cfg.ids, labels: cfg.labels, unprompted: cfg.unprompted });

await decorate();
await page.waitForTimeout(500);

if (!cfg.gif_dir) {
  await page.evaluate(() => {
    document.getElementById("log").scrollTop = 0;
    const f = window.lilyChat && window.lilyChat.face;   // a still: no mid-blink frame
    if (f) { clearTimeout(f._blinkTimer); f.blinking = false; f._render(); }
  });
  const fit = await page.evaluate(() => { const l = document.getElementById("log"); return [l.scrollHeight, l.clientHeight]; });
  if (fit[0] > fit[1] + 2) console.error(`warning: excerpt overflows the chat card (${fit[0]} > ${fit[1]})`);
  await page.screenshot({ path: cfg.out });
} else {
  // GIF: frames every 100 ms while her lines type out, with typing pauses.
  mkdirSync(cfg.gif_dir, { recursive: true });
  let n = 0;
  const shoot = async (ms) => {
    for (let t = 0; t < ms; t += 100) {
      await decorate();
      await page.screenshot({ path: `${cfg.gif_dir}/f${String(n++).padStart(4, "0")}.png`,
                              clip: { x: 0, y: 0, width: 800, height: 712 }, scale: "css" });
    }
  };
  await shoot(1200);
  for (const id of cfg.ids.filter((i) => !released.has(i))) {
    const her = all.find((m) => m.id === id).who === "her";
    if (her) { typing = true; await shoot(1800); typing = false; }
    released = new Set([...released, id]);
    await shoot(1600);
    // Keep filming until her line has finished typing out, then hold it.
    const text = () => page.evaluate(() => document.getElementById("log").textContent.length);
    for (let last = -1, n = 0; her && n < 40; n++) {
      const now = await text();
      if (now === last) break;
      last = now;
      await shoot(600);
    }
    if (her) await shoot(2400);
  }
  await shoot(2500);
}
await browser.close();
