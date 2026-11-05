# Q 版运动搭子：产品说明与实现现状（0.12.0）

这份文档把「运动陪伴 AI 搭子」的完整设想拆成**已经能跑的部分**、**需要独立工程的部分**和**不建议直接做的部分**。代码只实现了标注「已实现」的条目；其余是接口位或设计，不做假承诺。

## 1. 一句话与核心循环

> 一个会因为你而变胖变瘦、会饿、会社交的 Q 版搭子；它靠记录你的饮食和运动活下去。

```
身体数据 → 搭子出生（体型由身高/体重/体脂决定）
   ↑                                   ↓
   └── 每次记录饮食 / 运动 → 热量差 → 体型与情绪变化
                                   ↓
              提醒、餐选项、阶段总结、社交风格、沉睡与离别
```

设计立场：

- **数据换形象，而不是打分。** 不做「健康分」「自律榜」，只让体型、精神和离别说话。
- **热量是估算，趋势才可信。** 所有数字都带「估算」标注，看 2–4 周趋势。
- **不制造羞耻。** 不因为多吃一顿就指责，不因为断更就扣分；沉睡文案写成「我先睡一会儿」，而不是「你害死了它」。

## 2. 已经实现（本仓库可直接跑）

| 能力 | 实现 | 代码 |
| --- | --- | --- |
| 身体数据建档、搭子出生 | 身高/体重/体脂/年龄/性别/活动量/目标 → 自动创建第 1 代 | `app/companion/store.py::set_body` |
| BMR / 维持 / 运动消耗 / 热量差 / 周体重方向 | Mifflin–St Jeor + MET 表；运动记录自动计入 | `app/companion/energy.py` |
| Q 版形象随体型变化 | 服务端给参数（圆润度、肌肉感、情绪、阶段、饥饿），前端 SVG 绘制 | `static/js/companion.js`、`GET /api/companion` |
| 饮食记录 | 名称、热量、三大营养素、来源（文字/估算/照片候选/推荐） | `companion.log_meal` |
| 「不知道吃什么」 | 20 个健康餐选项，按剩余额度、时段、目标、蛋白缺口、近期重复排序；聊天里给选择卡 | `app/companion/meals.py` |
| 今日账本面板 | 常驻左下角头像 + 面板：摄入 / 消耗 / 热量差 / 连续天数 / 饥饿倒计时 | `static/js/companion.js` + CSS |
| 一个月不见 → 沉睡 | `COMPANION_DORMANCY_DAYS`（默认 30）；自动生成阶段总结 + 离别赠语；可唤醒新一代 | `store.status` / `store.revive` |
| 阶段总结 | 训练次数/分钟/公里、饮食次数与日均、体重变化、连续天数 | `companion.recap` |
| i/e 社交风格 | 只统计**字数、问句、表情、感叹号**（不存原文），给「偏内向 / 中间型 / 偏外向」+ 置信度；可手动改 | `store.observe_message` / `social_profile` |
| 食物照片识别接口 | 图片只在内存处理、不落盘；未配置视觉模型时明确拒绝；结果必须用户确认再记账 | `app/companion/vision.py`、`POST /api/companion/food/analyze` |
| 姿态检查接口 | 明确返回「未接入」，要求端侧分析、不上传视频帧 | `app/companion/pose.py` |
| 社交策略文本 | 按 i/e 生成打招呼话术与节奏建议；附近的人默认关闭 | `app/companion/social.py` |
| 聊天集成 | 模型选工具 → 只抽取参数 → 注册表校验/注入身份 → 写入 → 中文反馈；缺参数只追问 | `app/companion/chat.py` |
| 数据导出与备份 | `/api/export` 导出本人数据（不含密钥）；`scripts/backup_data.sh` 在线备份 SQLite | `app/main.py`、`scripts/backup_data.sh` |
| 提示词注入 | 搭子状态（热量、情绪、饥饿、社交标签）以数据块注入，不当作指令 | `store.prompt_block` |

### 热量算法（可核对）

```
BMR = 10·kg + 6.25·cm − 5·age + (男 +5 / 女 −161 / 未填 −78)
基础消耗 = BMR × 1.2                      # NEAT，不包含本次训练
运动消耗 = Σ MET(项目, 强度) × kg × 小时   # 跑步中等 = 10 MET
维持 = 基础消耗 + 运动消耗
热量差 = 摄入 − 维持
周体重变化 ≈ 热量差 × 7 ÷ 7700 kg
```

安全阀：摄入低于 1200/1500 kcal、热量差 < −1000、周减重 > 1 kg、BMI < 18.5 仍在减脂 → 打标记并温和提示；不鼓励、不美化。

### 体型与情绪怎么变成形象

| 参数 | 来源 | 表现 |
| --- | --- | --- |
| `roundness` | 体重 ÷（BMI 22 的参考体重），夹在 −0.18…0.30 | 身体和脸更圆 / 更紧致 |
| `definition` | 体脂越低越高 | 轮廓与线条感 |
| `mood` | 今天有没有记录、断更天数、是否沉睡 | 眼睛和嘴形：开心 / 平常 / 担心 / 蔫 / 睡着 |
| `stage` | 出生天数：<14 幼崽、<60 少年、之后伙伴 | 整体缩放与文案 |
| `hungry` | 距离沉睡 ≤ 3 天 | 头顶汗滴 |
| `streak` | 连续训练天数 ≥ 3 | 小星星 |

## 3. 需要独立工程的部分（当前只有接口位/设计）

### 3.1 摄像头识别食物

已实现：`analyze_food(image) → [{name, portion, kcal, macros, confidence}]`，要求用户确认后再 `log_meal`。

接上真实模型只需要：`.env` 里配 `VISION_MODEL` + `LLM_FAST_BASE_URL/API_KEY`（OpenAI 兼容多模态）。

产品约束（已写进代码注释与接口）：

- 图片**不落盘、不进轨迹、不进日志**；只把候选结果交给用户。
- 识别不确定时降低 confidence，不编造品牌和精确克数。
- 端侧拍摄建议在前端压缩到 4 MB 内；后续可换成手机端小模型，照片不出设备。

### 3.2 摄像头看动作 / 一起练 / 组间休息讨论

这需要实时链路，当前仓库没有实现，只有 `app/companion/pose.py` 的诚实接口位：

- 端侧推理（MediaPipe Pose / TensorFlow.js / 原生），帧率 ≤ 5 fps，只回关键点与规则判定；
- 视频不存、不上传；用户可随时关闭摄像头；
- 判定规则先做 3–5 个动作（深蹲膝轨、硬拉背线、俯卧撑肘角、跑步落地），每个给「看到了什么 + 一个可执行调整」，不吹哨、不羞辱；
- 组间休息的讨论复用现有语音/文字输入，直接把最近一组数据（次数、重量、RPE）交给模型；
- 「一起练」= 搭子按同一套计时器和组数陪你做，用 TTS 报数；不需要真人视频。

### 3.3 附近的人、i/e 社交、PK 与捣乱

这是**独立服务**（位置、关系、通知、审核），不能塞进当前单体。设计要点已写进 `app/companion/social.py`：

- **默认关闭**；开启后只存 geohash5（≈1 km），不存经纬度；双方都开启才互相可见。
- 每人每天最多 3 次主动打招呼；一键拉黑即永久静默；昵称、搭子形象、运动标签之外不展示体重/饮食。
- i/e 只用于**调整说话方式**（先聊训练 / 主动邀约 / 给拒绝的台阶），不展示给第三方，不用于匹配排序。
- PK 只比步数、训练分钟、连续天数这类可验证指标，不涉及金钱和排名羞辱。
- 「捣乱」限定为视觉小动画（偷走一颗星星、把对方搭子喂胖一点点），有冷却、可关闭、可举报；不碰对方真实数据。

### 3.4 周报推送（已实现）

- 生成：`app/companion/report.py::build_report`，用和面板同一套数字（训练次数/分钟/公里、饮食次数与日均、体重变化、连续天数），末尾给一条下周建议。
- 推送：写入 `proactive_items`（应用内收件箱），再通过 `jobs._try_deliver` 发到第一个已绑定渠道（飞书优先，其次微信）；没有绑定就只进收件箱，不报错。
- 时机：`COMPANION_WEEKLY_WEEKDAY=6`、`COMPANION_WEEKLY_HOUR=20`（用户时区，默认周日 20 点）；每 ISO 周最多一条，幂等账本 `proactive_runs`。
- 手动：面板里的「预览 / 推送到已绑定渠道」，或 `GET|POST /api/companion/report`。

### 3.5 拍照识餐（已接真实视觉模型）

- 配置：`VISION_MODEL` + `VISION_API_KEY`（`VISION_BASE_URL` 缺省用 `LLM_FAST_BASE_URL`）。
- 链路：H5 拍照/选图 → 前端压缩到 ≤1024px JPEG → `POST /api/companion/food/analyze`（multipart）→ 返回候选（名称、份量、热量、三大营养素、置信度）→ 用户点「记下 / 全部记下」才写饮食。
- 约束：图片只在内存；服务端按 magic bytes 校验 JPG/PNG/WebP；≤4 MB；未配置返回 501，不猜；热量上限做 clamp。
- 测试：`tests/test_companion_extras.py` 用一个假上游验证解析、裁剪、错误码。

### 3.6 组间休息计时卡（已实现）

- 后端：`app/companion/timer.py` 只给参数（15–600 秒、动作、组数、一句动作提示），不假装能看动作。
- 聊天：模型选 `companion.rest_timer` → 回复文字 + `rest_card`；H5 渲染倒计时卡，结束响铃 + 震动。
- 面板：60/90/120 秒快捷键 + 自定义秒数，直接调 `POST /api/companion/rest-timer`，不依赖模型。

### 3.7 H5（已实现）

- `static/manifest.webmanifest` + 192/512/maskable 图标 + `apple-mobile-web-app-*`：iOS「添加到主屏幕」、Android 安装后全屏运行。
- 移动端：搭子面板是底部抽屉（≤600px），dock 在输入框上方并避开 `safe-area-inset-bottom`。
- 没有做 Service Worker（避免缓存旧版 JS 与调试困难）；离线能力放到后续。

### 3.8 数据来源

| 来源 | 现状 | 下一步 |
| --- | --- | --- |
| 文字 / 语音 | 文字已通；语音可用浏览器/微信自带的语音转文字，或接 ASR 后走同一条文本链路 | 独立 ASR 通道 + 确认卡 |
| 运动手表 / 手环 | 未接 | 优先 Strava / Apple Health / 华为运动健康导出，走 `sports.store.log_workout` 去重写入 |
| 外设摄像头 | 接口位 | 端侧视觉，见 3.1 / 3.2 |
| 手动记录 | 已实现 | — |

### 3.5 形象资产

现在是参数化 SVG（零依赖、即时变化）。要更「Q」可以换 Rive / Live2D：

- 服务端继续只给参数（圆润度、情绪、阶段），前端换渲染器；
- 不要把体型算在美术资源里，否则每加一档体重就要重画一套。

## 4. 不建议直接做的部分

1. **「饿死」作为惩罚。** 30 天机制已实现，但建议默认改叫「饿睡 / 去旅行了」，提供「永不失联」开关；对抑郁、进食障碍用户，愧疚感可能加重症状。当前文案已避免指责（「不怪你」）。
2. **把 i/e 当人格诊断。** 我们只做「社交能量偏好」的启发式，带置信度、可手动改；不对外展示，不用于匹配排序。
3. **用照片估热量当作精确值。** 中餐混合菜误差极大；只做候选 + 用户确认，界面统一标「估算」。
4. **未接入摄像头却暗示能看动作。** `pose.py` 直接返回 `not_connected`，避免「假 AI」。
5. **靠社交裂变拉新。** 先把单人闭环（记录 → 体型 → 陪伴 → 提醒）做扎实，再开附近的人；否则空城和骚扰会先来。

## 5. 数据与接口

### 存储（不新增表）

| 数据 | 位置 |
| --- | --- |
| 身体、搭子、社交计数 | `user_profiles.profile_json`（`body_*`、`companion_*`、`social_*`） |
| 饮食 | `episodic_events`，`kind="meal"` |
| 身体变化 | `episodic_events`，`kind="body"` |
| 沉睡 / 唤醒 | `episodic_events`，`kind="companion_starved"` / `companion_revive` |
| 运动 | 复用 `episodic_events`，`kind="workout"` |

### HTTP

| 路径 | 说明 |
| --- | --- |
| `GET /api/companion` | 状态、能量、形象参数、餐选项、社交标签、姿态/隐私说明 |
| `POST /api/companion/profile` | 身体数据 / 名字 / 社交风格 |
| `POST /api/companion/meal` | 记录一餐（面板里的「记下」） |
| `POST /api/companion/revive` | 唤醒新一代 |
| `POST /api/companion/food/analyze` | 图片 → 候选（不落盘；未配置视觉模型返回 501） |

### 聊天工具

`companion.set_body` / `status` / `log_meal` / `meal_suggest` / `recap` / `set_style` / `revive`，风险等级与是否需要确认卡见 `docs/tools.md`。

## 6. 路线图

| 阶段 | 内容 | 依赖 |
| --- | --- | --- |
| P0（已完成） | 身体档案、热量账本、Q 版 SVG、餐选项、沉睡/离别、社交风格、照片与姿态接口位 | 无 |
| P1（已完成 0.11） | H5 安装、周报推送、真实视觉模型接照片、组间休息计时卡 | 视觉 Key、渠道绑定 |
| P1.5 | 手环导入（Strava / Apple Health）、周报图表、周报静默时段 | 第三方授权 |
| P2（4–8 周） | 端侧姿态检查 3–5 个动作；语音输入；形象换 Rive；饮食历史图表 | 移动端/前端工程 |
| P3（独立服务） | 附近的人、i/e 社交策略上线、PK、捣乱、举报与审核 | 位置合规、关系服务、运营 |

## 7. 验证

```bash
python -m pytest -q tests/test_companion.py tests/test_companion_extras.py   # 22 个用例
AUTH_SECRET=x python tests/golden/run_golden.py --mode mock
```

没有 Key 也能看界面：`python scripts/demo_sports_llm.py` + 指向本机的 `LLM_FAST_BASE_URL`，打开首页左下角的搭子头像即可。
