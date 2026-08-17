# 05m · MCP 调用失败重试与降级 Spec

- 状态：Draft
- 范围：Hermes 插件中的苏宁业务 MCP 调用边界
- 需求来源：`docs/05-技术重难点/05m-MCP调用失败重试与降级.md`
- 实现约束：不修改 Hermes 源码，不引入新的第三方依赖，不构建通用 resilience framework

## 1. Goal

在当前仓库已有的 Hermes 业务工具桥接边界集中实现 MCP 调用可用性策略：

1. 仅对允许重试的只读查询工具执行有上限的指数退避重试。
2. 按 MCP Server 在 Redis 中共享连续失败、打开期和半开探测状态。
3. 调用失败时返回结构化、可观测、可被 Agent 正确表达的降级结果，不因单个非关键工具失败中断整个分析。
4. 保留现有 RBAC、一次性身份凭证、OTel Trace、时间线部分结果和 Hermes Tool Registry 返回约定。
5. 使重试、熔断和降级尽量只出现在实际 MCP Client 调用边界，不散落到各 MCP Tool 实现。

## 2. Non-goals

1. 不创建可支持 HTTP、LLM、数据库等任意依赖的通用 resilience 框架。
2. 不修改 `/home/ctrau/.hermes/hermes-agent` 或其他 Hermes 源码；当前仓库没有用于本需求的 Hermes patch 机制。
3. 不在 `backend/mcp_suning/*_server.py` 的 `@mcp.tool` 函数内增加重试、熔断或 Agent 降级逻辑。
4. 不改变 MCP Server 已有的 RBAC、数据脱敏、NL2SQL 或数据库访问逻辑。
5. 不对非 MCP 能力（LLM、Milvus、图片上传、MySQL）应用本策略。
6. 不为尚未存在的写工具预先设计补偿事务或幂等键框架。新增写工具默认不重试，届时单独评审。
7. 首期不将插件侧 `MCPCallManager` 复制到 `backend/mcp_suning/timeline/gateway.py`。时间线 MCP 内部四路调用继续由现有整体超时和 `source_failures` 降级保护；只有在证实内部单源重试为必需时，才设计跨进程共享实现。

## 3. 当前 MCP 调用链

### 3.1 Agent 直接调用业务工具

```text
Hermes Agent
  -> suning_business toolset
  -> .hermes/plugins/suning-rbac-bridge/__init__.py:register
  -> .hermes/plugins/suning-rbac-bridge/bridge.py:make_handler
  -> bridge.py:invoke_business_tool
  -> current_identity + mint_attestation
  -> streamable_http_client
  -> ClientSession.initialize
  -> ClientSession.call_tool
  -> _successful_result / tool_error
  -> Hermes Agent
```

当前的统一入口是：

- `.hermes/plugins/suning-rbac-bridge/bridge.py:invoke_business_tool`
- 实际 `call_tool` 位于 `.hermes/plugins/suning-rbac-bridge/bridge.py`
- Tool 与 Endpoint 配置位于 `.hermes/plugins/suning-rbac-bridge/schemas.py:TOOL_SPECS`

当前网络、协议和解析异常被同一个 `except Exception` 转换为“苏宁业务服务暂时不可用”，没有重试和熔断。

### 3.2 订单时间线内部调用

```text
Agent -> bridge.py -> trace_order_timeline (8106)
  -> backend/mcp_suning/servers/timeline.py
  -> TimelineMCPGateway
  -> PrivateMCPCaller.call
  -> 并发 call_tool:
       mcp-order / get_order_detail
       mcp-aftersale / get_aftersale_workflow
       mcp-logistics / query_logistics
       mcp-payment / get_refund_status
  -> OrderTimelineTracker.trace
  -> _merge_timeline
  -> partial + source_failures + timeline
```

`backend/mcp_suning/timeline/tracker.py` 已实现 10 秒整体超时、单源异常隔离、慢任务取消和部分结果聚合。本 Spec 不把这些职责移入 Manager。

### 3.3 复杂分析聚合

`.hermes/plugins/suning-rbac-bridge/orchestration.py` 启动只允许 `suning_business` toolset 的子 Agent。子 Agent 的 MCP 调用仍回到 `bridge.py:invoke_business_tool`。`TaskOrchestrator.aggregate()` 聚合的是子 Agent 压缩摘要和状态，不是原始 MCP Result。

## 4. 新组件及各自职责

### 4.1 `MCPCallManager`

新文件：

`.hermes/plugins/suning-rbac-bridge/mcp_resilience.py`

负责：

1. 在真实 MCP 调用前查询 Server 熔断状态。
2. 对允许重试的超时执行有上限的指数退避。
3. 每次尝试调用由 `bridge.py` 提供的异步闭包，Manager 不知道用户身份、MCP Schema 或 Hermes Registry。
4. 将最终结果归一为 `MCPCallResult`。
5. 仅对代表 Server 健康度的失败更新熔断状态。
6. Redis 不可用时对熔断状态 fail-open，仍允许真实 MCP 调用，并记录告警。

Manager 不负责：

- 签发或验证身份凭证。
- 建造 Hermes `tool_result` / `tool_error`。
- 理解订单、退单或时间线业务 Schema。
- 聚合多个工具的业务数据。

### 4.2 `CircuitStore`

与 `MCPCallManager` 放在同一文件，不创建独立 package 或通用存储接口。

负责：

- 使用注入的现有同步 `redis.Redis` Client 读写熔断键。
- 利用 Redis `INCR`、TTL 和 `SET NX` 实现跨进程失败计数与单探测租约。
- 通过 `asyncio.to_thread()` 调用同步 Redis，避免在 MCP 异步调用中直接阻塞事件循环。
- 将 Redis 异常转换为“本次无熔断保护”，不把 Redis 故障伪装为 MCP Server 失败。

### 4.3 `FailureType` / `DegradeLevel` / `CircuitState` / `MCPCallResult`

与 Manager 放在同一模块。这些类型仅供苏宁 MCP 桥接使用，不暴露为通用 SDK。

### 4.4 现有文件的调整职责

| 路径 | 实现阶段职责 |
|---|---|
| `.hermes/plugins/suning-rbac-bridge/bridge.py` | 将“重新签发凭证 + 建连 + initialize + call_tool + 响应校验”封装成每次尝试的闭包；调用 Manager；将 `MCPCallResult` 转为现有 Hermes 返回格式。 |
| `.hermes/plugins/suning-rbac-bridge/schemas.py` | 在 `ToolSpec` 中声明稳定 `server_id`、`degrade_level`、`retry_on_timeout` 和空结果语义。 |
| `.hermes/plugins/suning-rbac-bridge/__init__.py` | 插件注册时创建一个 Manager，通过 `make_handler()` 注入所有 MCP 工具 Handler。 |
| `.hermes/plugins/suning-rbac-bridge/context_hooks.py` | 提取可复用的 Redis Client 构建函数，并允许 `build_conversation_runtime()` 复用已创建 Client，避免重复连接池。 |
| `.hermes/plugins/suning-rbac-bridge/observability.py` | 在现有 MCP Span 上增加尝试数、重试数、FailureType、CircuitState 和降级等级；不创建新观测客户端。 |
| `.hermes/plugins/suning-rbac-bridge/orchestration.py` | 仅完善子 Agent 指令，要求保留 L1/L2 降级提示；继续复用现有 DAG 和聚合报告。 |
| `backend/tests/test_mcp_resilience.py` | 新增 Manager、Redis 熔断状态和桥接转换的单元测试。 |
| `backend/tests/test_complex_analysis_orchestration.py` | 扩展降级提示在子 Agent 摘要和聚合报告中不丢失的回归测试。 |

## 5. FailureType 定义

| FailureType | 判定来源 | 重试 | 计入熔断 | 说明 |
|---|---|---:|---:|---|
| `TIMEOUT` | `TimeoutError`、`asyncio.TimeoutError`、`httpx.TimeoutException` 或调用超过单次上限 | 是 | 最终失败时计 1 次 | 唯一默认重试类型。 |
| `CONNECTION_REFUSED` | 建连拒绝、DNS/路由不可达、`httpx.ConnectError`、`ConnectionRefusedError` | 否 | 是 | 按需求直接失败，避免在明确不可达时累积延迟。 |
| `PERMISSION_DENIED` | 明确的 401/403、`PermissionError` 或可机器识别的鉴权拒绝 | 否 | 否 | 身份或权限问题不代表 Server 不健康。 |
| `EMPTY_RESULT` | 调用成功，但查询负载是 `None`、空列表或无内容结果 | 否 | 否 | 是可观测的结果状态，对已声明查询工具不视为 Server 失败。 |
| `MALFORMED_RESPONSE` | 无法解析 JSON、缺少可用 content、响应类型违反工具约定 | 否 | 是 | Server 可达但结果不可用。 |
| `REMOTE_TOOL_ERROR` | MCP Result `isError=true`，且不能明确分类为权限错误 | 否 | 否 | Server 已正常回应，可能是参数或业务拒绝，不凭错误文本猜测健康度。 |
| `CIRCUIT_OPEN` | 调用前检查发现 Server 在打开期，或半开探测权已被其他进程获取 | 否 | 否 | 不发起真实请求。 |
| `UNKNOWN` | 无法安全归类的异常 | 否 | 否 | 记录完整日志，对 Agent 仅返回安全提示。不用未知编程错误触发熔断。 |

`asyncio.CancelledError` 不转换为 `FailureType`，必须继续向上抛出，保留 Hermes 和时间线整体超时的取消语义。

## 6. Retry policy

1. 当前 `TOOL_SPECS` 中的 MCP 工具全部是只读查询，但仍通过 `retry_on_timeout` 显式声明，不从工具名称推断幂等性。
2. 仅 `TIMEOUT` 可重试。
3. `max_retries = 3`，表示首次尝试后最多再试 3 次，最多 4 次真实 MCP 调用。
4. 单次尝试超时为 10 秒，使用 Python 标准库 `asyncio.timeout()` 或 `asyncio.wait_for()`。
5. 退避间隔为 `1s -> 2s -> 4s`，每次加 `0~200ms` 随机抖动；使用 `asyncio.sleep()` 和 `random.uniform()`。
6. 半开探测不重试，只执行一次真实调用。
7. 每次真实尝试必须重新执行 `mint_attestation()`。禁止复用前一次 JTI，否则 MCP Server 会将重试判定为重放攻击。
8. 连续超时在整个逻辑调用终止后只计一次 Server 失败，不按内部 attempt 数重复累加熔断计数。
9. 上层任务取消或超时时立即终止退避和后续尝试。Manager 不吞掉取消信号。
10. 不引入 `tenacity`、`backoff` 或其他重试依赖。

## 7. Circuit Breaker 状态机

### 7.1 参数

| 参数 | 值 |
|---|---:|
| 失败阈值 | 60 秒窗口内 5 次逻辑调用失败 |
| OPEN 冷却时间 | 30 秒 |
| 状态保留 TTL | 120 秒 |
| HALF_OPEN 探测租约 | 15 秒 |

### 7.2 状态转换

```text
CLOSED
  -- 可计数失败达 5 次/60s --> OPEN

OPEN
  -- 30s 冷却未结束 --> 直接返回 CIRCUIT_OPEN
  -- 30s 冷却结束 --> HALF_OPEN

HALF_OPEN
  -- 未获取探测租约 --> 直接返回 CIRCUIT_OPEN
  -- 获取租约 + 调用成功 --> CLOSED，清除失败和状态键
  -- 获取租约 + 调用失败 --> OPEN，重新开始 30s 冷却
```

### 7.3 状态规则

- `TIMEOUT`、`CONNECTION_REFUSED`、`MALFORMED_RESPONSE` 的最终逻辑调用失败计入 Server 熔断。
- `PERMISSION_DENIED`、`REMOTE_TOOL_ERROR`、`EMPTY_RESULT`、`UNKNOWN`、`CIRCUIT_OPEN` 不计入熔断。
- 任何正常非空成功都清除当前 Server 的连续失败计数。
- 查询类 `EMPTY_RESULT` 视为 Server 成功响应，同样清除连续失败计数。
- Circuit Breaker 粒度为 `server_id`，不是 Tool、Endpoint URL 或用户。同一 Server 的多个 Tool 共享熔断状态。

## 8. Redis 状态存储

### 8.1 键设计

| Redis Key | 类型 | TTL | 含义 |
|---|---|---:|---|
| `suning:mcp:cb:{server_id}:failures` | String counter | 60s | CLOSED 状态下当前窗口内的连续失败次数。 |
| `suning:mcp:cb:{server_id}:state` | String，值为 `open` | 120s | 表示 Server 已进入过 OPEN，冷却结束后仍用于识别 HALF_OPEN。 |
| `suning:mcp:cb:{server_id}:cooldown` | String | 30s | 存在时逻辑状态为 OPEN；过期且 `state` 仍存在时为 HALF_OPEN。 |
| `suning:mcp:cb:{server_id}:probe` | String，值为随机 owner token | 15s | `SET NX` 获取的半开探测租约，保证跨进程只放行一个探测。 |

这些键与已有 `suning:authn:jti:*`、`conv:*` 和 `im:*` 命名空间隔离。

### 8.2 操作规则

1. 失败计数使用 Redis 原子 `INCR`；首次失败设置 60 秒 TTL。
2. 达到阈值时设置 `state` 和 `cooldown`，并清除 `failures`。
3. 半开探测使用 `SET probe owner NX EX 15`。不使用进程内布尔值代替跨进程租约。
4. 半开成功时清除四类键；半开失败时重置 `state`/`cooldown` TTL，再安全释放属于自己的 probe。
5. Redis 读、写、解析或连接失败时，Manager 记录 `circuit_store_unavailable` 并 fail-open。不返回 `CIRCUIT_OPEN`，不中断 MCP 调用。
6. 不把 Circuit Breaker 存储合并到 `backend/mcp_suning/security/attestation.py` 的防重放 Client；两者运行在不同进程，职责不同。

## 9. Tool → DegradeLevel 映射

`DegradeLevel` 是字符串枚举：`L1_NON_CRITICAL`、`L2_CORE`、`L3_CRITICAL`。映射集中保存在 `.hermes/plugins/suning-rbac-bridge/schemas.py:TOOL_SPECS`。

| Server ID | Tool | DegradeLevel | 空结果语义 | 超时重试 |
|---|---|---|---|---:|
| `mcp-order` | `search_orders` | `L2_CORE` | 合法空列表，需标记 `empty` | 是 |
| `mcp-order` | `get_order_detail` | `L2_CORE` | 空响应异常，正常“未找到”应由 Server 以业务结果表达 | 是 |
| `mcp-aftersale` | `query_return_stats_nl2sql` | `L2_CORE` | 合法空列表，需标记 `empty` | 是 |
| `mcp-aftersale` | `query_aftersale_nl2sql` | `L2_CORE` | 合法空聚合结果，需标记 `empty` | 是 |
| `mcp-aftersale` | `get_aftersale_workflow` | `L1_NON_CRITICAL` | 合法空列表 | 是 |
| `mcp-product` | `get_product_info` | `L1_NON_CRITICAL` | 空响应异常 | 是 |
| `mcp-logistics` | `query_logistics` | `L1_NON_CRITICAL` | 合法空轨迹 | 是 |
| `mcp-payment` | `get_refund_status` | `L2_CORE` | 合法“尚无退款”业务结果；无内容响应异常 | 是 |
| `mcp-order-timeline` | `trace_order_timeline` | `L2_CORE` | 以返回的 `partial/source_failures/timeline` 为准，不由 Manager 重新解释单源空数据 | 是 |

`send_aftersale_chart` 和 `orchestrate_aftersale_analysis` 不是 MCP Client Tool，不进入该映射。

当前没有 L3 工具。不为了用满三个等级而人为将只读查询升级为“缺失就中止整个请求”。

## 10. L1/L2/L3 行为

### 10.1 L1 — 非核心数据

- 不阻断 Agent 后续工具调用和回答。
- `bridge.py` 返回 Hermes `tool_result`，不返回 `tool_error`。
- 降级负载至少包含 `status="degraded"`、`available=false`、`degrade_level="L1_NON_CRITICAL"`、`tool_name`、`failure_type` 和用户可读 `notice`。
- `notice` 格式：`辅助数据「{tool_name}」暂未获取，其他分析可继续。`
- Agent 可继续生成其他结论，但不得编造缺失维度。

### 10.2 L2 — 核心数据

- 不强制中断整个 Agent 请求，但必须在最终回答明确声明缺失部分。
- `bridge.py` 返回 Hermes `tool_result`，降级负载格式与 L1 相同，`degrade_level="L2_CORE"`。
- `notice` 格式：`核心数据「{tool_name}」暂不可用；当前结果不完整，请稍后重试该部分。`
- 复杂分析中，子 Agent 摘要必须保留该 notice；聚合报告必须将对应维度写为“暂不可用”。

### 10.3 L3 — 关键数据

- `bridge.py` 返回 `tool_error`，安全提示为：`关键数据服务暂不可用，请稍后重试。`
- 调用方应终止依赖该数据的后续分析，不使用空值生成伪完整报告。
- 当前无 L3 映射，因此首期实现只需完成行为分支和单元测试，不改变任何现有 Tool 的请求级结果。

### 10.4 成功和空结果兼容

- 非空成功仍使用现有 `_successful_result()` 转换，不给所有业务负载强制增加新信封。
- 合法空结果保留原数据；L2 工具另附加结构化 `empty` 提示，不改写为 Server 故障。
- 降级信封仅用于 Manager 最终失败或需显式标记的 L2 空结果，尽量降低对现有 Agent 工具输出的影响。

## 11. MCPCallResult 数据结构

`MCPCallResult` 是 `.hermes/plugins/suning-rbac-bridge/mcp_resilience.py` 中的 `@dataclass`：

| 字段 | 类型 | 含义 |
|---|---|---|
| `server_id` | `str` | `TOOL_SPECS` 声明的稳定 Server ID。 |
| `tool_name` | `str` | 实际调用的 MCP Tool 名。 |
| `success` | `bool` | 是否获得可供业务使用的结果。合法空查询为 `true`。 |
| `data` | `Any` | 成功时的 MCP Result 或已解析负载；失败时为 `None`。 |
| `error_type` | `FailureType | None` | 失败类型；合法空结果可为 `EMPTY_RESULT`。 |
| `error_message` | `str` | 内部诊断文本。对 Agent 返回前必须转换为安全提示。 |
| `retry_count` | `int` | 实际执行的额外重试次数，首次成功为 `0`。 |
| `total_latency_ms` | `float` | 从熔断检查后到最终结果的总耗时，包含退避时间。 |
| `degraded` | `bool` | 是否应以降级结果呈现。 |
| `degrade_level` | `DegradeLevel` | Tool 声明的降级等级。 |
| `degrade_note` | `str` | 面向 Agent/用户的可读缺失说明。 |
| `circuit_state` | `CircuitState` | 本次决策时的 `CLOSED` / `OPEN` / `HALF_OPEN`。 |

约束：

- `success=false` 时 `data=None`。
- `error_type=CIRCUIT_OPEN` 时 `retry_count=0`且不发起网络请求。
- `success=true` 且 `error_type=EMPTY_RESULT` 时不更新熔断失败。
- `error_message` 可写日志，不得直接暴露连接密钥、凭证、SQL 或内部堆栈。

## 12. Agent 聚合结果方式

### 12.1 普通 Agent 调用

1. Manager 返回 `MCPCallResult`。
2. `bridge.py` 负责转换：
   - 正常成功：复用 `_successful_result()`。
   - L1/L2 降级：返回结构化 `tool_result`，包含 `available=false` 和 `notice`。
   - L3 降级：返回安全 `tool_error`。
3. 不修改 Hermes Tool Registry 或 Agent Loop；降级语义通过现有 Tool Result 通道传递。
4. Agent 回答必须区分“查询成功但结果为空”和“服务失败导致数据缺失”。

### 12.2 复杂分析

1. 继续使用 `.hermes/plugins/suning-rbac-bridge/orchestration.py:TaskOrchestrator`，不新增聚合器。
2. `_task_context()` 要求子 Agent 将工具返回的 `degrade_level`、`tool_name` 和 `notice` 原样保留在摘要中，不将降级伪装成“没有数据”。
3. `aggregate()` 继续聚合子任务摘要；对 L2 notice 必须显式写入报告“暂不可用”部分。
4. 子 Agent 生命周期成功但某个 L1/L2 Tool 降级时，子任务可保持 `SUCCESS`，但摘要和最终报告必须保留缺失说明。不通过修改 Hermes 子 Agent 终态来表达业务降级。

### 12.3 订单时间线

1. `backend/mcp_suning/timeline/tracker.py` 继续用 `source_failures` 聚合四个内部数据源的部分失败。
2. Agent 侧 Manager 只管理 `mcp-order-timeline` 这次顶层调用，不把其返回的 `partial=true` 等同于 8106 Server 失败。
3. `_successful_result()` 保留 `partial`、`source_failures` 和已成功的 `timeline`，由 Agent 向用户说明具体缺失数据源。

## 13. Acceptance Criteria

1. `.hermes/plugins/suning-rbac-bridge/bridge.py` 中所有 `TOOL_SPECS` MCP 调用均通过同一个 `MCPCallManager` 实例执行。
2. Manager 由插件 `register()` 创建并通过 Handler 注入；不在每次工具调用时新建 Manager 或 Redis 连接池。
3. 首次超时后最多额外尝试 3 次，退避基础值为 1、2、4 秒并含 0~200ms 抖动。
4. 每次真实尝试使用新的 attestation/JTI；测试能证明两次 attempt 的凭证不同。
5. 连接拒绝、权限错误、MCP `isError`、脏响应和未知异常不重试。
6. 同一 `server_id` 60 秒内 5 次可计数逻辑失败后 OPEN 30 秒；OPEN 期调用不执行真实闭包。
7. OPEN 冷却后跨进程只有一个调用能获得 HALF_OPEN 探测租约。探测成功转 CLOSED，失败重新 OPEN 30 秒。
8. Redis 不可用时有明确日志，但不仅因 Circuit Store 故障阻止真实 MCP 调用。
9. Tool 降级等级和 Server ID 只在 `TOOL_SPECS` 声明一次；Handler 中不存在重复映射。
10. L1 失败不中断后续分析；L2 失败在最终回答标记不完整；L3 分支返回请求级安全错误。
11. 合法空查询结果不增加熔断失败计数，与服务不可用的降级负载可明确区分。
12. `trace_order_timeline` 的现有四路并发、10 秒整体超时、`source_failures` 和部分时间线语义保持不变。
13. OTel/结构化日志可查看 `retry_count`、`failure_type`、`circuit_state`、`degrade_level` 和最终成功状态。
14. 不修改 Hermes 源码，不新增第三方依赖，不在 MCP Server Tool 内增加 resilience 逻辑。
15. `cd backend && uv run pytest` 全部通过，包括 `tests/test_function_documentation.py`。

## 14. Test Cases

新增 `backend/tests/test_mcp_resilience.py`，使用现有 `pytest`、`pytest-asyncio`、`monkeypatch` 和内存 Fake Redis，不连接真实 MCP/Redis。所有新增或修改的测试函数遵循项目 `输入/输出/功能` docstring 约束。

| ID | 场景 | 期望 |
|---|---|---|
| T01 | 首次调用成功 | `success=true`、`retry_count=0`、熔断失败计数清空。 |
| T02 | 前两次超时，第三次成功 | 实际调用 3 次，`retry_count=2`，退避基础值为 1s/2s，最终不计失败。 |
| T03 | 四次均超时 | 总调用 4 次，`TIMEOUT`，逻辑失败计数只加 1。 |
| T04 | 重试凭证 | 每次 attempt 的 attestation/JTI 均不同，不复用 `meta`。 |
| T05 | 连接拒绝 | 调用 1 次，`CONNECTION_REFUSED`，计入熔断。 |
| T06 | 权限拒绝 | 调用 1 次，`PERMISSION_DENIED`，不重试且不计入熔断。 |
| T07 | MCP `isError=true` | 调用 1 次，`REMOTE_TOOL_ERROR`，不重试，Agent 不获得内部堆栈。 |
| T08 | 无 content 或非法 JSON | `MALFORMED_RESPONSE`，不重试，计入熔断。 |
| T09 | 合法空查询 | `success=true`、`EMPTY_RESULT`，不计失败；L2 有 empty 提示。 |
| T10 | 60 秒内第 5 次可计数失败 | 写入 `state`/`cooldown`，Server 进入 OPEN。 |
| T11 | OPEN 期调用 | 返回 `CIRCUIT_OPEN`，真实调用闭包未执行。 |
| T12 | OPEN 冷却结束后两个并发调用 | 只有一个取得 probe；另一个立即降级。 |
| T13 | HALF_OPEN 探测成功 | 清除 state/cooldown/probe/failures，转 CLOSED。 |
| T14 | HALF_OPEN 探测失败 | 重置 30 秒 cooldown，转 OPEN。 |
| T15 | Redis 读写异常 | 记录 store unavailable，仍执行真实 MCP 调用。 |
| T16 | L1 最终失败 | 返回 `tool_result` 降级信封，后续分析可继续。 |
| T17 | L2 最终失败 | 返回 `tool_result` 降级信封，包含“当前结果不完整”。 |
| T18 | L3 最终失败 | 返回安全 `tool_error`，不返回伪空数据。 |
| T19 | 复杂分析中子 Agent 获得 L2 降级 | 子任务摘要和聚合报告都保留“暂不可用”。 |
| T20 | `trace_order_timeline` 返回 `partial=true` | 顶层 Manager 视为 8106 成功，保留 `source_failures`，不错误打开 8106 熔断。 |
| T21 | 上层取消发生在退避或真实调用中 | `CancelledError` 向上传播，不继续重试。 |
| T22 | Tool 映射一致性 | `TOOL_SPECS`、`plugin.yaml`、MCP Server Tool 名与降级策略无缺失或多余项。 |

现有回归测试：

- `backend/tests/test_order_timeline.py`：保持四路并发、整体超时、脏数据标记和部分结果。
- `backend/tests/test_mcp_rbac_wiring.py`：保持 Tool 名、插件清单、鉴权和脱敏边界。
- `backend/tests/test_auth_attestation.py`：保持 JTI 防重放和 Redis 默认拒绝策略。
- `backend/tests/test_observability.py`：扩展重试/熔断/降级属性断言。

## 15. 实现阶段拆分

### Phase 1 — 类型、策略和纯内存逻辑

文件：

- 新增 `.hermes/plugins/suning-rbac-bridge/mcp_resilience.py`
- 新增 `backend/tests/test_mcp_resilience.py`
- 修改 `.hermes/plugins/suning-rbac-bridge/schemas.py`

内容：

1. 实现枚举、`MCPCallResult`、Failure 分类和 Tool 策略声明。
2. 先用 Fake Store 测试 retry/degrade 逻辑，不接网络和真实 Redis。
3. 确保不引入新依赖。

### Phase 2 — Redis Circuit Store

文件：

- 修改 `.hermes/plugins/suning-rbac-bridge/mcp_resilience.py`
- 修改 `.hermes/plugins/suning-rbac-bridge/context_hooks.py`
- 修改 `backend/tests/test_mcp_resilience.py`

内容：

1. 使用已有 `redis` 依赖实现失败计数、OPEN 冷却和 HALF_OPEN 租约。
2. 提取可复用 Redis Client 构建入口，保留 Context Hooks 现有依赖注入。
3. 完成 Redis 故障 fail-open 和并发探测测试。

### Phase 3 — 桥接边界接入

文件：

- 修改 `.hermes/plugins/suning-rbac-bridge/__init__.py`
- 修改 `.hermes/plugins/suning-rbac-bridge/bridge.py`
- 修改 `backend/tests/test_mcp_resilience.py`

内容：

1. 注册时创建并注入 Manager，不每次重建 Redis Client。
2. 将每次 attestation 签发放入 attempt 闭包。
3. 保留现有 RBAC 身份来源、Trace metadata、`_successful_result()` 和安全错误文本。
4. 完成 L1/L2/L3 的 `tool_result/tool_error` 转换。

### Phase 4 — Agent 聚合与观测

文件：

- 修改 `.hermes/plugins/suning-rbac-bridge/orchestration.py`
- 修改 `.hermes/plugins/suning-rbac-bridge/observability.py`
- 修改 `backend/tests/test_complex_analysis_orchestration.py`
- 修改 `backend/tests/test_observability.py`

内容：

1. 要求子 Agent 和聚合报告保留降级标注。
2. 在现有 Span 上补充 retry/circuit/degrade 属性。
3. 不改 Hermes 子 Agent 生命周期、Tool Registry 或 Agent Loop。

### Phase 5 — 回归和文档同步

文件：

- 根据实际行为更新 `backend/.env.example`、`backend/README.md` 和需求文档
- 不修改 MCP Server Tool 业务实现

验证：

```bash
cd backend
uv run pytest
```

只有当生产指标证明 `trace_order_timeline` 内部某个下游源需要独立重试/熔断时，才进入后续独立 Spec，评估如何让 `backend/mcp_suning/timeline/gateway.py:PrivateMCPCaller` 复用同一套策略而不复制实现。
