# DESIGN.md

> Personal Agent 的界面应该像 Grok Bot 一样「开口就能用」：对话是**唯一**产品界面；记忆、草稿确认、提醒都以卡片出现在消息流里，不存在第二个主页面。浅色画布 + 软黑行动色 + 琥珀点缀；蓝只属于链接与状态点。

**范围**：`static/index.html`（对话，唯一产品页面）、`static/design.css`（共享 token 与组件）。
**约束**：API / 工具契约不变；默认 **浅色**；交互档位 **L2**；主语言中文；**单一对话页 IA（chat-only）**。
**字体**：Geist / Geist Mono 可变字重 woff2 **自托管**于 `static/fonts/`（构建期下载，运行时零外链）；中文自动回落 PingFang / 微软雅黑。

---

## 1. Visual Theme & Atmosphere

**Style**: Light SaaS（工具型浅色产品 UI）+ Grok 式对话优先
**Keywords**: 浅色画布、软黑 CTA、蓝仅链接/状态点、琥珀点缀、Geist、零表单门槛、确认卡片、对话内进度卡、克制动效
**Tone**: 干净、可信、偏工具 —— NOT 霓虹暗色、NOT 彩虹渐变、NOT Linear 近黑、NOT 营销大字报、NOT 多页面管理台
**Feel**: 打开就是输入框；行动永远是软黑，蓝只是链接与状态，琥珀只出现在「提醒」上；所有工作在同一条对话里完成。

**Interaction Tier**: L2 流畅交互
**Dependencies**: CSS only（无 GSAP / Lenis；L2 用 CSS transition + 轻量入场）

**页面角色（chat-only IA）**
- **对话页（唯一产品页面）**：Hero 空状态 + 消息流（气泡 / 草稿确认卡 / 提问卡 / 记忆卡 / 提醒卡）+ 底部 Composer；档案侧栏**默认收起**，是增强不是门槛，也不是第二个页面。
- **记忆 = 对话能力**：顶栏「记忆」与 Hero chip 只是把记忆卡渲染进消息流；用户可在卡上修改、删除、遗忘。**不跳转、不开新页**。

---

## 2. Color Palette & Roles

```css
:root {
  /* Backgrounds — light */
  --bg: #F7F7F8;                 /* 页面画布 */
  --surface: #FFFFFF;            /* 卡片、气泡底、Composer 容器 */
  --surface-alt: #FAFAFB;        /* 卡片弱底、代码块弱底 */
  --surface-soft: #F0F0F2;       /* hover 铺底 / badge 底 */
  --surface-hover: rgba(25, 25, 25, 0.04);
  --surface-active: rgba(25, 25, 25, 0.07);

  /* Borders */
  --border: #E7E7EA;
  --border-hover: #C9CDD4;
  --border-strong: rgba(25, 25, 25, 0.16);

  /* Text */
  --text: #191919;
  --text-secondary: #5F5F66;
  --text-tertiary: #9A9AA2;
  --text-inverse: #FFFFFF;

  /* Accent — 软黑即主色（主 CTA / 发送 / 用户气泡） */
  --accent: #191919;
  --accent-hover: #0E0E0E;
  --accent-soft: rgba(25, 25, 25, 0.06);
  --accent-soft-2: rgba(25, 25, 25, 0.10);
  --accent-ring: rgba(25, 25, 25, 0.22);

  /* Blue — 仅链接与状态点，不作主行动色 */
  --blue: #008CFF;
  --blue-deep: #0049D6;
  --blue-soft: rgba(0, 140, 255, 0.08);
  --link: var(--blue-deep);
  --link-hover: var(--blue);

  /* Amber — 提醒 / 需要人看一眼 / 暂不可用 */
  --amber: #F59E0B;
  --amber-deep: #B45309;
  --amber-soft: rgba(245, 158, 11, 0.14);

  /* Semantic */
  --success: #16A34A;  --success-soft: rgba(22, 163, 74, 0.10);
  --error:   #F53F3F;  --error-soft:   rgba(245, 63, 63, 0.09);
  --warning: var(--amber);  --warning-soft: var(--amber-soft);
  --info:    var(--accent); --info-soft:  var(--accent-soft);

  /* Radius（12–16 为主） */
  --radius-tag: 6px;  --radius-compact: 8px;  --radius-control: 12px;
  --radius-card: 16px; --radius-feature: 20px; --radius-pill: 9999px;

  /* Motion */
  --ease-out: cubic-bezier(0.22, 1, 0.36, 1);
  --duration-fast: 0.12s; --duration: 0.18s; --duration-sidebar: 0.28s;

  /* Elevation — border-first，投影保持安静 */
  --shadow-hover: 0 4px 16px rgba(23,24,26,0.05);
  --shadow-raised: 0 8px 24px rgba(25,25,25,0.09);
  --shadow-modal: 0 24px 48px rgba(15,15,20,0.14);
  --scrim: rgba(20,20,24,0.42);
}
```

**Color Rules:**
- 所有颜色通过 CSS 变量引用；组件样式里禁止裸 hex（变量定义区除外）。
- **主 CTA / 用户气泡 / 品牌标 = 软黑（#191919 → hover #0E0E0E）**，无阴影。
- 蓝（#008CFF / #0049D6）**只**用于链接、链接 hover、状态点；不得用作主按钮底、focus ring、拖拽高亮或大色块。
- 琥珀只服务「需要人看一眼」的场景：提醒卡、暂不可用提示、澄清卡左边线。
- 同一视线区内最多一个实心软黑按钮；其余用 Ghost / Quiet。
- 默认主题浅色；不做暗色变体（本期）。

---

## 3. Typography Rules

**Font Stack**（Geist 自托管可变字体，CJK 自动回落系统字体；**禁止**运行时外链 webfont CDN）：

```css
@font-face {
  font-family: "Geist";
  src: url("/static/fonts/Geist-Variable.woff2") format("woff2-variations");
  font-weight: 100 900;
  font-display: swap;
}
:root {
  --font-sans: "Geist", -apple-system, BlinkMacSystemFont, "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", system-ui, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
  --font-mono: "Geist Mono", ui-monospace, SFMono-Regular, Menlo, Monaco,
    Consolas, "Liberation Mono", "Courier New", monospace;
}
```

| Role | Font | Size | Weight | Line Height | Letter Spacing |
|------|------|------|--------|-------------|----------------|
| Page H1（顶栏标题） | sans | 16px | 600 | 1.3 | -0.01em |
| Hero H2 | sans | 22–26px | 600 | 1.3 | -0.02em |
| H3（卡片标题 / 列名） | sans | 12.5–14px | 600 | 1.4 | — |
| Body / 气泡 | sans | 14–15px | 400 | 1.6 | — |
| Composer 输入 | sans | 15.5px | 400 | 1.55 | — |
| Label / Meta | sans | 12px | 400–500 | 1.5 | — |
| Badge / Pill / Chip | sans | 11–12px | 500 | 1.3 | — |
| Mono（thread id / 调试徽章） | mono | 11.5–12px | 400 | 1.55 | — |

**Typography Rules:**
- 字重只用 **400 / 500 / 600**；中文加粗用 500，英文与数字强调用 600。
- 数字用 `font-variant-numeric: tabular-nums`（匹配分、统计、时间）。
- **NEVER**: 外链 Google Fonts、彩虹渐变字、字重 700+。

---

## 4. Component Stylings

### Buttons

```css
.btn { border-radius: var(--radius-control); padding: 10px 20px;
  font-size: 14px; font-weight: 600; transition: background/border/color/transform/box-shadow; }
.btn:focus-visible { outline: 2px solid var(--accent-ring); outline-offset: 2px; }
.btn:disabled { opacity: 0.45; cursor: not-allowed; }

/* Primary — 软黑实心，hover 加深，无阴影 */
.btn-primary { background: var(--accent); color: var(--text-inverse); }
.btn-primary:hover:not(:disabled) { background: var(--accent-hover); }
.btn-primary:active:not(:disabled) { transform: translateY(1px); }

/* Ghost — 白底描边 */ /* Quiet — 无边透明 */
.btn-ghost { background: var(--surface); border: 1px solid var(--border); }
.btn-ghost:hover:not(:disabled) { background: var(--surface-soft); border-color: var(--border-hover); }
.btn-quiet { background: transparent; color: var(--text-secondary); }
.btn-quiet:hover:not(:disabled) { background: var(--surface-hover); color: var(--text); }

/* Danger / Success 仅确认卡与抽屉次级动作 */
.btn-danger { background: var(--error); color: var(--text-inverse); }
.btn-success { background: var(--success); color: var(--text-inverse); }

.btn-sm { padding: 6px 12px; font-size: 12px; border-radius: var(--radius-compact); min-height: 32px; }
.btn-icon { width: 38px; height: 38px; border-radius: var(--radius-control); color: var(--text-secondary); }
.btn-icon:hover { background: var(--surface-hover); color: var(--text); }
```

### Cards（分析卡 / 确认卡 / 澄清卡）

```css
.confirm-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-card);
  padding: 16px 18px;
  max-width: 780px;
  align-self: stretch;
}
.confirm-card:hover { border-color: var(--border-hover); box-shadow: var(--shadow-hover); }
.confirm-card .score { font-size: 30px; font-weight: 600; color: var(--text); }
/* 草稿正文用正文字体（14px / 1.75 / pre-wrap），不用 pre、不用等宽灰底调试框 */
.confirm-card .draft-body { font-size: 14px; line-height: 1.75; white-space: pre-wrap; }
.confirm-card textarea.draft-edit { /* 「编辑」切换为同字号文本域，完成后随确认带 edits */ }
/* 动作三件套：软黑「确认草稿」+ Ghost「编辑」+ 文字钮「不用了」 */
.confirm-card .card-error { display: none; /* 冲突/失败时一条明确中文错误 */ }
.confirm-card .card-error.is-show { display: block; background: var(--error-soft); color: var(--error); }

/* 澄清卡：need_clarify 不是失败 —— 琥珀左边线 + 两个分流按钮 */
.confirm-card.clarify { border-left: 3px solid var(--amber); }
.confirm-card.clarify h3 { color: var(--amber-deep); }
```

### Composer（Grok 式，零表单门槛）

```css
.composer-shell {
  display: flex; align-items: flex-end; gap: 8px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-card);   /* 16px */
  padding: 8px 8px 8px 10px;
}
.composer-shell:focus-within {
  border-color: var(--border-hover);
  box-shadow: var(--shadow-hover);
}
.composer-shell textarea { flex: 1; border: none; resize: none;
  font-size: 15.5px; line-height: 1.55; max-height: 180px; }
.composer-shell .btn-send { width: 40px; height: 40px; border-radius: var(--radius-control); }

/* 输入错误：Composer 下方一行明确中文，不用 alert */
.composer-error { color: var(--error); font-size: 12.5px; font-weight: 500; }
```

**Composer 行为约定（与后端契约对齐）：**
- 文本随 `/api/chat` 发送；写作请求返回 `awaiting_confirm` + `task_card(draft_confirm)` 时渲染草稿确认卡。
- 空发送 → 一条明确中文错误（`.composer-error`）。
- 密码 / 手机号 / 身份证被后端拦截（`redact_user_message`）→ 用户气泡显示脱敏文本，并渲染安全输入区卡片；绝不当作失败。
- 确认失败 → 卡片内显示后端给出的中文错误（`detail.message`）。
- 确认卡**永不过期**：按钮绑定卡片自己的 `thread_id`，决策后可再点，后端以后点覆盖先点（`decision.overridden`）。

### Sidebar（对话页档案面板，可选）

```css
.sidebar { width: 0; overflow: hidden; transition: width .28s var(--ease-out); }
.sidebar.is-open { width: 340px; border-left: 1px solid var(--border); }
/* ≤900px：固定右侧抽屉 + backdrop；默认收起 */
```

顶部固定一条提示：「可选项 — 不填也能用。聊天里告诉我的事，我会自动记住。」

### Header / Badges

```css
.app-header { background: rgba(255,255,255,.86); backdrop-filter: blur(12px); position: sticky; }
.brand-mark { width: 28px; border-radius: 9px; background: var(--accent); }
.badge .dot { width: 6px; border-radius: 50%; }
.badge.is-live .dot { background: var(--success); }
.badge.is-mock .dot { background: var(--amber); }
.badge.badge-amber { background: var(--amber-soft); color: var(--amber-deep); }
```

健康徽章来自 `GET /api/health`（404 时回落 `GET /health`）：
- `llm live|mock · fast provider/model · strong provider/model`

---

## 5. Layout Principles

- 对话消息最大宽：气泡 720px / 卡片 780px；Composer 内容列 860px 居中。
- 单一页面 IA：所有功能（记忆 / 定时 / 档案）都收在对话页内，无主导航、无页间跳转。
- 档案侧栏桌面 340px，默认收起，聊天优先。
- Spacing 基线 8：组件内 8–12，区块 14–20，卡片内边距 16–18。

```css
.layout { flex: 1; display: flex; min-height: 0; }
main.chat-main { flex: 1; display: flex; flex-direction: column; }
#log { flex: 1; overflow-y: auto; padding: 24px 20px 28px; }
```

---

## 6. Depth & Elevation

| Level | Treatment | Use |
|-------|-----------|-----|
| Flat | 无阴影，`--bg` | 页面画布、消息区 |
| Border | `1px solid var(--border)` | 卡片、列、输入、气泡 |
| Subtle hover | `0 4px 16px rgba(23,24,26,.05)` | 卡片 / Composer |
| Accent hover | 无阴影，仅加深至 `#0E0E0E` | 主按钮 hover、品牌标 |
| Raised | `0 8px 24px rgba(25,25,25,.09)` | 抽屉、浮层 |
| Modal | `0 24px 48px rgba(15,15,20,.14)` + scrim | 新建卡片模态、移动抽屉 |

**原则**：浅色主题**边框优先**；主按钮无阴影；禁止彩色大光晕。

---

## 7. Animation & Interaction

**Tier**: L2 · 只动 opacity 与 transform · 纯 CSS。

```css
@keyframes ja-fade-up { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
.enter { animation: ja-fade-up var(--duration) var(--ease-out) both; }

/* 发送中：极轻三点 typing（对标 lp-typing-dot） */
.typing-dots span { animation: ja-typing-dot 1.1s ease-in-out infinite; }
```

- 拖拽文件进入窗口：全屏虚线蓝框 overlay「松开，把文件加进对话」。
- 尊重 `prefers-reduced-motion`（全站 media query 兜底）。

---

## 8. Do's and Don'ts

### Do
- 默认浅色：画布 `#F7F7F8`，表面白，弱底 `#FAFAFB` / `#F0F0F2`。
- 主按钮 / 用户气泡 / 品牌标用软黑 `#191919`，hover `#0E0E0E`；蓝只做链接与状态点；琥珀只做提醒。
- 圆角以 12（控件）/ 16（卡片 / Composer / 气泡）为主。
- Geist 自托管 + 系统 CJK 回落；字重 400/500/600。
- 对话零门槛：Hero 空状态给出通用示例（记住一件事 / 写一封邮件 / 设个提醒）。
- 记忆留在对话里：记忆卡数据来自 `/api/memory/{user_id}/card` 真实返回，以卡片进入消息流。
- 所有 API 错误统一解析 `detail`（string 或 `{message, code}`），一条中文说清楚。
- 尊重 `prefers-reduced-motion`；键盘可达（focus-visible 蓝环、Esc 关抽屉）。

### Don't
- ❌ 用蓝做主按钮 / 主行动色、focus ring、拖拽高亮或大色块（蓝只属于链接与状态点）
- ❌ 暗色默认主题、霓虹、彩虹渐变、大面积玻璃拟态
- ❌ 运行时外链 webfont CDN
- ❌ 在组件 CSS 里硬编码 hex（变量表除外）
- ❌ 伪造数据：不显示后端没有的字段，不做假 +N 角标、不假造提醒
- ❌ 把隐私拦截当失败红错；它是正常分流
- ❌ 改 API 字段、确认卡协议

---

## 9. Responsive Behavior

| Name | Width | Key Changes |
|------|-------|-------------|
| Desktop | > 900px | 聊天主列 + 可选 340px 侧栏（单一页面，无页间导航） |
| Tablet | 640–900px | 侧栏变为右侧抽屉（backdrop） |
| Mobile | < 640px | 顶栏紧凑、气泡全宽、Composer 全宽；抽屉 94vw |

**Touch Targets**: 主要按钮 ≥ 40px 高；发送 / 回形针 40×40。
**Collapsing Strategy**: 侧栏 `position: fixed` 右侧抽屉；卡片在消息流内自然折行。

---

## 附录 A · API 契约对照（前端如何消费）

| 场景 | 端点 | 前端行为 |
|------|------|----------|
| 健康 | `GET /api/health`（404 回落 `/health`） | 调试徽章（LLM 档位 / 版本） |
| 对话 | `POST /api/chat` | 文本；渲染 messages / ask 卡 / 草稿确认卡 / profile |
| 草稿 | `POST /api/draft` | 直接发起写作请求，返回同对话的草稿确认卡 |
| 档案 | `GET/POST /api/profile[/user_id]` | 侧栏读写；保存后刷新「Agent 记住了什么」 |
| 记忆 | `GET /api/memory/{user_id}/card` 及 facts / events / forget | 对话内记忆卡；修改、删除、遗忘 |
| 确认 | `POST /api/confirm/{thread_id}/token` → `POST /api/confirm` | 卡片绑定自身 thread_id；可带 `edits` / `reason`；404/403 展示 `detail.message` |
| 定时 | `/api/schedules*` | 顶栏「定时」面板；对话内确认卡决定 |

## 附录 B · 实现检查清单

- [x] `:root` 含本文全部变量；组件无裸 hex
- [x] Primary = 软黑 `#191919`；蓝仅链接 / 状态点；琥珀只做提醒
- [x] 确认卡：软黑「确认草稿」+ Ghost「编辑」+ 文字钮「不用了」；草稿正文用正文字体
- [x] **chat-only IA**：`index.html` 无第二个主页面；记忆 / 定时 / 档案都在对话页内
- [x] 对话页 chrome 无常驻 User ID；错误只显示友好中文
- [x] Geist woff2 自托管（`static/fonts/`），无运行时外链
- [x] Composer 空发送一条中文错误；隐私拦截渲染安全输入区卡片
- [x] Hero 空状态给出通用示例 chips
- [x] L2：入场 fade-up + hover + typing dots；含 `prefers-reduced-motion`
- [x] 移动端侧栏抽屉 + 卡片流内折行 + Esc 关闭
- [ ] API 行为回归通过后再合入视觉 PR
