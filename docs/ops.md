# 运维：备份、恢复、导出与公网加固

这份文档只讲「怎么安全地长期用」。功能与产品说明见 [companion.md](companion.md) 和 [sports-agent.md](sports-agent.md)。

## 1. 备份（SQLite）

```bash
cd /Volumes/飞刀/Personal-Agent-main
bash scripts/backup_data.sh
```

- 用 SQLite 官方 `.backup` 在线备份，不锁库、不影响正在运行的服务。
- 默认写到 `data/backups/`，保留最近 14 份（`BACKUP_KEEP` 可改）。
- 备份是 `yangya-data-YYYYmmdd-HHMMSS.tar.gz`，里面是各个 `.db` 和 `.harness_id_key`。
- **备份含 `.harness_id_key`，按敏感数据保管**：不要提交到 Git、不要放公开网盘。

自定义位置：

```bash
DATA_DIR=/srv/yangya/data BACKUP_DIR=/srv/yangya/backups BACKUP_KEEP=30 \
  bash scripts/backup_data.sh
```

每天自动备份（macOS launchd 或 Linux cron 二选一）：

```bash
# Linux cron：每天 03:17
17 3 * * * cd /srv/yangya/app && DATA_DIR=/srv/yangya/data bash scripts/backup_data.sh >>/var/log/yangya-backup.log 2>&1
```

```xml
<!-- macOS ~/Library/LaunchAgents/com.yangya.backup.plist，加载：launchctl load ... -->
<key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>17</integer></dict>
<key>ProgramArguments</key><array>
  <string>/bin/bash</string><string>/Users/you/yangya/scripts/backup_data.sh</string>
</array>
```

## 2. 恢复

1. 停服务（`lsof -ti tcp:8787 -sTCP:LISTEN | xargs kill`）。
2. 解包到临时目录，确认文件：`tar -xzf yangya-data-YYYYmmdd-HHMMSS.tar.gz -C /tmp/restore`。
3. 备份现在的 `data/`：`mv data data.before-restore`。
4. 新建 `data/`，把 `/tmp/restore/*.db` 和 `.harness_id_key` 放进去，保持权限 `chmod 600`。
5. 启动服务，打开 `/health` 和搭子面板，确认档案、训练、饮食都在。
6. 确认无误后再删 `data.before-restore`。

注意：`.harness_id_key` 决定轨迹里的用户假名；换掉它不会丢业务数据，但旧轨迹的假名会对不上。

## 3. 导出个人数据

登录状态下面板里有「导出我的数据」，或直接请求：

```bash
curl -b "ja_session=<你的会话Cookie>" \
  "http://127.0.0.1:8787/api/export" -o yangya-export.json
```

返回 JSON：档案、记忆卡、训练、饮食、运动档案与计划、搭子信息、定时任务、已保存密钥的**标签**（不含值）。
不包含：密码/手机号等安全输入区的值、他人数据、任何密钥。

## 4. 公网加固开关

在 `.env` 里设置（说明见 `.env.example`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `API_RATE_LIMIT_ENABLED` | `0`（代码）/`1`（示例） | 每个 IP 与每个登录态令牌的分钟级限流；进程内计数，多实例按实例算 |
| `API_RATE_LIMIT_PER_MINUTE` | `300` | 每 IP 每分钟 |
| `API_RATE_LIMIT_USER_PER_MINUTE` | `120` | 每个令牌每分钟 |
| `API_TRUSTED_PROXY_HEADER` | 空 | 只有前置代理会覆盖该头时才填（如 `x-envoy-external-address`），否则不要信任转发头 |
| `CORS_ALLOW_ORIGINS` | 空 = 同源 | 空最安全；跨域前端才填逗号分隔白名单，`*` 仅限本地调试 |
| `ENABLE_DOCS` | `1` | 公网部署建议 `0`，关闭 `/docs`、`/redoc`、`/openapi.json` |

安全响应头（`X-Content-Type-Options`、`Referrer-Policy`、`X-Frame-Options`、CSP、
`Permissions-Policy`，HTTPS 下加 HSTS）默认开启，不需要配置。

## 5. 上线前清单

- [ ] `AUTH_SECRET` 为随机 32 字节，HTTPS 终止在反代/平台。
- [ ] `API_RATE_LIMIT_ENABLED=1`。
- [ ] `CORS_ALLOW_ORIGINS` 留空（同源）或只填你的域名。
- [ ] `ENABLE_DOCS=0`。
- [ ] `DATA_DIR` 在持久盘上；每天备份并验证能恢复。
- [ ] `SECRETS_FERNET_KEY` 和 `HARNESS_ID_HMAC_KEY` 固定且不进仓库。
- [ ] 飞书/微信按需开启，`PUBLIC_BASE_URL` 为 HTTPS。
- [ ] 监控 `/health`；失败时先看磁盘和 `.db-wal` 权限。
- [ ] 定期跑一次 `python -m pytest -q` 和 `tests/golden/run_golden.py --mode mock`。

## 6. 常见问题

**备份文件多大？** 空库约 128K，随对话/轨迹增长；轨迹库单独在 `audit.db`。

**可以只备份某一个库吗？** 不建议：档案、记忆、轨迹、定时任务跨库关联，整套备份最稳。

**限流 429 了怎么办？** 等 `Retry-After` 秒数；如果是正常用户被误伤，调高 `API_RATE_LIMIT_*` 或检查是否有脚本在刷。

**导出会包含图片吗？** 不会。食物照片只在识别时存在内存里，不落盘、不导出。
