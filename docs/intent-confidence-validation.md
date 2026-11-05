# 意图置信度评分（hybrid 模式）

`INTENT_SCORING_MODE` 默认 `rules`，不读取任何示例向量。显式设为 `hybrid` 时，规则没有命中的文本会走
`POST https://api.siliconflow.cn/v1/embeddings`（`Pro/BAAI/bge-m3`）做 kNN 评分。评分只作为
危机覆盖与澄清的安全网，不会把消息路由到任务流程；危机规则始终优先。

凭据只从进程环境变量 `SILICONFLOW_API_KEY` 读取，不需要本地编码器或 Torch。

## 示例向量缓存

- 示例句在 `intents/exemplars.yaml`（`confirm / chat / unresolved / crisis` 各 10 句，DRAFT 状态，需产品评审）。
- 向量缓存 `intents/exemplar-vectors.json` **不随仓库分发**。启用 hybrid 前运行
  `python scripts/embed_intents.py` 生成；缓存与模型、端点、示例版本和文件 SHA-256 绑定。
- 启动只读缓存：缓存缺失、过期或无效时评分初始化失败并 fail closed，路由退回规则结果。
- 示例句一旦修改，必须重新生成缓存。

## 评测集

`intents/eval.jsonl` 是通用评测集；`scripts/calibrate_intents.py` 可在本地重算阈值报告，默认输出到
`intents/calibration-report.json`（同样不随仓库分发）。

## 测试

`tests/unit/test_intent_scoring.py` 用 mock HTTP 覆盖分批与顺序、缺失凭据、无效向量、提供方失败、
缓存一致性与缓存启动；CI 显式断言未安装 Torch 与 sentence-transformers。
