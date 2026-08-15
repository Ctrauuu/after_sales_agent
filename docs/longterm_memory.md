# 长期记忆结构化沉淀与语义召回实施方案

## 1. 文档目的

本文档用于指导并接管苏宁 Hermes Agent 的长期记忆实现。方案以
[`05e-长期记忆结构化沉淀与召回.md`](./05-技术重难点/05e-长期记忆结构化沉淀与召回.md)
为设计依据，第一版优先保证结构简单、链路完整、容易阅读和验证。

本文档同时作为实现与验收基线。第一版已于 2026-08-11 完成，代码、容器、运行时配置
和验证结果见本文末尾的“当前环境检查结果”。

## 2. 实现原则

第一版严格保留下列设计：

- 使用 SQLite FTS5 做关键词召回。
- 使用 Milvus 做向量语义召回。
- 使用 LLM 判断一轮对话是否值得沉淀，并提取结构化记忆。
- 关键词分数和语义分数各占 `0.5`。
- 使用 30 天半衰期进行时间衰减。
- 合并排序后向 Agent 注入 Top3 记忆。
- 相同主题、实体和过滤条件的新结论更新原记录，并增加版本号。
- 通过 Hermes Skill 描述记忆提取规则，通过插件 Hook 保证自动执行。

第一版不实现以下能力：

- 记忆删除。
- 完整历史版本表。
- 消息队列和持久化异步任务。
- 多级重试、复杂熔断和补偿任务。
- 组织级共享记忆和复杂权限快照。
- 自动冲突分析及多版本结论合并。

## 3. 总体数据流

### 3.1 回答前召回

```text
用户问题
  -> 读取 Redis 中的当前结构化槽位
  -> 拼接召回查询文本
  -> FTS5 关键词召回
  -> Milvus 语义召回
  -> 分数归一化并按 0.5 / 0.5 融合
  -> 应用 30 天半衰期
  -> 选择 Top3
  -> 通过 pre_llm_call 注入本轮上下文
  -> Agent 生成回答
```

### 3.2 回答后沉淀

```text
Agent 完成回答
  -> post_llm_call 获取本轮对话和最新结构化槽位
  -> 提交单线程后台任务
  -> DeepSeek 判断 should_record
  -> 提取 MemoryRecord
  -> 按 memory_key 新增或更新 SQLite 记录
  -> FTS5 索引同步
  -> DashScope 生成 embedding
  -> Milvus 新增或更新向量
```

`post_llm_call` 不等待 LLM 提取、Embedding 和索引写入结束，避免延长用户收到
飞书回答的时间。第一版使用进程内 `ThreadPoolExecutor(max_workers=1)` 实现异步写入。
进程退出时尚未执行的任务允许丢失。

## 4. 数据模型

### 4.1 MemoryExtraction

LLM 的结构化输出：

```python
class MemoryExtraction(BaseModel):
    should_record: bool
    topic: str | None = None
    entities: dict[str, object] = {}
    filters: dict[str, object] = {}
    conclusion: str | None = None
    evidence: dict[str, object] = {}
    confidence: float = 0.0
```

字段含义：

- `should_record`：是否具有跨会话复用价值。
- `topic`：长期记忆主题。
- `entities`：商品、区域、原因、订单等业务实体。
- `filters`：产生结论时使用的筛选条件。
- `conclusion`：可复用的业务结论。
- `evidence`：支撑结论的关键数据或事实。
- `confidence`：LLM 对提取结果的置信度，范围为 `0～1`。

### 4.2 MemoryRecord

实际持久化的数据：

```python
class MemoryRecord(BaseModel):
    id: str
    memory_key: str
    user_id: str
    topic: str
    entities: dict[str, object]
    filters: dict[str, object]
    conclusion: str
    evidence: dict[str, object]
    confidence: float
    created_at: datetime
    updated_at: datetime
    version: int = 1
```

`user_id` 用于不同飞书用户之间的记忆隔离。`memory_key` 根据下列内容生成：

```text
user_id + topic + 标准化后的 entities + 标准化后的 filters
```

### 4.3 RetrievalResult

```python
class RetrievalResult(BaseModel):
    record: MemoryRecord
    score: float
    source: Literal["fts5", "vector", "both"]
```

## 5. 可沉淀记忆的判断规则

第一版只允许下列主题：

| 长期记忆主题 | 来源场景 |
| --- | --- |
| `return_analysis` | 退单、售后原因和趋势分析 |
| `order_trace` | 订单链路、状态异常和处理结论 |

当前结构化槽位主题可按以下方式映射：

| 槽位主题 | 长期记忆主题 |
| --- | --- |
| `return_case` | `return_analysis` |
| `order_query` | `order_trace` |

只有同时满足以下条件才写入：

1. LLM 返回 `should_record=true`。
2. `topic` 在白名单中。
3. `confidence >= 0.7`。
4. `conclusion` 非空。

提取提示词要求 LLM 跳过：

- 单次查询得到的临时数字。
- 只对当前会话有效的信息。
- 没有形成分析结论的普通问答。
- 无法确定主题或缺少基本依据的内容。

适合记录的内容包括：

- 多次查询仍可复用的趋势性结论。
- 退单或售后问题的主要原因及支撑数据。
- 订单链路中已确认的问题和处理结论。

## 6. LLM 与 Embedding

### 6.1 结构化提取模型

沿用项目已有方式，通过 `suning_context_runtime.create_model` 创建 DeepSeek 模型，
不新增基于网站接口的调用方式。使用 LangChain 结构化输出生成
`MemoryExtraction`。

需要的配置：

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
LONG_MEMORY_LLM_TIMEOUT_SECONDS=20
```

其中 `DEEPSEEK_BASE_URL` 和 `DEEPSEEK_MODEL` 可继续使用共享运行时默认值。

### 6.2 向量模型

按照原设计使用 DashScope `text-embedding-v3` 生成向量。写入 Milvus 前必须先确定
实际返回的向量维度，并使用相同维度创建 collection。

需要的配置：

```dotenv
DASHSCOPE_API_KEY=
MEMORY_EMBEDDING_MODEL=text-embedding-v3
```

## 7. SQLite 与 FTS5

### 7.1 数据库位置

长期记忆使用独立 SQLite 文件，不修改 Hermes 内部数据库结构：

```text
~/.hermes/state/suning_business_memory.db
```

启动时需要确保父目录存在且 Hermes Gateway 进程具有读写权限。

### 7.2 基础表

`memories` 保存完整结构化记录，至少包含：

```text
id, memory_key, user_id, topic, entities_json, filters_json,
conclusion, evidence_json, confidence, created_at, updated_at, version
```

约束：

- `id` 为主键。
- `memory_key` 唯一。
- 时间统一保存为 UTC ISO 8601 字符串。

### 7.3 FTS5 索引

创建 `memories_fts` 虚拟表，索引以下字段：

- `topic`
- `entities_text`
- `conclusion`

通过 SQLite Trigger 保持基础表和 FTS5 表的插入、更新同步。虽然第一版没有外部删除
入口，仍可保留删除同步 Trigger，防止维护数据库时留下脏索引。

召回时先按 FTS5 匹配候选 ID，再从 `memories` 基础表按 `user_id` 过滤和加载完整记录。

## 8. Milvus

### 8.1 Collection

collection 名称：

```text
memory_vectors
```

至少包含以下字段：

```text
id, user_id, topic, vector, updated_at
```

- `id` 与 SQLite 中的记忆 ID 相同。
- `vector` 维度必须和 `text-embedding-v3` 实际输出一致。
- 新版本结论继续使用相同 `id` 更新向量。
- 检索时必须使用 `user_id` 过滤表达式。

### 8.2 容器部署

当前 MySQL/Redis Compose 不包含 Milvus。实施前需要增加 Milvus Standalone 容器，
可以将官方 Standalone 所需的 Milvus、etcd 和 MinIO 服务放入独立 Compose 文件，也可以
合并到现有 `backend/infra/mysql/compose.yaml`。为了职责清楚，建议使用：

```text
backend/infra/milvus/compose.yaml
```

本机开发只需向 `127.0.0.1` 暴露 Milvus SDK 端口，etcd 和 MinIO 可以仅在 Compose
内部网络中访问。镜像版本在实现时统一固定，Milvus 服务端与 `pymilvus` 客户端版本需要
兼容。

需要的运行时配置：

```dotenv
MILVUS_URI=http://127.0.0.1:19530
MILVUS_COLLECTION=memory_vectors
```

## 9. 写入实现

### 9.1 post_llm_call 输入

长期记忆 Hook 使用 Hermes 提供的：

- `session_id`
- `sender_id`
- `user_message`
- `assistant_response`
- `conversation_history`

同时从现有 `ContextManager` 读取本轮最新的 Redis 结构化槽位。

### 9.2 写入步骤

1. Hook 复制本轮所需数据并提交后台线程。
2. DeepSeek 返回 `MemoryExtraction`。
3. 执行主题白名单、置信度和空结论校验。
4. 生成 `memory_key`。
5. 如果 `memory_key` 不存在，写入新记录并设置 `version=1`。
6. 如果已存在，更新结论、证据、置信度和时间，并执行 `version += 1`。
7. SQLite Trigger 同步 FTS5 索引。
8. 将主题、实体、过滤条件和结论拼成 Embedding 文本。
9. 生成向量并新增或更新 Milvus 记录。

任一步骤失败时记录日志，不影响已经生成的用户回答。第一版不自动重试。

## 10. 召回实现

### 10.1 查询文本

回答前将以下内容拼成召回查询文本：

- 当前用户消息。
- Redis 中的当前 topic。
- 当前 entities。
- 当前 filters。

### 10.2 双通道候选

- FTS5 取 `top_k * 2`，默认 6 条。
- Milvus 取 `top_k * 2`，默认 6 条。
- 两路结果按照记忆 ID 去重。

FTS5 的 BM25 值通常越小越相关，Milvus 原始距离的方向取决于使用的 metric，因此不能
直接相加。实现时先将两个通道的分数分别转换为 `0～1`，并统一为“越大越相关”。

融合权重保持原设计：

```text
combined_score = 0.5 * keyword_score + 0.5 * semantic_score
```

如果一条记忆只被一个通道命中，另一个通道的分数按 `0` 计算。

### 10.3 时间衰减

使用 30 天半衰期：

```text
decay = exp(-ln(2) * age_days / 30)
final_score = combined_score * decay
```

按照 `final_score` 降序排列，返回 Top3。

### 10.4 注入格式

长期记忆通过 `pre_llm_call` 返回的 `context` 注入。Hermes 当前 Hook 接口不会直接修改
真正的 System Prompt，因此这里使用 Hermes 支持的上下文注入能力完成同等目标。

```text
以下是该用户过去形成的业务分析结论，仅作为历史参考。
如果历史结论与本次实时查询结果冲突，应以实时查询结果为准。

1. 主题：return_analysis
   条件：...
   结论：...
   证据：...
   更新时间：...
```

召回失败时返回空上下文，不中断 Agent 回答。Milvus 不可用时允许只使用 FTS5 结果。

## 11. Skill 与 Hooks

### 11.1 memory-extractor Skill

项目内保存可版本管理的 Skill：

```text
.hermes/skills/memory-extractor/SKILL.md
```

部署时确保 Hermes 能从 `~/.hermes/skills/memory-extractor/SKILL.md` 发现它。Skill 只描述：

- 适合记录和不适合记录的场景。
- 主题白名单。
- `MemoryExtraction` 输出结构。
- 置信度含义。
- 手工调用和调试方式。

Hermes Skill 不能可靠保证每轮回答后自动执行，因此自动沉淀由
`post_llm_call` Hook 调用同一套 `MemoryExtractor` 完成；Skill 保留为模型规则说明和手工
调试入口。

### 11.2 Hook 注册顺序

```text
pre_llm_call:
1. 现有 ConversationHooks.pre_llm_call
2. LongTermMemoryHooks.pre_llm_call

post_llm_call:
1. 现有 ConversationHooks.post_llm_call
2. LongTermMemoryHooks.post_llm_call
```

这样长期记忆写入可以读取本轮刚更新的结构化槽位，长期记忆召回也可以使用当前槽位增强
查询。

## 12. 代码组织与推荐阅读顺序

建议文件结构：

```text
packages/suning-context-runtime/
└── src/suning_context_runtime/
    ├── long_memory_models.py
    ├── long_memory_store.py
    ├── long_memory_extractor.py
    ├── long_memory_retriever.py
    └── long_memory_pipeline.py

.hermes/plugins/suning-rbac-bridge/
└── memory_hooks.py

.hermes/skills/memory-extractor/
└── SKILL.md
```

接管时建议按以下顺序阅读：

1. `memory_hooks.py`：确认什么时候召回、什么时候异步写入。
2. `long_memory_pipeline.py`：查看整体编排。
3. `long_memory_extractor.py`：查看 DeepSeek 结构化提取。
4. `long_memory_retriever.py`：查看双通道召回、融合和衰减。
5. `long_memory_store.py`：查看 SQLite、FTS5 和 Milvus 操作。
6. `long_memory_models.py`：查看所有输入输出模型。
7. `SKILL.md`：查看模型判断规则和手工调试说明。

所有新增或修改的 Python 函数必须按照项目 `AGENTS.md` 的要求，在函数体第一行提供包含
“输入”“输出”“功能”的 docstring。

## 13. 前置条件

### 13.1 基础运行环境

- Python 3.11 或以上；当前共享包要求 `>=3.11`，后端项目要求 `>=3.12`。
- `uv` 可以安装共享包依赖并运行测试。
- Python 自带的 SQLite 启用了 FTS5。
- Docker Engine 和 Docker Compose 可用。
- Hermes Gateway 可以加载当前项目插件和共享运行时包。

### 13.2 外部服务

- MySQL 正常运行，用于现有结构化槽位白名单。
- Redis 正常运行，用于现有短期结构化槽位。
- 新建并启动 Milvus Standalone 容器及其依赖服务。
- Milvus 的数据目录或 Docker Volume 需要持久化。

### 13.3 模型凭据

- DeepSeek API Key，用于结构化记忆判断和提取。
- DashScope API Key，用于 `text-embedding-v3`。
- 部署机器能够访问对应模型 API。

### 13.4 Python 依赖

在 `suning-context-runtime` 中增加并锁定：

- `pymilvus`：连接 Milvus、创建 collection、写入和检索向量。
- DashScope Python SDK或项目选定的兼容客户端：调用 `text-embedding-v3`。

现有 LangChain、DeepSeek、Pydantic、Redis、SQLAlchemy 依赖继续复用。

### 13.5 目录和权限

- 创建 `~/.hermes/state/`，Hermes Gateway 用户具有读写权限。
- 创建或部署 `~/.hermes/skills/memory-extractor/`。
- 确保 `suning-context-runtime` 已安装到 Hermes Gateway 使用的 Python 环境。
- SQLite 数据库和 Milvus Volume 不能使用临时目录。

### 13.6 环境变量

实现前至少准备：

```dotenv
DEEPSEEK_API_KEY=
DASHSCOPE_API_KEY=
MILVUS_URI=http://127.0.0.1:19530
MILVUS_COLLECTION=memory_vectors
MILVUS_TIMEOUT_SECONDS=5
MEMORY_DB_PATH=~/.hermes/state/suning_business_memory.db
MEMORY_EMBEDDING_MODEL=text-embedding-v3
MEMORY_EMBEDDING_TIMEOUT_SECONDS=10
LONG_MEMORY_MIN_CONFIDENCE=0.7
LONG_MEMORY_TOP_K=3
LONG_MEMORY_HALF_LIFE_DAYS=30
LONG_MEMORY_LLM_TIMEOUT_SECONDS=20
```

现有 `REDIS_URL`、`MYSQL_*` 和 `DEEPSEEK_API_KEY` 可以继续使用。路径配置在代码中需要先
调用 `expanduser()`，不能假设运行库会自动展开 `~`。

## 14. 实施顺序

1. 新建 Milvus Compose 并确认服务健康。
2. 配置 DashScope、Milvus 和长期记忆环境变量。
3. 给共享运行时增加 Milvus 与 Embedding 依赖。
4. 实现数据模型和 SQLite/FTS5 初始化。
5. 实现 Milvus collection 初始化及向量写入。
6. 实现 DeepSeek 结构化记忆提取。
7. 实现版本更新和双存储写入管道。
8. 实现 FTS5、Milvus 双通道召回、融合和时间衰减。
9. 实现 `LongTermMemoryHooks` 并按顺序注册。
10. 新建并部署 `memory-extractor` Skill。
11. 增加单元测试、集成测试和飞书端测试案例。
12. 更新 `~/.hermes/.env`，重启 Hermes Gateway 并执行端到端验收。

## 15. 验收标准

至少完成以下验证：

1. 可复用的退单分析结论写入 SQLite、FTS5 和 Milvus。
2. 一次性临时数字返回 `should_record=false`，不产生记忆。
3. 新会话使用关键词可以从 FTS5 召回旧结论。
4. 新会话更换说法后可以从 Milvus 召回语义相近结论。
5. 同一用户、主题、实体和条件的新结论使 `version` 增加。
6. 30 天前的记忆分数约衰减为原分数的一半。
7. 不同飞书用户不能互相召回记忆。
8. Milvus 不可用时 FTS5 仍能返回结果。
9. 提取或写入失败不影响当前飞书回答。
10. 注入 Agent 的长期记忆不超过 Top3。
11. 执行项目完整测试：

```bash
cd backend
uv run pytest
```

## 16. 当前环境检查结果

检查日期：2026-08-11。

| 项目 | 当前状态 |
| --- | --- |
| Python | 已安装，当前为 3.14.4 |
| uv | 已安装，当前为 0.12.1 |
| Docker | 已安装 |
| Docker Compose | 已安装 |
| SQLite FTS5 | 可用 |
| MySQL | `suning-mysql` 正常运行且健康 |
| Redis | `suning-redis` 正常运行且健康 |
| DeepSeek 配置 | `~/.hermes/.env` 已存在 `DEEPSEEK_API_KEY` |
| Milvus | Standalone、etcd、MinIO 均正常运行；`memory_vectors` 已按实际 1024 维 Embedding 创建 |
| DashScope 配置 | `~/.hermes/.env` 已存在 `DASHSCOPE_API_KEY`，真实 Embedding 调用成功 |
| 长期记忆配置 | `~/.hermes/.env` 已配置 `MILVUS_*`、`MEMORY_*` 和 `LONG_MEMORY_*` |
| Python 长期记忆依赖 | 共享包已声明 `pymilvus==3.0.1` 与 `dashscope>=1.26.6`，Gateway 环境已安装 |
| SQLite/FTS5 | 生产路径数据库、基础表、trigram FTS5 表及三类同步 Trigger 已初始化 |
| Hermes 插件 | `suning-rbac-bridge` 0.3.0 已启用，短期 Hook 在前、长期 Hook 在后 |
| memory-extractor Skill | 已链接到 `~/.hermes/skills/memory-extractor`，Hermes 显示为 enabled |
| 自动化测试 | `cd backend && uv run pytest`：119 项通过 |
| 外部服务冒烟 | 真实 DeepSeek → SQLite/FTS5 → DashScope 1024 维向量 → Milvus → 双通道召回全链路通过 |

实现额外为 DashScope 和 Milvus 设置了有界超时；中文主链路使用 trigram FTS5，针对不足
三个字符的品类等短实体使用受用户过滤的 SQLite 子串降级，避免“空调、冰箱”等关键词漏召回。
