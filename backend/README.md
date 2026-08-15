# Backend

当前后端实现 MCP 只读查询、跨系统订单全链路、RBAC、NL2SQL、结构化会话上下文和用户级长期记忆。
IM 消息接入直接复用 Hermes Gateway。

图表由 Hermes 桥接插件在内存中生成 PNG；飞书直接上传，企微经短时临时文件交给现有 AI Bot
WebSocket 上传。因此 Hermes 的独立 Python 环境也需要安装 Matplotlib：

```bash
/home/ctrau/.hermes/hermes-agent/venv/bin/pip install -r \
  /home/ctrau/suning-hermes-agent/.hermes/plugins/suning-rbac-bridge/requirements.txt
```

飞书应用需具备机器人能力、图片上传及发送消息权限；企微图表复用 Hermes 已配置的
`WECOM_BOT_ID`、`WECOM_SECRET` 和当前 AI Bot WebSocket，不需要企业应用的 Corp Secret 或
Agent ID。Hermes 的图片接口仅接受文件路径，因此移动版 PNG 会在发送期间写入临时文件并在
完成后立即删除。钉钉当前 Hermes 会话 Webhook 不支持本地图片上传，图表会自动降级为文字摘要。

## 安装与配置

```bash
uv sync
cp .env.example .env
```

`.env` 至少需要 MySQL、Redis 和身份桥接配置；使用模型能力时填写 DeepSeek，
启用长期记忆时再填写 DashScope 与 Milvus 配置。完整变量见 `.env.example`。

## MCP 服务

六个服务分别监听 8101～8106：

```bash
uv run python -m mcp_suning.order_server
uv run python -m mcp_suning.aftersale_server
uv run python -m mcp_suning.product_server
uv run python -m mcp_suning.logistics_server
uv run python -m mcp_suning.payment_server
uv run python -m mcp_suning.order_timeline_server
```

动态分析工具 `query_return_stats_nl2sql` 和 `query_aftersale_nl2sql` 共用
`nl2sql.runtime.nl2sql_pipeline`。订单搜索/详情、售后流程、商品、物流和退款状态等明确业务 API
继续使用固定参数化 SQL。

## MCP 调用链路观测

桥接插件会在每个 Agent 回合创建 `trace_id`，自动记录 LLM 耗时与可用 Token 用量，以及所有私有
MCP 的服务地址、耗时、返回行数和失败原因。Span 批量导出至
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`（在 `.env.example` 中配置为 `http://127.0.0.1:4318/v1/traces`）；汇总指标以
`suning_agent_trace` JSON 写入 Hermes 日志。单条 Trace 墙钟耗时超过 30 秒或 Token 超过 50K 时写入
`suning_agent_trace_alert`，供现有日志告警链路转发至飞书。

## 跨系统订单全链路

`trace_order_timeline` 以订单 ID 为入口，在 10 秒整体超时内并发调用订单、售后、物流和支付
四个私有 MCP。它直接汇总成功负载、异常和超时结果，标准化不同时间格式，合并为
申请→审核→取件→入库→质检→退款时间线，并标记当前卡点和 SLA 状态。单路失败只会写入
`source_failures`，不会丢弃其他来源的已授权结果。完整实现契约见
[`../docs/跨系统订单全链路的实现.md`](../docs/跨系统订单全链路的实现.md)。

## 多轮会话上下文

Python 3.11+ 公共包 `suning-context-runtime` 按 `conv:<session_id>` 在 Redis 中保存当前话题、筛选槽位、
置信度、轮次和分层历史。私聊会先将飞书、企微或钉钉的已绑定账号解析为 `hermes_user_id`，并以
`im:active-session:<hermes_user_id>` 维持 30 分钟跨平台逻辑会话；群聊不会恢复私聊上下文。Hermes
插件通过 `pre_llm_call` / `post_llm_call` Hooks 自动提取、白名单校验、注入和保存上下文；超过 10 轮时
压缩早期历史并保留最近 3 轮原文。后端的
`suning_hermes_agent.conversation_context` 仅保留兼容导入。具体约束见
`docs/05-技术重难点/05c-多轮会话上下文继承.md`。

## 长期记忆

长期记忆由同一 Hermes 插件自动执行：回答前从 SQLite FTS5 与 Milvus 召回、融合并注入
Top3，回答后把结构化提取任务提交到单线程后台执行。SQLite 文件默认位于
`~/.hermes/state/suning_business_memory.db`；Milvus 可用以下命令启动：

```bash
cd infra/milvus
docker compose up -d
```

Milvus 或 Embedding 暂时不可用时会自动保留 FTS5 关键词召回。配置和验收规则见
`../docs/longterm_memory.md`。

## 售后知识库 RAG

政策、保修和维修类提问会在回答前从独立的 `knowledge_chunks` Milvus collection 召回原文片段，
再按关键词、品类、品牌、文档类型和生效日期重排 Top3。首次部署或更新现有 mock 文档后执行：

```bash
uv run python scripts/ingest_knowledge.py --input mock-files
```

Hermes 会注入带 `[知识来源N]` 的原文，回答必须据此引用。配置、排序规则和飞书验证方式见
[`../docs/售后知识库RAG精准检索的实现.md`](../docs/售后知识库RAG精准检索的实现.md)。

从旧工具名升级已有数据库时执行：

```bash
mysql --default-character-set=utf8mb4 -u <user> -p <database> < infra/mysql/migrations/001_rename_return_stats_nl2sql.sql
mysql --default-character-set=utf8mb4 -u <user> -p <database> < infra/mysql/migrations/002_repair_mock_category_names.sql
mysql --default-character-set=utf8mb4 -u <user> -p <database> < infra/mysql/migrations/003_repair_mock_business_labels.sql
mysql --default-character-set=utf8mb4 -u <user> -p <database> < infra/mysql/migrations/004_register_order_timeline.sql
```

随后重启 `mcp_suning.aftersale_server`，并刷新 Hermes 的 MCP Tool 列表。

## 测试

```bash
uv run pytest
```
