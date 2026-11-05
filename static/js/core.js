export const VERSION = (typeof document !== "undefined" && document.querySelector('meta[name="app-version"]')?.content) || "__APP_VERSION__";
export const DEBUG = new URLSearchParams(location.search).get("debug") === "1";
export const MAX_FILE_BYTES = 5 * 1024 * 1024;

export const $ = (id) => document.getElementById(id);

let seq = 0;
export function nextId(prefix = "e") {
  seq += 1;
  return `${prefix}${Date.now().toString(36)}${seq.toString(36)}`;
}

/** Tiny element builder. props: class, text, dataset, on* handlers, value, plain attributes. */
export function h(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, val] of Object.entries(props || {})) {
    if (val == null || val === false) continue;
    if (key === "class") node.className = val;
    else if (key === "text") node.textContent = val;
    else if (key === "dataset") Object.assign(node.dataset, val);
    else if (key === "value") node.value = val;
    else if (key.startsWith("on") && typeof val === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), val);
    } else node.setAttribute(key, val === true ? "" : String(val));
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of [].concat(children)) {
    if (child == null || child === false || child === "") continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

const ICONS = {
  copy: '<rect x="9" y="9" width="12" height="12" rx="2.5"/><path d="M5 15V5a2 2 0 012-2h8"/>',
  check: '<path d="M20 6L9 17l-5-5"/>',
  file: '<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6"/>',
  x: '<path d="M18 6L6 18"/><path d="M6 6l12 12"/>',
  refresh: '<path d="M21 12a9 9 0 11-3-6.7"/><path d="M21 4v5h-5"/>',
  external: '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5"/>',
  alert: '<path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/>',
  down: '<path d="M6 9l6 6 6-6"/>',
  arrowDown: '<path d="M12 5v14"/><path d="M19 12l-7 7-7-7"/>',
  doc: '<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/>',
  clip: '<path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l8.57-8.57A4 4 0 1118 8.84l-8.59 8.57a2 2 0 01-2.83-2.83l8.49-8.48"/>',
  sun: '<path d="M12 3v2"/><path d="M5.6 5.6l1.4 1.4"/><path d="M3 12h2"/><path d="M19 12h2"/><path d="M17 7l1.4-1.4"/><circle cx="12" cy="13" r="4"/><path d="M5 20h14"/>',
  list: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M8 9h8"/><path d="M8 13h5"/><path d="M8 17h3"/>',
  bell: '<path d="M18 8a6 6 0 10-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 01-3.4 0"/>',
  mic: '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0014 0"/><path d="M12 17v4"/>',
  lock: '<rect x="4" y="11" width="16" height="10" rx="2.5"/><path d="M8 11V7a4 4 0 018 0v4"/>',
  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  eyeOff: '<path d="M10.6 5.1A10.6 10.6 0 0112 5c6.4 0 10 7 10 7a17.6 17.6 0 01-3.2 4.2"/><path d="M6.6 6.6C3.9 8.4 2 12 2 12s3.6 7 10 7a9.7 9.7 0 005.4-1.6"/><path d="M9.9 9.9a3 3 0 004.2 4.2"/><path d="M3 3l18 18"/>',
  shield: '<path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
};

export function icon(name, cls = "") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  if (cls) svg.setAttribute("class", cls);
  svg.innerHTML = ICONS[name] || "";
  return svg;
}

export function fmtSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** Backend timestamps are naive ISO strings in UTC. */
export function parseTime(value) {
  if (!value) return null;
  if (value instanceof Date) return value;
  if (typeof value === "number") return new Date(value);
  const s = String(value);
  const hasZone = /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(s);
  const d = new Date(hasZone ? s : `${s}Z`);
  return Number.isNaN(d.getTime()) ? null : d;
}

const DEFAULT_TZ = "Asia/Shanghai";
let viewTz = DEFAULT_TZ;
const partFormatters = new Map();

/** Server-provided display zone; every timestamp in the chat renders in it, not the browser's. */
export function setViewTz(tz) {
  if (!tz || tz === viewTz) return;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: tz });
    viewTz = tz;
  } catch {
    /* unknown zone: keep the current one */
  }
}

export function viewTimeZone() {
  return viewTz;
}

export function tzLabel() {
  return viewTz === DEFAULT_TZ ? "北京时间" : viewTz;
}

function formatterFor(tz) {
  if (!partFormatters.has(tz)) {
    partFormatters.set(
      tz,
      new Intl.DateTimeFormat("en-US", {
        timeZone: tz,
        year: "numeric",
        month: "numeric",
        day: "numeric",
        hour: "numeric",
        minute: "numeric",
        weekday: "short",
        hourCycle: "h23",
      })
    );
  }
  return partFormatters.get(tz);
}

const WEEKDAY_INDEX = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };

/** Wall-clock parts of ``d`` in the view zone: { y, m, d, hh, mm, wd } (m 1-based, wd 0=Sun). */
export function zonedParts(d) {
  const out = {};
  for (const p of formatterFor(viewTz).formatToParts(d)) out[p.type] = p.value;
  return {
    y: Number(out.year),
    m: Number(out.month),
    d: Number(out.day),
    hh: Number(out.hour) % 24,
    mm: Number(out.minute),
    wd: WEEKDAY_INDEX[out.weekday] ?? 0,
  };
}

/** Whole calendar days from today to ``d`` in the view zone (0 = today). */
export function dayOffset(d) {
  const a = zonedParts(d);
  const b = zonedParts(new Date());
  return Math.round((Date.UTC(a.y, a.m - 1, a.d) - Date.UTC(b.y, b.m - 1, b.d)) / 86400000);
}

/** "YYYY-MM-DDTHH:MM" wall time in the view zone → Date. */
export function fromZonedInput(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(value || "");
  if (!m) return null;
  const wall = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
  let t = wall;
  for (let i = 0; i < 2; i += 1) {
    const p = zonedParts(new Date(t));
    t -= Date.UTC(p.y, p.m - 1, p.d, p.hh, p.mm) - wall;
  }
  return new Date(t);
}

function two(n) {
  return String(n).padStart(2, "0");
}

export function toZonedInput(d) {
  const p = zonedParts(d);
  return `${p.y}-${two(p.m)}-${two(p.d)}T${two(p.hh)}:${two(p.mm)}`;
}

export function clock(value) {
  const d = parseTime(value);
  if (!d) return "";
  const p = zonedParts(d);
  return `${two(p.hh)}:${two(p.mm)}`;
}

export function dayTime(value) {
  const d = parseTime(value);
  if (!d) return "";
  const day = dayOffset(d);
  if (day === 0) return `今天 ${clock(d)}`;
  if (day === -1) return `昨天 ${clock(d)}`;
  const p = zonedParts(d);
  return `${p.m}月${p.d}日 ${clock(d)}`;
}

export function relTime(value) {
  const d = parseTime(value);
  if (!d) return "";
  const minutes = Math.floor((Date.now() - d.getTime()) / 60000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} 天前`;
  const p = zonedParts(d);
  return `${p.m}月${p.d}日`;
}

export function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function announce(text) {
  const box = $("srAnnounce");
  if (!box || !text) return;
  box.textContent = "";
  window.setTimeout(() => {
    box.textContent = String(text).slice(0, 220);
  }, 40);
}

export function toast(text, { error = false } = {}) {
  const wrap = $("toastWrap");
  if (!wrap) return;
  const node = h("div", { class: `toast${error ? " is-error" : ""}`, text });
  wrap.appendChild(node);
  window.setTimeout(() => node.remove(), 3200);
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = h("textarea", { class: "sr-only", value: text, "aria-hidden": "true" });
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch {
      ok = false;
    }
    area.remove();
    return ok;
  }
}

export function debugBlock(label, payload) {
  if (!DEBUG) return null;
  let body = "";
  try {
    body = typeof payload === "string" ? payload : JSON.stringify(payload, null, 2);
  } catch {
    body = String(payload);
  }
  return h("details", { class: "debug-raw debug-only" }, [
    h("summary", { text: label }),
    h("pre", { text: body.slice(0, 12000) }),
  ]);
}
