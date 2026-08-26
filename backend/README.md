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
uv run python -m mcp_suning.servers.order
uv run python -m mcp_suning.servers.aftersale
uv run python -m mcp_suning.servers.product
uv run python -m mcp_suning.servers.logistics
uv run python -m mcp_suning.servers.payment
uv run python -m mcp_suning.servers.timeline
```

生产机使用一个模板服务和一个 target 统一管理六个实例：

```bash
sudo install -m 0644 infra/systemd/suning-mcp@.service /etc/systemd/system/
sudo install -m 0644 infra/systemd/suning-mcp.target /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now suning-mcp.target
```

统一启停和查看状态：

```bash
sudo systemctl restart suning-mcp.target
sudo systemctl stop suning-mcp.target
systemctl status 'suning-mcp@*.service'
journalctl -u suning-mcp@order.service -f
```

模板按当前部署位置 `/home/ctrau/suning-hermes-agent` 和运行用户 `ctrau` 配置，并从
`backend/.env` 读取所有 MCP 共用环境变量。部署到其他位置或用户时，只需修改模板中的三处路径及
`User`、`Group`，不复制单独的 service 文件。

动态分析工具 `query_return_stats_nl2sql` 和 `query_aftersale_nl2sql` 共用
`nl2sql.runtime.nl2sql_pipeline`。`query_sku_return_rate` 按订单创建时间以固定参数化 SQL
计算 SKU 退单订单率（退单订单数 ÷ 包含该 SKU 的订单数），不调用 NL2SQL。订单搜索/详情、售后流程、
商品、物流和退款状态等明确业务 API 也继续使用固定参数化 SQL。

## MCP 调用链路观测

桥接插件会在每个 Agent 回合创建 `trace_id`，自动记录 LLM 耗时与可用 Token 用量，以及所有私有
MCP 的服务地址、耗时、返回行数和失败原因。`TOOL_SPECS` 中的 MCP Tool 统一经过插件注册时创建的
`MCPCallManager`：仅对显式允许重试的超时执行最多 3 次重试（1/2/4 秒退避并附加 0～200ms
抖动），且每次真实请求都会重新签发 attestation/JTI。熔断状态按稳定的 `server_id` 存入现有
Redis；60 秒内 5 次可计数失败会熔断 30 秒，半开时只允许一个跨进程探测，Redis 不可用时调用链
fail-open。L1/L2 降级保留为结构化 `tool_result` 并进入复杂分析摘要，L3 返回安全的
`tool_error`；合法空结果和时间线的 `partial/source_failures` 不计入服务故障。

MCP Span 额外记录 `retry_count`、`failure_type`、`circuit_state`、`degrade_level`、最终成功状态和
是否降级。Span 批量导出至
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
mysql --default-character-set=utf8mb4 -u <user> -p <database> < infra/mysql/migrations/007_complete_mcp_registry_metadata.sql
mysql --default-character-set=utf8mb4 -u <user> -p <database> < infra/mysql/migrations/008_add_sku_return_rate_tool.sql
```

随后重启 `mcp_suning.servers.aftersale`，并刷新 Hermes 的 MCP Tool 列表。

## 管理后台 API

FastAPI 管理服务直接复用现有数据源：用户、角色、IM 绑定和 MCP 注册表读写 MySQL；对话审计和仪表盘只读
Hermes `state.db` 与结构化日志；Skill 管理原子更新 `.hermes/skills/evolved`。启动：

```bash
uv run uvicorn suning_hermes_agent.admin_api:app --host 127.0.0.1 --port 8080
```

实现位于 `src/suning_hermes_agent/admin/`：`app.py` 负责应用组装，`common.py` 和 `runtime.py`
承载跨域基础设施，其余模块按用户、MCP、会话、Skill、看板业务域组织；`admin_api.py` 仅保留兼容启动入口。

本机开发可由 Vite 代理直接访问。远程部署必须配置 `SUNING_ADMIN_API_TOKEN`，并由同源反向代理注入
`Authorization: Bearer <token>`。工具开关同时写入 MySQL 和共享 Redis，Agent 下一次调用即时生效；
Skill 启停写回现有 Skill 文件，下一轮路由即时生效。

## 测试

```bash
uv run pytest
```
