# 管理后台 Slice A

在进程环境中设置 `ADMIN_TOKEN` 或 `HARNESS_ADMIN_TOKEN`（至少 16 位，使用随机值；不要提交到仓库）。两者可同时使用，身份为 `legacy`、权限为 `*`。打开 `/admin` 或 `/admin/`，粘贴令牌登录。令牌仅保存在当前标签页的 sessionStorage，退出即清除；所有管理 API 请求通过 Bearer 发送，不使用用户会话。

也支持 `HARNESS_ADMIN_TOKENS` 的 `name:token:scope|scope` 格式和已有 `admin_tokens` 表。`python scripts/admin_tokens.py --help` 查看令牌管理命令。未配置有效管理员令牌时，后台及 `/api/admin/*` 返回 503；普通用户令牌无权访问管理 API。

| Scope | 用途 |
|---|---|
| `users:read` | 用户搜索、状态筛选、分页、只读详情、记忆查看 |
| `users:write` | 禁用 / 解禁 |
| `audit:read` | 访问审计及变更日志 |
| `traces:read` | 已有轨迹及反馈列表 |
| `feedback:write` | 已有反馈导出 |
| `prompts:read`, `prompts:write`, `prompts:publish` | 预留，下一期 |
| `routes:read`, `routes:write`, `routes:publish` | 预留，下一期 |
| `*` | 全部权限 |

`GET /api/admin/me` 可供任何有效管理令牌查询身份和权限。用户列表参数为 `q`、`disabled`、`limit`、`offset`。详情默认只显示记忆 SHA-256 / 字符数；`?reveal=1` 返回经过密钥清理的原文，并记录访问审计。界面显示原文前需要输入「确认」。档案正文不可编辑，记忆不可删除；用量暂未聚合。

在用户详情抽屉点击禁用 / 解禁，输入「确认」后提交。禁用对已有用户令牌和 Cookie 的下一次受保护业务请求立即生效（403），不取消已经开始的请求。状态与 `admin_change_log` 在同一事务中提交；审计或变更日志写入失败时返回 503。重复禁用 / 解禁不重复记录状态变更。已有反馈导出在外部副作用前持久化变更意图，外部导出失败可能保留意图日志。

SQLite 的 `init_db` 自动创建 `admin_user_flags` 和 `admin_change_log`。Postgres 升级时，先由 owner 使用 `DATABASE_URL` 执行 `python -m app.data_access.rls_migration up`，再启动新版应用；此命令可重复执行，会补建 `admin_user_flags`、`admin_change_log`、`admin_tokens` 和 `admin_audit` 四张表并配置权限。运行时通过 `SYSTEM_DATABASE_URL` 的独立 system 登录读取禁用标记及执行管理操作：`personal_agent_system` 可读写标记和令牌表，变更日志和访问审计仅可 SELECT / INSERT；三个自增 ID 序列授予 USAGE。`personal_agent_app` 和 PUBLIC 无权访问这四张表及其序列。`down` 撤销运行角色权限但保留表和数据。运行角色的 `init_db` 不执行 DDL；本轮未连接 Postgres 或外部服务。

本地合成数据冒烟：使用独立临时 DATA_DIR，配置临时随机管理员令牌和用户签名密钥，在未占用的本地端口启动应用。打开 `/admin`，验证登录 → 用户搜索 → 详情 → 记忆确认显示 → 禁用 → 用户 chat/profile 返回 403 → 解禁 → 审计页出现访问及两次状态变更。不要使用生产数据、端口 8788 或线上服务。

本轮只运行指定目标测试；全量 CI 仍须在适合运行全套测试的环境中完成。
