# 定时任务与提醒

用户在对话里说「明早八点提醒我交报告」「每周一早上帮我看看 HN 头条」，模型选中 `schedule.*` 工具，服务端抽取参数后给出**确认卡**；用户确认才会保存。

## 任务类型

| `job_kind` | 说明 |
|------------|------|
| `one_shot_reminder` | 一次性提醒：`trigger.type = once`，必须有 `prompt_text` 和未来时间 |
| `custom_nl` | 自定义只读指令：`prompt_text` 保存用户原话；运行时模型规划最多 4 次白名单只读工具调用，再汇总成中文报告 |

白名单（`app/agent/schedules/custom.py`）：`web_fetch`、`web_search`、`fetch_feed`、`get_hn`、`search_social`、`memory_search`、`memory_card`。不允许发送、写数据或创建其他任务。

## 触发器

| `trigger.type` | 字段 |
|----------------|------|
| `once` | `run_at`（ISO 日期时间，无时区则按任务 `tz`） |
| `cron` | `expr` 五段表达式 |
| `interval` | `every_seconds`（60 秒 ~ 366 天） |

时区默认 `Asia/Shanghai`，档案里有 `tz` 时用档案。

## 送达

`delivery`：`in_app`（收件箱）、`channel`（已绑定的飞书 / 微信）、`both`、`email`（未配置传输时跳过并记录原因）。`channel_preference`：`auto` / `feishu` / `wechat`。

## 执行

- 调度器 `app/agent/proactive/scheduler.py` 按 `PROACTIVE_TICK_SECONDS` 轮询，`runner.run_due` 取到期任务，每个任务先写 `user_schedule_runs` 做一次性声明，再执行；失败不泄露来源文本。
- 每用户最多 10 个启用任务；`max_runs` 到达后自动完成；长期不活跃的用户任务会被 `activity.pause_idle` 冻结。
- 手动运行：`schedule.run_now` 工具 / `POST /api/schedules/{id}/run`。

## 接口

`GET /api/schedules`、`GET /api/schedules/{id}`、`PATCH`、`DELETE`、`POST .../pause|resume|run`、`GET .../runs`、`POST /api/schedules/proposals/{id}/decide`。工具 `schedule.create / update / delete` 只产生确认卡；`pause / resume / run_now` 直接生效。
