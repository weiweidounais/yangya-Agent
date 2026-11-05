import { currentUserId, sessionReady } from "./identity.js?v=__APP_VERSION__";

const PREFIX = "ja.transcript.v1:";
const MAX_ENTRIES = 160;
const SENSITIVE_LABEL = /密码|口令|验证码|短信码|动态码|\b(?:password|passwd|pwd|otp|token|api[ _-]?key|secret|cookie|session[_-]?id|access[_-]?token|bearer)\b/i;
const SENSITIVE_VALUE = new RegExp(`(?:${SENSITIVE_LABEL.source})\\s*(?:[:：=]|是|为|\\bis\\b|\\s)\\s*\\S+`, "i");
const SENSITIVE_CODE = /(?:验证码|短信码|动态码|\botp\b)\s*(?:是|为|[:：=])?\s*\d{4,8}\b/i;
const privateEntries = new WeakSet();

let cache = null;
let cacheKey = "";

function privateUserText(entry, previous) {
  if (entry.type !== "user") return false;
  const text = String(entry.data?.text || "");
  if (SENSITIVE_VALUE.test(text) || SENSITIVE_CODE.test(text)) return true;
  const prompt = previous?.data;
  return /^\s*\d{4,8}\s*$/.test(text) && SENSITIVE_LABEL.test(
    prompt?.text || prompt?.card?.question || prompt?.card?.label || ""
  );
}

function serializedTranscript() {
  // Raw text remains available to the current tab; every storage write uses
  // the placeholder, including later updates and the quota-recovery path.
  return JSON.stringify(cache.map(entry => privateEntries.has(entry)
    ? { ...entry, data: { ...entry.data, text: "[已隐藏敏感内容]", omitted: 0, redacted: true } }
    : entry));
}

function storageKey() {
  return PREFIX + currentUserId();
}

function load() {
  const key = storageKey();
  if (cache && cacheKey === key) return cache;
  cacheKey = key;
  try {
    const parsed = JSON.parse(localStorage.getItem(key) || "[]");
    cache = Array.isArray(parsed) ? parsed : [];
  } catch {
    cache = [];
  }
  return cache;
}

function persist() {
  // Never write a transcript under a session that is not confirmed yet
  // ("pending" or a uid the server no longer recognises).
  if (!sessionReady() || currentUserId() === "pending") return;
  try {
    localStorage.setItem(cacheKey, serializedTranscript());
  } catch {
    // Quota: keep every confirm card (they never expire) plus the most recent tail.
    const tail = cache.slice(-40);
    cache = cache.filter((e) => e.type === "confirm" || tail.includes(e));
    try {
      localStorage.setItem(cacheKey, serializedTranscript());
    } catch {
      /* storage unavailable: transcript lives for this tab only */
    }
  }
}

export const store = {
  entries() {
    return load().slice();
  },
  add(entry) {
    const entries = load();
    if (privateUserText(entry, entries.at(-1))) privateEntries.add(entry);
    entries.push(entry);
    if (cache.length > MAX_ENTRIES) cache.splice(0, cache.length - MAX_ENTRIES);
    persist();
    return entry;
  },
  get(id) {
    return load().find((e) => e.id === id) || null;
  },
  update(id, patch) {
    const entry = load().find((e) => e.id === id);
    if (!entry) return null;
    Object.assign(entry.data, patch);
    persist();
    return entry;
  },
  find(predicate) {
    return load().filter(predicate);
  },
  clear() {
    load();
    cache = [];
    try {
      localStorage.removeItem(cacheKey);
    } catch {
      /* ignore */
    }
  },
  reload() {
    cache = null;
    cacheKey = "";
  },
};
