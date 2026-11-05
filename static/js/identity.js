// Every request reads the user id through this function; keep identity rules here only.
// S1/#5: the server picks the user (HttpOnly session cookie from POST /api/session).
// The id below is only a cached label for local storage keys and display — the
// server ignores any user_id that does not match the session.
//
// Never swap identities silently: if the cookie expired and the server would hand
// out a different uid, we stop, show the expired card, and switch only when the
// user clicks 「重新开始」.
const CACHE_KEY = "ja.session_uid";
let sessionPromise = null;
// "pending" | "ready" | "expired" | "unavailable"
let state = "pending";
let offeredUid = "";

function cached() {
  try {
    return window.localStorage.getItem(CACHE_KEY) || "";
  } catch {
    return "";
  }
}

export function maskId(uid) {
  const s = String(uid || "");
  if (!s || s === "pending") return "";
  if (s.length <= 8) return s.slice(0, 2) + "…";
  return `${s.slice(0, 6)}…${s.slice(-4)}`;
}

function show(uid) {
  const el = document.getElementById("userId");
  if (el) {
    el.value = maskId(uid);
    el.readOnly = true;
  }
}

export function currentUserId() {
  return cached() || "pending";
}

export function sessionState() {
  return state;
}

/** True only once the server session is confirmed for the cached uid. */
export function sessionReady() {
  return state === "ready";
}

export class SessionBlocked extends Error {
  constructor(kind) {
    super(`session_${kind}`);
    this.kind = "session";
    this.status = kind === "unavailable" ? 503 : 401;
    this.code = kind;
  }
}

const COPY = {
  expired: {
    title: "登录已过期，之前的记录在原账号下",
    body: "为了不把你的记录混到别的账号里，我先停在这里。点「重新开始」会用一个新的会话从头开始；原来的记录不会被删掉，也不会被新会话看到。",
    button: "重新开始",
  },
  unavailable: {
    title: "暂时无法确认你的身份",
    body: "服务端的登录功能暂时没开启，现在没法确认是你，所以先不继续。你的记录都还在，稍后再试就好。（如果你是部署者：请在 .env 里配置 AUTH_SECRET 后重启服务。）",
    button: "稍后再试",
  },
};

function renderBlockedCard(kind) {
  if (typeof document === "undefined") return;
  let card = document.getElementById("sessionBlocked");
  if (card) card.remove();
  const copy = COPY[kind] || COPY.expired;
  card = document.createElement("div");
  card.id = "sessionBlocked";
  card.className = "session-blocked";
  card.setAttribute("role", "alert");
  const title = document.createElement("p");
  title.className = "session-blocked-title";
  title.textContent = copy.title;
  const body = document.createElement("p");
  body.className = "session-blocked-body";
  body.textContent = copy.body;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn btn-primary session-blocked-btn";
  btn.textContent = copy.button;
  btn.addEventListener("click", kind === "unavailable" ? retry : restart);
  card.append(title, body, btn);
  (document.querySelector("main") || document.body).prepend(card);
  btn.focus();
}

function block(kind) {
  state = kind;
  renderBlockedCard(kind);
  return new SessionBlocked(kind);
}

function clearCard() {
  const card = typeof document !== "undefined" && document.getElementById("sessionBlocked");
  if (card) card.remove();
}

/** 503 「稍后再试」: retry the same session in place; never navigates or switches uid. */
export function retry() {
  if (state !== "unavailable") return Promise.resolve(cached());
  state = "pending";
  sessionPromise = null;
  clearCard();
  return ensureSession().catch(() => "");
}

/** User-confirmed switch: adopt the server's new uid (if any) and reload at /. */
export function restart() {
  try {
    if (offeredUid) window.localStorage.setItem(CACHE_KEY, offeredUid);
    else window.localStorage.removeItem(CACHE_KEY);
  } catch {
    /* private mode */
  }
  window.location.assign("/");
}

export function ensureSession(force = false) {
  if (state === "expired" || state === "unavailable") return Promise.reject(new SessionBlocked(state));
  if (sessionPromise && !force) return sessionPromise;
  state = "pending";
  sessionPromise = fetch("/api/session", { method: "POST", credentials: "same-origin" })
    .then((r) => {
      if (r.status === 503) throw block("unavailable");
      if (r.status === 401) throw block("expired");
      return r.ok ? r.json() : Promise.reject(new Error(`session ${r.status}`));
    })
    .then((j) => {
      const uid = (j && j.data && j.data.user_id) || "";
      const prev = cached();
      if (uid && prev && uid !== prev) {
        // Cookie expired and the server minted someone new. Tell the user first.
        offeredUid = uid;
        throw block("expired");
      }
      if (uid) {
        try {
          window.localStorage.setItem(CACHE_KEY, uid);
        } catch {
          /* private mode */
        }
        show(uid);
        state = "ready";
      }
      return uid;
    })
    .catch((err) => {
      sessionPromise = null;
      throw err;
    });
  return sessionPromise;
}

/** A protected request came back 401: the session is gone. Never re-mint silently. */
export function sessionExpired() {
  sessionPromise = null;
  return block("expired");
}

show(currentUserId());
