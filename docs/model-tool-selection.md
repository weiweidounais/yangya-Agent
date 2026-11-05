# 模型选工具

`app/agent/tool_selection.py`：有模型 key 时，每条用户消息先让 FAST 模型从注册表里挑**一个**工具（或 `null` 表示闲聊）。选择只是计划，不是授权——真正执行仍走注册表的校验、确认门禁和审计。

## 可选集合（`TOOL_ROUTES`）

| 工具 | 路由 |
|------|------|
| `draft_text` | `draft`：进入草稿 → 确认卡流程 |
| `schedule.create / list / update / delete / pause / resume / run_now` | 对话内定时任务流程（`app/agent/schedules/chat.py` 抽参数，缺信息就追问） |
| `memory_facts_search` | chat（向量记忆开启时） |
| `web_fetch / web_search / fetch_feed / get_hn / search_social` | chat，并把来源作为不可信数据附进上下文 |

不在集合里的工具（记忆写入、档案保存、主动推送等）模型不能选，只能由服务端工作流调用。

## 约束

- 置信度低于 0.8、返回格式不对、或网关兜底（mock / 解析失败）→ 视为不可用，回退离线规则。
- 模型不能提供 user_id、确认标志、凭证或替代执行函数；参数由工作流用可信输入填充。
- 离线（无 key）时用规则：确认 / 拒绝短命令、`looks_like_draft_request`，其余闲聊。
