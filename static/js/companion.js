/**
 * 搭子面板：Q 版形象 + 今日热量账本 + 餐选项 + 沉睡/唤醒。
 * 只消费 /api/companion；不在这里计算热量，避免前后端两套算法。
 */
import { $, h, toast, announce } from "./core.js?v=__APP_VERSION__";
import { api, friendlyError } from "./api.js?v=__APP_VERSION__";
import { showRestCard } from "./restcard.js?v=__APP_VERSION__";
import { currentUserId } from "./identity.js?v=__APP_VERSION__";

let status = null;
let busy = false;
let dock = null;
let dockAvatar = null;
let dockBadge = null;
let panel = null;
let panelBody = null;
let avatarHost = null;
let timer = 0;
let photo = { state: "idle", items: [], note: "", error: "", preview: "" };
let reportState = { state: "idle", text: "", delivery: "", error: "" };

const SEX = [["other", "不填"], ["male", "男"], ["female", "女"]];
const ACTIVITY = [["sedentary", "久坐少动"], ["light", "每周 1–2 练"], ["moderate", "每周 3–4 练"], ["high", "每周 5 练以上"]];
const GOAL = [["maintain", "维持"], ["cut", "减脂"], ["bulk", "增重"]];
const SOCIAL = [["", "自动判断"], ["偏内向", "偏内向"], ["中间型", "中间型"], ["偏外向", "偏外向"]];

function avatarSvg(a = {}, size = 96) {
  const round = Number(a.roundness || 0);
  const mood = a.mood || "ok";
  const dead = !!a.dead;
  const scale = Number(a.scale || 1);
  const bodyRx = 27 + round * 30;
  const bodyRy = 23 + round * 13;
  const headR = 25 + round * 5;
  const ink = dead ? "#8A8A92" : "#2B2118";
  const skin = dead ? "#E7E7EA" : "#FFE0BA";
  const body = dead ? "#D8D8DE" : "#FFCF8B";
  const blush = dead ? "#C9C9D1" : "#FF9A9E";
  const eyes = {
    happy: `<path d="M-9 -2 q4.5 -5 9 0" /><path d="M9 -2 q4.5 -5 9 0" />`,
    ok: `<circle cx="-10" cy="-1" r="2.6" /><circle cx="10" cy="-1" r="2.6" />`,
    worried: `<circle cx="-10" cy="-1" r="2.4" /><circle cx="10" cy="-1" r="2.4" /><path d="M-16 -9 l9 3" /><path d="M16 -9 l-9 3" />`,
    sad: `<path d="M-15 -3 q5 3 9 0" /><path d="M15 -3 q-5 3 -9 0" />`,
    sleep: `<path d="M-15 -1 q6 4 12 0" /><path d="M3 -1 q6 4 12 0" />`,
  };
  const mouth = {
    happy: `<path d="M-7 8 q7 7 14 0" />`,
    ok: `<path d="M-5 8 q5 4 10 0" />`,
    worried: `<path d="M-6 10 h12" />`,
    sad: `<path d="M-6 11 q6 -6 12 0" />`,
    sleep: `<path d="M-5 9 q5 3 10 0" />`,
  };
  const zzz = dead ? `<text x="78" y="24" font-size="14" fill="#9A9AA2">z</text><text x="90" y="12" font-size="10" fill="#9A9AA2">z</text>` : "";
  const hungry = a.hungry && !dead ? `<path d="M96 34 q6 10 0 14 q-6 -4 0 -14z" fill="#8FD3FF" stroke="${ink}" stroke-width="1.6"/>` : "";
  const spark = a.streak >= 3 && !dead
    ? `<text x="4" y="18" font-size="12" fill="#F59E0B">✦</text><text x="100" y="70" font-size="9" fill="#F59E0B">✦</text>`
    : "";
  return `<svg class="buddy-svg" viewBox="0 0 120 150" width="${size}" height="${Math.round(size * 1.25)}" role="img" aria-label="搭子形象">
    <g transform="translate(60 132) scale(${scale}) translate(-60 -132)">
      ${zzz}${hungry}${spark}
      <ellipse cx="60" cy="142" rx="${28 + round * 10}" ry="5" fill="rgba(25,25,25,.10)" />
      <path d="M50 120 q-5 12 -2 18" stroke="${ink}" stroke-width="6" stroke-linecap="round" fill="none" />
      <path d="M70 120 q5 12 2 18" stroke="${ink}" stroke-width="6" stroke-linecap="round" fill="none" />
      <ellipse cx="47" cy="139" rx="6" ry="3.6" fill="${ink}" opacity=".85" />
      <ellipse cx="73" cy="139" rx="6" ry="3.6" fill="${ink}" opacity=".85" />
      <ellipse cx="60" cy="104" rx="${bodyRx}" ry="${bodyRy}" fill="${body}" stroke="${ink}" stroke-width="2.6" />
      <path d="M${60 - bodyRx * 0.55} 104 q${bodyRx * 0.55} 10 ${bodyRx * 1.1} 0" fill="none" stroke="${ink}" stroke-width="1.4" opacity=".25" />
      <path d="M${60 - bodyRx - 1} 98 q-13 8 -14 20" stroke="${ink}" stroke-width="5.4" stroke-linecap="round" fill="none" />
      <path d="M${60 + bodyRx + 1} 98 q13 8 14 20" stroke="${ink}" stroke-width="5.4" stroke-linecap="round" fill="none" />
      <circle cx="60" cy="56" r="${headR}" fill="${skin}" stroke="${ink}" stroke-width="2.6" />
      <path d="M${60 - headR * 0.75} ${56 - headR * 0.55} q${headR * 0.3} -${headR * 0.55} ${headR * 0.75} -${headR * 0.35}" stroke="${ink}" stroke-width="2" fill="none" opacity=".35" />
      <g transform="translate(60 56)" stroke="${ink}" stroke-width="2.4" fill="none" stroke-linecap="round">${eyes[mood] || eyes.ok}${mouth[mood] || mouth.ok}</g>
      <ellipse cx="${60 - headR * 0.62}" cy="62" rx="4.4" ry="2.8" fill="${blush}" opacity=".55" />
      <ellipse cx="${60 + headR * 0.62}" cy="62" rx="4.4" ry="2.8" fill="${blush}" opacity=".55" />
      <path d="M${60 - 6} ${56 - headR} q6 -7 12 0" fill="${skin}" stroke="${ink}" stroke-width="2.2" />
    </g>
  </svg>`;
}

function pct(value, total) {
  if (!total) return 0;
  return Math.max(0, Math.min(100, Math.round((Number(value || 0) / Number(total)) * 100)));
}

function badgeText(s) {
  if (!s || s.needs_setup) return "设置";
  if (s.dead) return "睡着";
  const net = s.energy && s.energy.net;
  if (net === null || net === undefined) return "待记录";
  return `${net > 0 ? "+" : ""}${Math.round(net)}`;
}

function renderDock() {
  if (!dock) return;
  dockAvatar.innerHTML = avatarSvg((status && status.avatar) || { mood: "ok" }, 46);
  dockBadge.textContent = badgeText(status);
  const tired = status && (status.dead || (status.hunger_days_left ?? 99) <= 3);
  dock.classList.toggle("is-tired", !!tired);
  dock.setAttribute("aria-label", `搭子：${badgeText(status)}，打开面板`);
}

function stat(label, value, hint = "") {
  return h("div", { class: "buddy-stat" }, [
    h("span", { class: "buddy-stat-label", text: label }),
    h("span", { class: "buddy-stat-value", text: value }),
    hint ? h("span", { class: "buddy-stat-hint", text: hint }) : null,
  ]);
}

function formRow(label, input) {
  return h("label", { class: "buddy-field" }, [h("span", { text: label }), input]);
}

function setupForm(s) {
  const body = s.body || {};
  const height = h("input", { class: "field-input", type: "number", min: "100", max: "250", step: "0.5", value: body.body_height_cm || "", placeholder: "170" });
  const weight = h("input", { class: "field-input", type: "number", min: "25", max: "300", step: "0.1", value: body.body_weight_kg || "", placeholder: "65" });
  const fat = h("input", { class: "field-input", type: "number", min: "3", max: "60", step: "0.5", value: body.body_fat_pct || "", placeholder: "可选" });
  const age = h("input", { class: "field-input", type: "number", min: "10", max: "100", value: body.body_age || 30 });
  const name = h("input", { class: "field-input", maxlength: "24", value: s.name || "阿动" });
  const sex = h("select", { class: "field-input" }, SEX.map(([v, t]) => h("option", { value: v, selected: v === (body.body_sex || "other") ? "" : null, text: t })));
  const activity = h("select", { class: "field-input" }, ACTIVITY.map(([v, t]) => h("option", { value: v, selected: v === (body.activity_level || "light") ? "" : null, text: t })));
  const goal = h("select", { class: "field-input" }, GOAL.map(([v, t]) => h("option", { value: v, selected: v === (body.body_goal || "maintain") ? "" : null, text: t })));
  const social = h("select", { class: "field-input" }, SOCIAL.map(([v, t]) => h("option", { value: v, selected: v === (s.social && s.social.source === "manual" ? s.social.label : "") ? "" : null, text: t })));

  const save = h("button", { type: "submit", class: "btn btn-primary btn-sm", text: s.needs_setup ? "创建搭子" : "保存" });
  const form = h("form", { class: "buddy-form" }, [
    h("div", { class: "buddy-grid2" }, [formRow("身高 cm", height), formRow("体重 kg", weight)]),
    h("div", { class: "buddy-grid2" }, [formRow("体脂 %", fat), formRow("年龄", age)]),
    h("div", { class: "buddy-grid2" }, [formRow("性别", sex), formRow("活动量", activity)]),
    h("div", { class: "buddy-grid2" }, [formRow("目标", goal), formRow("社交风格", social)]),
    formRow("搭子名字", name),
    h("p", { class: "buddy-note", text: "体脂和年龄可以不填；不填时按估算处理。" }),
    h("div", { class: "buddy-actions" }, [save]),
  ]);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    save.disabled = true;
    try {
      const res = await api.companionSave({
        height_cm: Number(height.value || 0),
        weight_kg: Number(weight.value || 0),
        body_fat_pct: fat.value === "" ? null : Number(fat.value),
        age: Number(age.value || 30),
        sex: sex.value,
        activity_level: activity.value,
        goal: goal.value,
        name: name.value.trim(),
        social_label: social.value,
      });
      applyStatus(res.data && res.data.status);
      toast("搭子已更新");
      announce("搭子已更新");
    } catch (err) {
      toast(friendlyError(err, "搭子").text);
    } finally {
      busy = false;
      save.disabled = false;
    }
  });
  return form;
}



async function compressImage(file) {
  if (!file || !file.type || !file.type.startsWith("image/")) return file;
  if (typeof createImageBitmap !== "function") return file;
  try {
    const bitmap = await createImageBitmap(file);
    const max = 1024;
    const scale = Math.min(1, max / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.82));
    if (!blob) return file;
    return new File([blob], "food.jpg", { type: "image/jpeg" });
  } catch {
    return file;
  }
}

function pickPhoto() {
  const input = h("input", { type: "file", accept: "image/*", capture: "environment", hidden: "" });
  input.addEventListener("change", () => {
    const file = (input.files || [])[0];
    input.remove();
    if (file) analyzePhoto(file);
  });
  document.body.append(input);
  input.click();
}

async function analyzePhoto(file) {
  photo = { state: "loading", items: [], note: "", error: "", preview: URL.createObjectURL(file) };
  renderPanel();
  try {
    const compressed = await compressImage(file);
    const res = await api.companionFoodAnalyze(compressed);
    const data = res.data || {};
    photo = { state: "ready", items: data.items || [], note: data.note || "", error: "", preview: photo.preview };
  } catch (err) {
    const info = friendlyError(err, "识餐");
    photo = { state: "error", items: [], note: "", error: info.text, preview: photo.preview };
  }
  renderPanel();
}

async function logPhotoItem(item, all = false) {
  if (busy) return;
  busy = true;
  try {
    const targets = all ? photo.items : [item];
    for (const target of targets) {
      await api.companionMeal({
        name: target.name,
        kcal: target.kcal,
        protein_g: target.protein_g,
        carbs_g: target.carbs_g,
        fat_g: target.fat_g,
        source: "photo",
      });
    }
    toast(all ? `已记下 ${targets.length} 项` : "已记下");
    photo = { state: "idle", items: [], note: "", error: "", preview: "" };
    await refreshCompanion();
  } catch (err) {
    toast(friendlyError(err, "饮食").text);
  } finally {
    busy = false;
  }
}

function photoSection(s) {
  const vision = s.vision || {};
  const children = [h("h4", { text: "拍照识餐" })];
  children.push(h("p", { class: "buddy-note", text: vision.message || "拍一张食物照片，我给候选热量，你确认后才记账。" }));
  if (photo.preview) {
    children.push(h("img", { class: "buddy-photo", src: photo.preview, alt: "待识别的食物照片" }));
  }
  if (photo.state === "loading") children.push(h("p", { class: "buddy-note", text: "正在识别…" }));
  if (photo.state === "error") children.push(h("p", { class: "buddy-hunger is-warn", text: photo.error }));
  if (photo.state === "ready") {
    if (photo.note) children.push(h("p", { class: "buddy-note", text: photo.note }));
    children.push(h("div", { class: "buddy-candidates" }, photo.items.map((item) =>
      h("div", { class: "buddy-candidate" }, [
        h("div", {}, [
          h("strong", { text: `${item.name} · ${item.kcal} kcal` }),
          h("p", { text: [item.portion, item.protein_g ? `蛋白 ${item.protein_g} g` : "", `可信度 ${Math.round((item.confidence || 0) * 100)}%`].filter(Boolean).join(" · ") }),
        ]),
        h("button", { type: "button", class: "btn btn-ghost btn-sm", text: "记下", onClick: () => logPhotoItem(item) }),
      ])
    )));
    children.push(h("div", { class: "buddy-actions" }, [
      h("button", { type: "button", class: "btn btn-primary btn-sm", text: `全部记下（${photo.items.length}）`, onClick: () => logPhotoItem(null, true) }),
    ]));
  }
  children.push(h("div", { class: "buddy-actions" }, [
    h("button", {
      type: "button",
      class: "btn btn-ghost btn-sm",
      text: photo.state === "loading" ? "识别中…" : "拍照 / 选图",
      disabled: photo.state === "loading" || !vision.configured ? "" : null,
      onClick: pickPhoto,
    }),
  ]));
  if (!vision.configured) {
    children.push(h("p", { class: "buddy-note", text: "未配置时可先用文字记录；配置 VISION_MODEL 后即可拍照。" }));
  }
  return h("section", { class: "buddy-section" }, children);
}

function restSection() {
  const seconds = h("input", { class: "field-input buddy-seconds", type: "number", min: "15", max: "600", step: "15", value: "90" });
  const start = async (value) => {
    try {
      const res = await api.companionRestTimer({ seconds: Number(value || 90) });
      const card = res.data && res.data.card;
      panel.close();
      showRestCard(card);
    } catch (err) {
      toast(friendlyError(err, "计时").text);
    }
  };
  return h("section", { class: "buddy-section" }, [
    h("h4", { text: "组间休息" }),
    h("div", { class: "buddy-rest-row" }, [
      ...[60, 90, 120].map((value) => h("button", {
        type: "button", class: "btn btn-ghost btn-sm", text: `${value} 秒`, onClick: () => start(value),
      })),
      seconds,
      h("button", { type: "button", class: "btn btn-primary btn-sm", text: "开始", onClick: () => start(seconds.value) }),
    ]),
    h("p", { class: "buddy-note", text: "倒计时结束会响铃并震动；休息时也可以直接和我聊这一组的感受。" }),
  ]);
}

function reportSection() {
  const children = [h("h4", { text: "本周周报" })];
  if (reportState.state === "loading") children.push(h("p", { class: "buddy-note", text: "正在生成…" }));
  if (reportState.text) children.push(h("pre", { class: "buddy-report", text: reportState.text }));
  if (reportState.delivery) children.push(h("p", { class: "buddy-note", text: reportState.delivery }));
  if (reportState.error) children.push(h("p", { class: "buddy-hunger is-warn", text: reportState.error }));
  children.push(h("div", { class: "buddy-actions" }, [
    h("button", {
      type: "button", class: "btn btn-ghost btn-sm", text: "预览",
      onClick: async () => {
        reportState = { state: "loading", text: "", delivery: "", error: "" };
        renderPanel();
        try {
          const res = await api.companionReport(7);
          reportState = { state: "ready", text: (res.data && res.data.body) || "", delivery: "", error: "" };
        } catch (err) {
          reportState = { state: "error", text: "", delivery: "", error: friendlyError(err, "周报").text };
        }
        renderPanel();
      },
    }),
    h("button", {
      type: "button", class: "btn btn-ghost btn-sm", text: "推送到已绑定渠道",
      onClick: async () => {
        reportState = { state: "loading", text: reportState.text, delivery: "", error: "" };
        renderPanel();
        try {
          const res = await api.companionSendReport(7);
          const delivery = (res.data && res.data.delivery) || {};
          reportState = {
            state: "ready",
            text: (res.data && res.data.report && res.data.report.body) || reportState.text,
            delivery: delivery.ok ? `已推送到 ${delivery.channel}` : "已放进应用内收件箱（还没有绑定渠道）",
            error: "",
          };
        } catch (err) {
          reportState = { state: "error", text: reportState.text, delivery: "", error: friendlyError(err, "周报").text };
        }
        renderPanel();
      },
    }),
  ]));
  return h("section", { class: "buddy-section" }, children);
}

function mealList(s) {
  const options = s.suggestions || [];
  if (!options.length) return null;
  return h("div", { class: "buddy-meals" }, [
    h("h4", { text: "不知道吃什么？" }),
    ...options.map((item) =>
      h("div", { class: "buddy-meal" }, [
        h("div", {}, [
          h("strong", { text: `${item.name} · ${item.kcal} kcal` }),
          h("p", { text: item.reason || item.note || "" }),
        ]),
        h("button", {
          type: "button",
          class: "btn btn-ghost btn-sm",
          text: "记下",
          onClick: () => logSuggestion(item),
        }),
      ])
    ),
  ]);
}

async function logSuggestion(item) {
  if (busy) return;
  busy = true;
  try {
    const res = await api.companionMeal({
      name: item.name,
      kcal: item.kcal,
      protein_g: item.protein_g,
      carbs_g: item.carbs_g,
      fat_g: item.fat_g,
      source: "suggestion",
    });
    applyStatus(res.data && res.data.status);
    toast("已记下这一餐");
  } catch (err) {
    toast(friendlyError(err, "饮食").text);
  } finally {
    busy = false;
  }
}

function deadBlock(s) {
  return h("div", { class: "buddy-dead" }, [
    h("p", { class: "buddy-farewell", text: s.farewell || `${s.name} 睡着了。` }),
    h("p", { class: "buddy-note", text: "数据都还在；叫醒后会从新一代重新开始。" }),
    h("button", {
      type: "button",
      class: "btn btn-primary btn-sm",
      text: `叫醒第 ${Number(s.generation || 1) + 1} 代`,
      onClick: revive,
    }),
  ]);
}

async function revive() {
  if (busy) return;
  busy = true;
  try {
    const res = await api.companionRevive();
    applyStatus(res.data && res.data.status);
    toast("它醒了");
    announce("搭子醒了");
  } catch (err) {
    toast(friendlyError(err, "唤醒").text);
  } finally {
    busy = false;
  }
}

function renderPanel() {
  if (!panelBody) return;
  const s = status;
  if (!s) {
    panelBody.replaceChildren(h("p", { class: "buddy-note", text: "正在读取搭子状态…" }));
    return;
  }
  const avatarBox = h("div", { class: "buddy-avatar" });
  avatarBox.innerHTML = avatarSvg(s.avatar || {}, 128);
  const social = s.social || {};
  const socialChip = h("span", { class: "buddy-chip", text: `${social.label || "还没看出来"}${social.confidence ? ` · ${Math.round(social.confidence * 100)}%` : ""}` });
  const body = s.body || {};
  const day = s.energy || {};
  const net = day.net;
  const netText = net === null || net === undefined ? "待记录" : `${net > 0 ? "+" : ""}${Math.round(net)} kcal`;
  const stats = h("div", { class: "buddy-stats" }, [
    stat("摄入", `${Math.round(day.intake || 0)} kcal`, `${s.meals_today || 0} 餐`),
    stat("运动消耗", `${Math.round(day.exercise_burn || 0)} kcal`, `${s.workouts_today || 0} 次`),
    stat("热量差", netText, day.direction || ""),
  ]);
  const meta = h("p", { class: "buddy-note" }, [
    `维持约 ${Math.round(day.maintenance || 0)} kcal`,
    day.bmi ? ` · BMI ${day.bmi}` : "",
    body.body_weight_kg ? ` · ${body.body_weight_kg} kg` : "",
    ` · 连续 ${s.streak_days || 0} 天`,
  ]);
  const hunger = s.dead
    ? null
    : h("p", { class: `buddy-hunger${(s.hunger_days_left ?? 99) <= 7 ? " is-warn" : ""}`, text: `还有 ${s.hunger_days_left} 天不见面，${s.name} 会饿睡。` });

  const children = [
    h("div", { class: "buddy-head" }, [
      avatarBox,
      h("div", { class: "buddy-title" }, [
        h("h3", { text: `${s.name || "阿动"} ` }),
        h("p", { class: "buddy-sub", text: `第 ${s.generation || 1} 代 · ${(s.stage && s.stage.stage) || "幼崽"} · 第 ${(s.stage ? s.stage.age_days : 0) + 1} 天` }),
        h("div", { class: "buddy-chips" }, [socialChip, h("span", { class: "buddy-chip", text: (s.mood === "happy" ? "精神很好" : s.mood === "worried" ? "有点担心你" : s.mood === "sad" ? "蔫了" : s.mood === "sleep" ? "睡着了" : "在陪你") })]),
      ]),
    ]),
    s.needs_setup ? h("p", { class: "buddy-msg", text: s.message || "先填一下身体数据。" }) : null,
    !s.needs_setup ? stats : null,
    !s.needs_setup ? meta : null,
    s.direction_text && !s.needs_setup ? h("p", { class: "buddy-msg", text: s.direction_text }) : null,
    !s.needs_setup && ((s.energy && s.energy.flags) || []).some((f) => ["low_intake", "aggressive_deficit", "fast_weight_loss"].includes(f))
      ? h("p", { class: "buddy-hunger is-warn", text: "今天热量差偏大，别为了数字硬扛；饿就吃，长期这样容易掉肌肉。" })
      : null,
    hunger,
    s.dead ? deadBlock(s) : null,
    s.dead ? null : restSection(),
    s.dead ? null : mealList(s),
    s.dead ? null : photoSection(s),
    s.dead ? null : reportSection(),
    h("details", { class: "buddy-details" }, [
      h("summary", { text: s.needs_setup ? "填写身体数据" : "修改身体数据 / 名字 / 社交风格" }),
      setupForm(s),
    ]),
    h("p", { class: "buddy-note", text: `摄像姿态检查：${(s.pose && s.pose.message) || "未接入"}` }),
    h("p", { class: "buddy-note", text: "热量是估算，不替代医生或营养师；体重看 2–4 周趋势更准。" }),
  ].filter(Boolean);
  panelBody.replaceChildren(...children);
}

function applyStatus(next) {
  if (!next) return;
  if (next.user_id && currentUserId() && next.user_id !== currentUserId()) return;
  status = next;
  renderDock();
  renderPanel();
}

export async function refreshCompanion() {
  try {
    const res = await api.companion();
    applyStatus(res.data);
  } catch {
    /* offline: keep the last avatar */
  }
}

function openPanel() {
  if (!panel || typeof panel.showModal !== "function") return;
  renderPanel();
  if (!panel.open) panel.showModal();
  refreshCompanion();
}

export function initCompanion() {
  if (dock) return;
  dock = h("button", { type: "button", class: "buddy-dock", "aria-label": "打开搭子面板", onClick: openPanel }, [
    (dockAvatar = h("span", { class: "buddy-dock-avatar" })),
    h("span", { class: "buddy-dock-text" }, [(dockBadge = h("span", { class: "buddy-dock-badge" })), h("span", { class: "buddy-dock-label", text: "搭子" })]),
  ]);
  document.body.append(dock);

  panel = document.createElement("dialog");
  panel.className = "buddy-panel";
  panel.setAttribute("aria-labelledby", "buddyTitle");
  panelBody = h("div", { class: "buddy-panel-body", id: "buddyBody" });
  panel.append(
    h("div", { class: "buddy-panel-head" }, [
      h("h2", { id: "buddyTitle", text: "我的搭子" }),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", text: "关闭", onClick: () => panel.close() }),
    ]),
    panelBody
  );
  panel.addEventListener("click", (event) => {
    if (event.target === panel) panel.close();
  });
  document.body.append(panel);

  document.addEventListener("turn-finished", () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(refreshCompanion, 600);
  });
  window.addEventListener("focus", refreshCompanion);
  refreshCompanion();
}
