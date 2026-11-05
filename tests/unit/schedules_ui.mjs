// Real cards, flow context, and transcript store; synthetic DOM and API only.
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.events = {}; }
  get value() {
    if (this.tag === "select") return this.children.find(child => child.selected)?.value || "";
    return this._value ?? "";
  }
  set value(value) {
    if (this.tag === "select") {
      for (const child of this.children) child.selected = child.value === String(value);
    } else this._value = String(value);
  }
  append(...children) { this.children.push(...children.filter(x => x != null)); }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  setAttribute(key, value) { this[key] = value; }
  addEventListener(event, fn) { this.events[event] = fn; }
  get textContent() { return (this.text || "") + this.children.map(x => x.textContent || "").join(""); }
  set textContent(text) { this.text = text; this.children = []; }
  querySelectorAll(tag) {
    return this.children.flatMap(child => [
      ...(child.tag === tag ? [child] : []), ...child.querySelectorAll(tag),
    ]);
  }
}
const h = (tag, props = {}, children = []) => {
  const el = new Element(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "text") el.textContent = value;
    else if (key === "onClick") el.events.click = value;
    else el[key] = value;
  }
  el.append(...children);
  return el;
};
const storage = new Map(), calls = [];
let failure = false, runs = [];
const schedule = {
  id: 1, name: "合成提醒", job_kind: "one_shot_reminder", tz: "Asia/Shanghai",
  trigger: { type: "once", run_at: "2026-12-01T15:00" },
  next_run_at: "2026-12-01T07:00:00+00:00", prompt_text: "合成内容",
  delivery: "in_app", channel_preference: "auto", status: "active", enabled: true,
  run_count: 0, success_count: 0, fail_count: 0,
};
const body = new Element("div");
const context = vm.createContext({
  h, $: () => body, toast() {}, currentUserId: () => "synthetic-schedule-user", sessionReady: () => true,
  localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
  friendlyError: err => ({ text: err.message }),
  api: {
    decideSchedule: async (id, confirmed) => {
      calls.push([id, confirmed]);
      if (failure) throw new Error("合成请求失败");
      return { data: { status: confirmed ? "confirmed" : "rejected" } };
    },
    schedule: async () => ({ data: schedule }),
    scheduleRuns: async () => ({ data: runs }),
    updateSchedule: async (id, patch) => { calls.push([id, patch]); Object.assign(schedule, patch); },
    schedules: async () => ({ data: { schedules: [schedule], stats: schedule } }),
  },
});
for (const file of ["store", "schedules", "flows"]) {
  const source = fs.readFileSync(`static/js/${file}.js`, "utf8")
    .replace(/^import\s*\{([\s\S]*?)\}\s*from\s*[^;]+;\n/gm, (_, names) => {
      for (const name of names.split(",").map(s => s.trim()).filter(Boolean)) {
        if (!(name in context)) context[name] = () => {};
      }
      return "";
    }).replace(/^export /gm, "");
  vm.runInContext(source, context, { filename: `static/js/${file}.js` });
}
const store = vm.runInContext("store", context);
const findButton = (root, label) => root.querySelectorAll("button").find(b => b.textContent === label);

// Every mutation card uses the real context adapter, persists its decision,
// and remains decided after a transcript reload without issuing another API call.
for (const [operation, label] of [["create", "创建"], ["update", "修改"], ["delete", "删除"]]) {
  for (const confirmed of [true, false]) {
    const id = `${operation}-${confirmed}`;
    const entry = { id, type: "schedule_confirm", data: { proposal: { id, operation, preview: schedule } } };
    store.add(entry);
    const root = context.buildCard(entry, false);
    assert.ok(root.textContent.includes("一次性 · 2026-12-01 15:00 · Asia/Shanghai"));
    await findButton(root, confirmed ? `确认${label}` : "取消").events.click();
    assert.deepEqual(calls.at(-1), [id, confirmed]);
    assert.equal(root.querySelectorAll("button").length, 0);
    assert.ok(root.textContent.includes(confirmed ? "已确认并保存" : "已取消"));
    store.reload();
    const restored = store.get(id);
    assert.equal(restored.data.decision, confirmed ? "confirmed" : "rejected");
    const callCount = calls.length;
    const reloaded = context.buildCard(restored, true);
    assert.equal(reloaded.querySelectorAll("button").length, 0);
    assert.ok(reloaded.textContent.includes(confirmed ? "已确认并保存" : "已取消"));
    assert.equal(calls.length, callCount);
  }
}

// Failed requests retain an actionable card and never persist a decision.
failure = true;
const pending = { id: "failed", type: "schedule_confirm", data: { proposal: {
  id: "failed", operation: "create", preview: schedule,
} } };
store.add(pending);
let root = context.buildCard(pending, false);
await findButton(root, "确认创建").events.click();
assert.ok(root.textContent.includes("合成请求失败"));
assert.ok(root.querySelectorAll("button").every(b => !b.disabled));
store.reload();
assert.equal(store.get("failed").data.decision, undefined);

// Detail and confirmation agree for both wall-time and explicit-offset input.
vm.runInContext("body = $('schedulesBody')", context);
for (const run_at of ["2026-12-01T15:00", "2026-12-01T15:00:00+08:00", "2026-12-01T07:00:00Z"]) {
  schedule.trigger.run_at = run_at;
  await context.loadDetail(1);
  assert.ok(body.textContent.includes("一次性 · 2026-12-01 15:00 · Asia/Shanghai"));
  assert.equal(body.querySelectorAll("input").find(el => el.type === "datetime-local").value, "2026-12-01T15:00");
  root = context.buildCard(pending, false);
  assert.ok(root.textContent.includes("一次性 · 2026-12-01 15:00 · Asia/Shanghai"));
}

// C custom prompt edits, idle badge and truthful email delivery history.
schedule.job_kind = "custom_nl";
schedule.trigger = { type: "interval", every_seconds: 3600 };
schedule.status = "paused_idle";
schedule.enabled = false;
schedule.paused_by_idle = true;
schedule.delivery = "in_app,email";
schedule.prompt_text = "合成只读指令";
runs = [{ started_at: "2030-12-01T07:00:00Z", ok: true, item_id: 1,
  detail: { skipped: "email_unconfigured", email: { ok: false, skipped: true, reason: "email_unconfigured" } } }];
await context.loadList();
assert.ok(body.textContent.includes("自定义指令"));
assert.ok(body.textContent.includes("14 天未活跃，已暂停"));
await context.loadDetail(1);
assert.ok(body.textContent.includes("自定义指令（仅只读查询）"));
assert.ok(body.textContent.includes("14 天未活跃，已暂停"));
assert.ok(body.textContent.includes("邮件未配置，已跳过"));
assert.ok(!body.textContent.includes("邮件已发送"));
const customPrompt = body.querySelectorAll("textarea")[0];
assert.equal(customPrompt.value, "合成只读指令");
assert.equal(customPrompt.required, true);
customPrompt.value = "合成修改指令";
assert.ok(findButton(body, "恢复"));
body.querySelectorAll("form")[0].events.submit({ preventDefault() {} });
await new Promise(resolve => setImmediate(resolve));
assert.equal(calls.at(-1)[1].prompt_text, "合成修改指令");
assert.equal(calls.at(-1)[1].delivery, "in_app,email");
root = context.buildCard(pending, false);
assert.ok(root.textContent.includes("自定义指令"));
assert.ok(root.textContent.includes("合成修改指令"));
assert.ok(root.textContent.includes("邮件（未配置时跳过）"));

// Legacy comma-separated values and canonical API values must survive a prompt
// edit. Like a native select, an unmatched value clears the selection.
const select = h("select", {}, [h("option", { value: "both" })]);
select.value = "in_app,channel";
assert.equal(select.value, "");
for (const savedDelivery of ["in_app,channel", "both"]) {
  schedule.delivery = savedDelivery;
  await context.loadDetail(1);
  assert.equal(body.querySelectorAll("select")[0].value, "both");
  body.querySelectorAll("textarea")[0].value = `合成修改：${savedDelivery}`;
  body.querySelectorAll("form")[0].events.submit({ preventDefault() {} });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.at(-1)[1].delivery, "both");
  assert.equal(calls.at(-1)[1].prompt_text, `合成修改：${savedDelivery}`);
  await context.loadDetail(1);
  assert.equal(body.querySelectorAll("select")[0].value, "both");
  assert.equal(body.querySelectorAll("textarea")[0].value, `合成修改：${savedDelivery}`);
}
