# Skills（可选的领域指导包）

`app/agent/skills/` 是一个版本固定、本地加载的指导文本框架：每个包放在 `packages/<name>/<version>/`，含 `metadata.json`（`schema_version`、`name`、`version`、`description`、`intents`）和 `SKILL.md`（≤ 8 KB）。`registry.json` 列出启用的包与版本。

通用 Personal Agent **默认不带任何包**（`registry.json` 的 `skills` 为空）。需要给某类任务（`draft` 或 `chat`）附加写作规范、语气指南时：

1. 新建 `packages/<name>/<x.y.z>/metadata.json` 与 `SKILL.md`。
2. 在 `registry.json` 登记 `"<name>": "<x.y.z>"`。
3. 调用方用 `with_skill_context(system, "<name>")` 把正文拼到系统提示前面。

技能只提供文本指导：不授予工具、不改权限、不改会话状态；正文会登记进泄露护栏，不会被原样输出给用户。
