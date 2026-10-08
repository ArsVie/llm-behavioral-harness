// The page backdrop behind /chat while he is away: an image from the
// backdrops folder (listed by /api/backdrops) or one he uploaded on /setup.
// The choice is per viewer (localStorage), like the phone; an uploaded image
// is shrunk and kept in this browser only. Here mode keeps its scene, which
// fills the page instead. ?backdrop=<name> forces a folder image for review.

const KEY = "lily.backdrop";            // "" (none), "custom", or a folder image url
const CUSTOM = "lily.backdrop.custom";  // the uploaded image, as a data url
const MAX_SIDE = 1920;

const get = (k) => { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } };

export async function listBackdrops() {
  try { return (await (await fetch("/api/backdrops")).json()).backdrops || []; } catch (e) { return []; }
}
export const chosenBackdrop = () => get(KEY);
export const customBackdrop = () => get(CUSTOM);
export function chooseBackdrop(value) {
  try { localStorage.setItem(KEY, value); } catch (e) { /* per-viewer only */ }
}

// Shrink an uploaded picture to fit MAX_SIDE and keep it as a JPEG data url;
// throws when the browser's storage is full.
export async function saveCustomBackdrop(file) {
  const bitmap = await createImageBitmap(file);
  const k = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * k); canvas.height = Math.round(bitmap.height * k);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  localStorage.setItem(CUSTOM, canvas.toDataURL("image/jpeg", 0.85));
  chooseBackdrop("custom");
}

function paint(url) {
  if (url) {
    document.documentElement.style.setProperty("--backdrop", `url("${url}")`);
    document.body.dataset.backdrop = "";
  } else {
    document.documentElement.style.removeProperty("--backdrop");
    delete document.body.dataset.backdrop;
  }
}

async function apply() {
  const forced = new URLSearchParams(location.search).get("backdrop");
  if (forced) { paint((await listBackdrops()).find((b) => b.name === forced)?.url); return; }
  const choice = chosenBackdrop();
  paint(choice === "custom" ? customBackdrop() : choice);
}

if (document.querySelector(".chat-grid")) {
  apply();
  addEventListener("storage", (e) => { if (e.key === KEY || e.key === CUSTOM) apply(); });
}
