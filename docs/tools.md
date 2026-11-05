# 工具分类清单

工具经 `app.agent.tool_registry` 统一注册；身份只认服务器登录态，不接受模型参数指定的用户身份。
列表接口为 `/api/tools`、`/api/agent/tools`；更机制性的说明见 [tool-registry.md](tool-registry.md)。
本清单依据 `app/agent/tool_catalog.py` 的中文说明和 `ensure_defaults` / `list_tools` 的注册信息整理，包含按开关开放的记忆检索；注册并不表示对应外部服务已连接。

表中「是否需要确认卡」严格对应注册表的 `requires_confirm`，表示执行该工具是否必须先获确认。
草稿类（`draft_text`）与定时任务提案类（`schedule.create` / `schedule.update` / `schedule.delete`）会**弹出**确认卡供用户审核，但工具本身风险为 `read` / `self_write`、`requires_confirm=false`，因此表中记为「否」。
当前注册表中没有 `external` 工具：助理不会替用户对外发送邮件、消息或提交表单；若将来加入，必须点确认卡且确认凭证一次性使用。

风险等级沿用注册表：`read`（读取、检索或生成草稿）、`self_write`（写入本人数据或本地流程）、`notify_self`（主动提醒本人）、`external`（对外执行）。

## 档案

| name | 一句话用途 | 风险等级 | 是否需要确认卡 |
| --- | --- | --- | --- |
| `profile_read` | 读取用户已保存的个人档案 | `read` | 否 |
| `profile_save` | 整体保存用户的个人档案内容 | `self_write` | 否 |
| `profile_update` | 合并字段并保存用户个人档案 | `self_write` | 否 |

## 写作

草稿永远先进确认卡；工具本身不发送任何内容。

| name | 一句话用途 | 风险等级 | 是否需要确认卡 |
| --- | --- | --- | --- |
| `draft_text` | 根据用户的写作请求和档案生成一版草稿 | `read` | 否 |

## 运动

运动工具只读写当前用户自己的运动档案、训练记录和周计划：档案与计划保存在个人档案里，训练记录写入情节记忆（可在记忆卡查看或删除），不对外发送任何内容。

| name | 一句话用途 | 风险等级 | 是否需要确认卡 |
| --- | --- | --- | --- |
| `sport.history` | 汇总本人一段时间内的训练次数、时长、距离与连续天数 | `read` | 否 |
| `sport.log_workout` | 记录一次已完成的训练 | `self_write` | 否 |
| `sport.plan_get` | 读取当前保存的训练计划 | `read` | 否 |
| `sport.plan_save` | 保存用户确认过的周训练计划 | `self_write` | 否 |
| `sport.profile_get` | 读取运动档案（目标、水平、每周天数、伤病、器械） | `read` | 否 |
| `sport.profile_update` | 合并保存运动档案字段 | `self_write` | 否 |

## 搭子

搭子工具只处理本人身体数据、饮食和状态；热量是估算，不做医疗诊断。沉睡与新一代由 `COMPANION_DORMANCY_DAYS`（默认 30）控制。

| name | 一句话用途 | 风险等级 | 是否需要确认卡 |
| --- | --- | --- | --- |
| `companion.log_meal` | 记录一次饮食及其热量 | `self_write` | 否 |
| `companion.meal_suggest` | 按剩余热量与目标给三个健康餐选项 | `read` | 否 |
| `companion.recap` | 汇总一段时间的运动、饮食与体重变化 | `read` | 否 |
| `companion.rest_timer` | 生成组间休息计时卡并给一条动作提示 | `read` | 否 |
| `companion.revive` | 唤醒沉睡的搭子，进入下一代 | `self_write` | 否 |
| `companion.set_body` | 保存身高、体重、体脂等身体数据并创建搭子 | `self_write` | 否 |
| `companion.set_style` | 修改搭子名字或社交风格 | `self_write` | 否 |
| `companion.status` | 查看搭子状态、今日热量差与体重方向 | `read` | 否 |
| `companion.weekly_report` | 生成本周搭子周报文本 | `read` | 否 |

## 记忆

下表的 `memory_facts_search` **仅当 `MEMORY_VECTOR_ENABLED` 开启（且用户命中 rollout）时才会出现在工具列表 / `list_tools`；开关关闭时行为与原先一致。**

| name | 一句话用途 | 风险等级 | 是否需要确认卡 |
| --- | --- | --- | --- |
| `memory_card` | 生成可查看和编辑的个人记忆卡片 | `read` | 否 |
| `memory_context` | 组合档案和相关事件供对话参考 | `read` | 否 |
| `memory_delete` | 删除本人指定的一条事件记忆 | `self_write` | 否 |
| `memory_delete_all` | 删除用户全部事件记录但保留档案 | `self_write` | 否 |
| `memory_episode_delete` | 删除记忆卡中的事件并刷新记忆版本 | `self_write` | 否 |
| `memory_episode_update` | 编辑记忆卡中的事件并刷新记忆版本 | `self_write` | 否 |
| `memory_fact_delete` | 通过记忆卡删除指定档案事实 | `self_write` | 否 |
| `memory_fact_update` | 通过记忆卡修改指定档案事实 | `self_write` | 否 |
| `memory_facts_search` | 查记忆：混合检索当前用户的有效事实，返回 confirmed 标记 | `read` | 否 |
| `memory_forget` | 经明确确认后清除用户档案和事件记忆 | `self_write` | 否 |
| `memory_get` | 通过事件编号读取一条记忆记录 | `read` | 否 |
| `memory_search` | 按关键词、标签和线程检索事件记忆 | `read` | 否 |
| `memory_update` | 更新本人指定事件的摘要内容 | `self_write` | 否 |
| `memory_write` | 追加用户的事件及摘要记忆 | `self_write` | 否 |

## 提醒和定时任务

创建、修改、删除工具生成待确认提案，用户在聊天确认卡中批准后才变更任务，因此工具本身的 `requires_confirm` 为 false。所有工具仅操作当前会话用户的任务；活跃任务上限为 10 个，超限创建或恢复返回中文错误。

| name | 一句话用途 | 风险等级 | 是否需要确认卡 |
| --- | --- | --- | --- |
| `proactive.deliver` | 向本人已绑定的渠道发送一条提醒 | `notify_self` | 否 |
| `schedule.create` | 生成创建本人定时任务的确认卡 | `self_write` | 否 |
| `schedule.list` | 查看本人定时任务及运行次数 | `read` | 否 |
| `schedule.update` | 生成修改本人定时任务的确认卡 | `self_write` | 否 |
| `schedule.delete` | 生成删除本人定时任务的确认卡 | `self_write` | 否 |
| `schedule.pause` | 冻结本人指定的定时任务 | `self_write` | 否 |
| `schedule.resume` | 在活跃任务上限内恢复本人任务 | `self_write` | 否 |
| `schedule.run_now` | 立即运行本人启用的定时任务 | `notify_self` | 否 |

## 其他

公开来源检索工具返回不可信来源资料；内容不能授权或触发其他工具，抓取受服务端策略约束。

| name | 一句话用途 | 风险等级 | 是否需要确认卡 |
| --- | --- | --- | --- |
| `web_fetch` | 读取公开网页正文和来源链接 | `read` | 否 |
| `web_search` | 搜索公开网页及近期信息 | `read` | 否 |
| `fetch_feed` | 读取 RSS 或批准的免登录 RSSHub 路由 | `read` | 否 |
| `get_hn` | 读取或搜索 Hacker News 热帖 | `read` | 否 |
| `search_social` | 经官方 X API 或搜索引擎查询 Reddit 讨论 | `read` | 否 |
| `summarize_turn` | 把一轮对话加入后台摘要任务队列 | `self_write` | 否 |
