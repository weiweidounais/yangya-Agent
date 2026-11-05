/**
 * 组间休息计时卡：倒计时、跳过、+30 秒、结束时提示音/震动。
 * 卡片只消费服务端给的 {seconds, exercise, sets, tip}，不自己发明训练建议。
 */
import { $, h, announce, toast } from "./core.js?v=__APP_VERSION__";

let audio = null;

function context() {
  if (audio) return audio;
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return null;
  try {
    audio = new Ctx();
  } catch {
    audio = null;
  }
  return audio;
}

function beep() {
  const ctx = context();
  if (!ctx) return;
  if (ctx.state === "suspended") ctx.resume().catch(() => {});
  const now = ctx.currentTime;
  [0, 0.22, 0.44].forEach((offset, index) => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = index === 2 ? 1040 : 760;
    gain.gain.setValueAtTime(0.0001, now + offset);
    gain.gain.exponentialRampToValueAtTime(0.18, now + offset + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + 0.16);
    osc.connect(gain).connect(ctx.destination);
    osc.start(now + offset);
    osc.stop(now + offset + 0.18);
  });
}

function vibrate() {
  try {
    navigator.vibrate?.([180, 90, 180]);
  } catch {
    /* unsupported */
  }
}

function clock(seconds) {
  const value = Math.max(0, Math.round(seconds));
  const m = Math.floor(value / 60);
  const s = value % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function showRestCard(card, { container } = {}) {
  if (!card || !card.seconds) return null;
  const log = container || $("log");
  if (!log) return null;
  let remaining = Number(card.seconds);
  let running = false;
  let done = false;
  let ticker = 0;

  const time = h("div", { class: "rest-time", text: clock(remaining), "aria-live": "off" });
  const status = h("span", { class: "rest-status", text: "准备开始" });
  const bar = h("div", { class: "rest-bar-fill" });
  const progress = h("div", { class: "rest-bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(card.seconds) }, [bar]);
  const toggle = h("button", { type: "button", class: "btn btn-primary btn-sm", text: "开始休息" });
  const plus = h("button", { type: "button", class: "btn btn-ghost btn-sm", text: "+30 秒" });
  const skip = h("button", { type: "button", class: "btn btn-ghost btn-sm", text: "跳过" });
  const next = h("button", { type: "button", class: "btn btn-ghost btn-sm", text: "下一组", hidden: "" });

  const title = h("h4", { class: "rest-title", text: [card.exercise, card.label].filter(Boolean).join(" · ") || "组间休息" });
  const tip = h("p", { class: "rest-tip", text: card.tip || "" });

  const cardEl = h("section", { class: "rest-card", "aria-label": "组间休息计时" }, [
    h("div", { class: "rest-head" }, [title, status]),
    time,
    progress,
    h("div", { class: "rest-actions" }, [toggle, plus, skip, next]),
    tip,
  ]);

  function paint() {
    time.textContent = clock(remaining);
    const ratio = Math.max(0, Math.min(1, 1 - remaining / Math.max(1, Number(card.seconds))));
    bar.style.width = `${Math.round(ratio * 100)}%`;
    progress.setAttribute("aria-valuenow", String(Math.round(Number(card.seconds) - remaining)));
    cardEl.classList.toggle("is-done", done);
  }

  function stopTicker() {
    window.clearInterval(ticker);
    ticker = 0;
  }

  function finish() {
    if (done) return;
    done = true;
    running = false;
    remaining = 0;
    stopTicker();
    paint();
    status.textContent = "休息结束";
    toggle.hidden = true;
    plus.hidden = true;
    skip.hidden = true;
    next.hidden = false;
    beep();
    vibrate();
    announce("休息结束，可以开始下一组了");
    toast("休息结束，下一组");
    cardEl.dispatchEvent(new CustomEvent("rest-finished", { bubbles: true }));
  }

  function start() {
    if (done || running) return;
    running = true;
    status.textContent = "休息中";
    toggle.textContent = "暂停";
    const startedAt = Date.now();
    const base = remaining;
    ticker = window.setInterval(() => {
      remaining = Math.max(0, base - (Date.now() - startedAt) / 1000);
      paint();
      if (remaining <= 0.05) finish();
    }, 200);
  }

  function pause() {
    if (!running) return;
    running = false;
    stopTicker();
    status.textContent = "已暂停";
    toggle.textContent = "继续";
  }

  toggle.addEventListener("click", () => (running ? pause() : start()));
  plus.addEventListener("click", () => {
    remaining = Math.min(Number(card.seconds) + 120, remaining + 30);
    if (done) {
      done = false;
      next.hidden = true;
      toggle.hidden = false;
      plus.hidden = false;
      skip.hidden = false;
      status.textContent = "加时 30 秒";
    }
    paint();
  });
  skip.addEventListener("click", () => {
    stopTicker();
    running = false;
    done = true;
    status.textContent = "已跳过";
    toggle.hidden = true;
    plus.hidden = true;
    skip.hidden = true;
    next.hidden = false;
    paint();
  });
  next.addEventListener("click", () => {
    cardEl.classList.add("is-collapsed");
    status.textContent = "下一组加油";
    announce("下一组加油");
  });

  paint();
  log.append(cardEl);
  const scroller = $("logScroll");
  if (scroller) scroller.scrollTop = scroller.scrollHeight;
  // Auto-start: the user just asked for a rest timer.
  window.setTimeout(start, 300);
  return cardEl;
}
