# 05m · MCP 调用失败重试与降级 Implementation Plan

- 依据：`specs/05m-mcp-resilience.md`
- 状态：Ready for implementation
- 范围：仅当前仓库的 `suning-rbac-bridge` MCP Client 边界、相关聚合与测试
- 排除：Hermes 源码、MCP Server `@mcp.tool`、时间线内部 `PrivateMCPCaller`、新第三方依赖、通用 resilience framework

## Spec Gap / Repository Constraint

### G1 — `schemas.py` 目前被 `runpy.run_path()` 独立加载

- 现状：`backend/tests/test_mcp_rbac_wiring.py:test_hermes_bridge_tool_names_match_mcp_servers` 用 `runpy.run_path()` 执行 `schemas.py`。
- 冲突：Spec 要求 `DegradeLevel` 定义在新模块 `mcp_resilience.py`，`schemas.py` 需要相对导入；`run_path()` 没有 package context，相对导入会失败。
- 最小修改：只调整该测试的加载 helper，仿照 `test_auth_attestation.py:_load_real_bridge` 构造临时 package 后加载 `schemas`；不把枚举重复定义到 `schemas.py`。

### G2 — 缺少 `REDIS_URL` 时仍必须注册业务工具

- 现状：`register()` 先注册工具，再构建 Context Runtime；`test_plugin_keeps_rbac_tools_when_context_env_is_missing` 锁定了该降级行为。
- 冲突：Spec 要求插件注册时创建 Manager 并复用 Redis Client，但不能让 Circuit Store 配置变成工具注册前置条件。
- 最小修改：`MCPCallManager` 允许无 Store 运行；`register()` 尝试构建 Redis Client，失败则注入 fail-open Manager 并继续注册所有工具。

### G3 — 协议级空结果与业务级空结果不能统一猜测

- 现状：`_successful_result()` 可转发任意 `structuredContent`；物流空轨迹可表现为 `{"traces": []}`，支付无退款是非空业务对象。
- 约束：Manager 不应理解每个业务 Schema。
- 最小修改：Manager 只识别协议级 `None`、原始空列表或无 content；非空映射一律交给现有业务返回语义。`ToolSpec.empty_result_is_success` 仅决定协议级空结果是 `EMPTY_RESULT` 还是 `MALFORMED_RESPONSE`。

### G4 — MCP `isError` 没有稳定的权限错误码

- 现状：`bridge.py` 只读取 `isError` 和文本 content。
- 最小修改：仅将本地 `PermissionError` 或可确认的 HTTP 401/403 分类为 `PERMISSION_DENIED`；其他 `isError=true` 统一为 `REMOTE_TOOL_ERROR`，不用文本模糊匹配猜测。

### G5 — 复杂分析只能看到子 Agent 自由文本摘要

- 现状：`TaskOrchestrator._run_task()` 仅保存 `lifecycle.result(handle).summary`，没有原始 Tool Result 或结构化降级字段。
- 最小修改：不修改 Hermes 生命周期；在 `_task_context()` 中要求原样保留 `degrade_level/tool_name/notice`，并在 `aggregate()` 的 system message 中要求 L2 notice 必须进入最终报告。测试锁定提示和 evidence 传递，不伪造 Hermes 未提供的结构化通道。

### G6 — attestation 配置校验与每次重签存在时序冲突

- 现状：`mint_attestation()` 同时校验密钥/签发方并产生一次性 JTI，`invoke_business_tool()` 在建立调用前捕获 `BridgeConfigurationError`。
- 冲突：将 `mint_attestation()` 移入 attempt 后，配置错误不能被误分类为 MCP `UNKNOWN` 并进入 L1/L2 降级。
- 最小修改：在 `bridge.py` 提取无 JTI 副作用的配置校验 helper，调用 Manager 前保留现有配置错误返回；真正 `mint_attestation()` 仍在每个 attempt 内执行。

### G7 — 目标文件存在用户未提交修改

- `.hermes/plugins/suning-rbac-bridge/__init__.py` 已有 `on_session_end` Hook 注册修改。
- 最小修改：Phase 3 只在工具注册和 Runtime 构建附近改动，必须保留 `on_session_end` 及其他用户差异。

## Phase 1 — 类型、策略和纯内存逻辑

### 1. Goal

完成插件专用的 resilience 类型、Tool 策略、Failure 分类和超时重试主流程，使用无 Redis Store 和异步 fake callable 单测。

本阶段不完成 Redis 熔断、Bridge 建连接入、Hermes 返回转换、Agent 聚合或观测字段。

### 2. Files

- 新增：`.hermes/plugins/suning-rbac-bridge/mcp_resilience.py`
- 新增：`backend/tests/test_mcp_resilience.py`
- 修改：`.hermes/plugins/suning-rbac-bridge/schemas.py`
- 修改：`backend/tests/test_mcp_rbac_wiring.py`
- 不应修改：`bridge.py`、`__init__.py`、`context_hooks.py`、`observability.py`、`orchestration.py`、`backend/mcp_suning/*_server.py`、Hermes 源码

### 3. Existing Symbols

- `schemas.py:ToolSpec` dataclass；已有字段 `endpoint_env`、`default_endpoint`、`schema`。
- `schemas.py:TOOL_SPECS`；已有 9 个 MCP Client Tool 配置。
- `bridge.py:_result_text()` 和 `_successful_result()`；Phase 1 仅作为后续响应语义参照，不修改。
- `test_mcp_rbac_wiring.py:DATABASE_TOOL_NAMES`、`_manifest_tool_names()`、`test_hermes_bridge_tool_names_match_mcp_servers()`。
- `test_auth_attestation.py:_load_real_bridge()`；复用其临时 package 装载模式，不直接调用该私有 helper。
- `test_function_documentation.py:SOURCE_ROOTS`、`REQUIRED_SECTIONS`；新模块和测试函数会被自动扫描。

### 4. Changes

#### Step 1 — 建立类型和 Tool 策略

- 在 `mcp_resilience.py` 新增 `FailureType`、`DegradeLevel`、`CircuitState` 字符串枚举和 `MCPCallResult` dataclass，字段严格对应 Spec 第 11 节。
- 新增固定策略常量：`MAX_RETRIES=3`、`ATTEMPT_TIMEOUT_SECONDS=10`、`BASE_BACKOFF_SECONDS=1`、`MAX_JITTER_SECONDS=0.2`。不增加环境变量或配置类。
- 扩展 `ToolSpec` 字段：`server_id`、`degrade_level`、`retry_on_timeout`、`empty_result_is_success`。
- 按 Spec 第 9 节更新全部 9 个 `TOOL_SPECS` 条目；不改 Tool Schema 和 Endpoint。
- 将 `test_mcp_rbac_wiring.py` 的 schema 加载改为支持相对导入的临时 package helper，并在 T22 中断言每个 MCP Tool 都有完整策略。
- 影响：只增加静态策略数据，不改变当前调用链。

#### Step 2 — 实现单次调用和 Failure 分类

- 在 `mcp_resilience.py` 新增 `MCPCallManager`，初始允许 `redis_client=None`。
- Manager 接收 `server_id/tool_name/degrade_level/retry_on_timeout/empty_result_is_success` 和无参数异步 attempt callable，不导入 `schemas.py` 或 `bridge.py`。
- 分类 `TIMEOUT`、`CONNECTION_REFUSED`、`PERMISSION_DENIED`、`EMPTY_RESULT`、`MALFORMED_RESPONSE`、`REMOTE_TOOL_ERROR`、`UNKNOWN`；`CancelledError` 在宽泛异常捕获前单独向上抛出。
- 仅检查 MCP 协议级响应：`isError`、`structuredContent`、`content`；不解析物流/支付/时间线业务字段。
- 影响：形成可测的调用结果，但尚未被生产 Bridge 调用。

#### Step 3 — 实现超时重试与退避

- 在 `MCPCallManager.call()` 中用 `asyncio.wait_for()` 或 `asyncio.timeout()` 约束每次 attempt。
- 仅 `TIMEOUT` 且 `retry_on_timeout=true` 时重试，最多 4 次尝试。
- 用 `asyncio.sleep()` 和 `random.uniform()` 实现 1/2/4 秒退避及 0~200ms 抖动；测试用 monkeypatch 记录参数，不真实等待。
- 逻辑调用结束时填充 `retry_count`、`total_latency_ms`、`degraded`、`degrade_note`。
- 影响：重试主流程可独立验证，仍不发起真实 MCP 请求。

### 5. Tests

- 新测试模块使用临时 plugin package loader，避免导入 Hermes 宿主。
- 新增 async fake callable：按序列返回 MCP Result 或抛出异常，记录调用次数。
- T01：首次成功与 `retry_count=0`。
- T02：两次超时后成功，monkeypatch `asyncio.sleep/random.uniform`。
- T03：四次超时，最终仅产生一个逻辑失败结果。
- T05/T06/T07/T08/T09：分别验证连接、权限、`isError`、脏响应和合法空响应。
- T21：在 attempt 和退避中取消都不继续重试。
- T22：`TOOL_SPECS`、`plugin.yaml`、Server Tool 名和策略完整性。
- 需保持：`test_mcp_rbac_wiring.py`、`test_function_documentation.py`。
- 本阶段不需要 Fake Redis。

### 6. Acceptance Criteria Mapping

- AC3 → Phase 1 / Step 3
- AC5 → Phase 1 / Step 2、Step 3
- AC9 → Phase 1 / Step 1
- AC11 → Phase 1 / Step 2
- AC14 → Phase 1 / Step 1~3（仅 stdlib + 已安装 `httpx`，不触及 Server/Hermes）
- AC15 → Phase 1 / 所有新函数和测试 docstring

### 7. Verification

```bash
cd backend
uv run pytest tests/test_mcp_resilience.py tests/test_mcp_rbac_wiring.py tests/test_function_documentation.py
```

### 8. Risks

- `httpx.TimeoutException` 和 `httpx.ConnectError` 可复用已安装 `httpx`，但不应将所有 `OSError` 粗暴归类为连接拒绝。
- `asyncio.CancelledError` 必须在通用 Exception 转换之前向上抛出。
- L2 空结果后续需要信封才能附带 notice；Phase 1 只产生 `MCPCallResult`，不提前改 Hermes 输出。

## Phase 2 — Redis Circuit Store

### 1. Goal

用已有同步 Redis Client 实现 Spec 规定的 CLOSED/OPEN/HALF_OPEN 状态、失败窗口、跨进程探测租约和 Store 故障 fail-open，并为插件提供可复用 Redis Client 构建入口。

本阶段不修改 Bridge Handler 签名，不将 Manager 注入生产调用链。

### 2. Files

- 新增：无
- 修改：`.hermes/plugins/suning-rbac-bridge/mcp_resilience.py`
- 修改：`.hermes/plugins/suning-rbac-bridge/context_hooks.py`
- 修改：`backend/tests/test_mcp_resilience.py`
- 修改：`backend/tests/test_conversation_hooks.py`
- 不应修改：`bridge.py`、`__init__.py`、`observability.py`、`orchestration.py`、`backend/mcp_suning/security/attestation.py`、所有 MCP Server Tool、Hermes 源码

### 3. Existing Symbols

- `context_hooks.py:_required_env()`、`build_conversation_runtime()`、`build_conversation_hooks()`、`register_context_hooks()`。
- `context_hooks.py` 中的 `redis.Redis.from_url(..., decode_responses=False)` 现有 Client 构建逻辑。
- `test_conversation_hooks.py:FakeRedis`、`FakePluginContext`、`_load_plugin_package()`。
- `test_conversation_hooks.py:test_plugin_registers_pre_and_post_llm_hooks()`、`test_plugin_keeps_rbac_tools_when_context_env_is_missing()`。
- Phase 1 新增的 `MCPCallManager`、`CircuitState`、`FailureType`、`MCPCallResult`及常量。

### 4. Changes

#### Step 1 — 实现 CLOSED 失败窗口与 OPEN 快速失败

- 在 `mcp_resilience.py` 内新增插件专用 `CircuitStore`，不新增独立 package/protocol/factory。
- 新增固定常量：阈值 5、失败 TTL 60s、OPEN cooldown 30s、state TTL 120s、probe TTL 15s。
- 使用 `INCR + EXPIRE`、`state`、`cooldown` 键实现阈值和 OPEN；达阈值后清除 failure counter。
- `MCPCallManager.call()` 在 attempt 前读取状态；OPEN 时返回 `CIRCUIT_OPEN` 且不调用 fake callable。
- 逻辑失败只在所有重试耗尽后计数一次；成功和 `EMPTY_RESULT` 清除 failures。

#### Step 2 — 实现 HALF_OPEN 单探测租约

- 当 `state` 存在而 `cooldown` 过期时返回 `HALF_OPEN`。
- 使用 Redis `SET probe owner NX EX 15` 语义取得租约；未取得者返回 `CIRCUIT_OPEN`。
- HALF_OPEN 只执行一次 attempt；成功清除四类键，失败重置 state/cooldown。
- 释放 probe 时必须比较 owner token 后再删除，防止删掉过期后新 owner 的租约；使用已有 Redis 原子能力，不增加分布式锁依赖。

#### Step 3 — 复用 Redis Client 并实现 fail-open

- 在 `context_hooks.py` 提取 `build_redis_client()`，保留当前 URL 和 `decode_responses=False`。
- 将 `build_conversation_runtime()` 改为接收可选已构建 Redis Client；参数为 `None` 时调用 `build_redis_client()`，不使用布尔求值触发 Redis 连接。
- CircuitStore 的所有同步 Redis 操作通过 `asyncio.to_thread()`。
- Redis 任意操作异常时记录 `circuit_store_unavailable`，Manager 继续真实 attempt；不改 `auth_attestation.py` 的 fail-closed 防重放逻辑。
- 在 `context_hooks.__all__` 中导出 `build_redis_client`。

### 5. Tests

- 在 `test_mcp_resilience.py` 新增线程安全 `FakeCircuitRedis`，仅实现本 Spec 需要的 `get/set/incr/expire/delete/exists` 与 TTL 推进；不扩展现有 Context `FakeRedis`。
- T10：第 5 次可计数失败进入 OPEN。
- T11：OPEN 期闭包未执行。
- T12：两个并发 HALF_OPEN 调用仅一个取得 probe。
- T13/T14：探测成功转 CLOSED，失败重新 OPEN。
- T15：Fake Redis 抛异常时真实 callable 仍执行，caplog 可见 store unavailable。
- 补充 T03：四次超时只 `INCR` 一次。
- `test_conversation_hooks.py` 新增 Client 复用断言：`build_conversation_runtime(existing_client)` 不再调用 `Redis.from_url`。
- 需保持：`test_plugin_registers_pre_and_post_llm_hooks`、`test_plugin_keeps_rbac_tools_when_context_env_is_missing`、已有 ContextManager 测试。

### 6. Acceptance Criteria Mapping

- AC2 → Phase 2 / Step 3（共享 Client 构建能力，注册注入在 Phase 3）
- AC6 → Phase 2 / Step 1
- AC7 → Phase 2 / Step 2
- AC8 → Phase 2 / Step 3
- AC11 → Phase 2 / Step 1
- AC14 → Phase 2 / Step 1~3
- AC15 → Phase 2 / docstring 与相关回归

### 7. Verification

```bash
cd backend
uv run pytest tests/test_mcp_resilience.py tests/test_conversation_hooks.py::test_plugin_keeps_rbac_tools_when_context_env_is_missing tests/test_function_documentation.py
```

### 8. Risks

- 同步 Redis 操作不得直接运行在事件循环；所有 Store I/O 必须走 `asyncio.to_thread()`。
- `decode_responses=False` 会返回 bytes，状态解析必须同时接受 `b"open"` 和 `"open"`。
- HALF_OPEN probe 必须跨进程互斥，不能用 Manager 实例字段替代 Redis `SET NX`。
- Redis 熔断 Store 的 fail-open 与 Server 防重放 Redis 的 fail-closed 是有意区分，不应合并。

## Phase 3 — 桥接边界接入

### 1. Goal

将单个 `MCPCallManager` 注入全部 `TOOL_SPECS` Handler，把每次 attestation 重签、MCP 建连和 `call_tool` 放入 attempt，并完成成功、L1/L2/L3、空结果与 timeline partial 的 Hermes 返回转换。

本阶段不改 MCP Server Tool，不修改 `backend/mcp_suning/timeline/gateway.py`，不修改 Hermes Agent Loop。

### 2. Files

- 新增：无
- 修改：`.hermes/plugins/suning-rbac-bridge/__init__.py`
- 修改：`.hermes/plugins/suning-rbac-bridge/bridge.py`
- 修改：`backend/tests/test_mcp_resilience.py`
- 修改：`backend/tests/test_conversation_hooks.py`
- 可能仅扩展回归断言：`backend/tests/test_mcp_rbac_wiring.py`
- 不应修改：`backend/mcp_suning/servers/*.py`、`backend/mcp_suning/timeline/gateway.py`、`backend/mcp_suning/timeline/tracker.py`、Hermes 源码、`plugin.yaml`

### 3. Existing Symbols

- `bridge.py:BridgeConfigurationError`、`_required_env()`、`current_identity()`、`mint_attestation()`、`_result_text()`、`_successful_result()`。
- `bridge.py:invoke_business_tool()`、`Handler`、`make_handler()`。
- `bridge.py:TOKEN_TTL_SECONDS`、`MIN_SECRET_BYTES`、`TOKEN_ISSUER`、`TOKEN_AUDIENCE`。
- `__init__.py:register()` 与 `TOOL_SPECS` 注册循环。
- `context_hooks.py:build_redis_client()`、`build_conversation_runtime()`（Phase 2 后）。
- `observability.ensure_trace()`、`start_mcp_span()`、`inject_trace_metadata()`、`end_mcp_span()`、`finish_trace()`；本阶段保持调用，扩展字段放在 Phase 4。
- `test_conversation_hooks.py:FakePluginContext`、`_load_plugin_package()`、`test_plugin_keeps_rbac_tools_when_context_env_is_missing()`。
- `test_order_timeline.py:test_trace_starts_all_sources_and_returns_partial_data_after_timeout()` 作为 T20 语义参照。

### 4. Changes

#### Step 1 — 在插件注册时创建并注入唯一 Manager

- 调整 `register()` 的局部顺序：尝试导入/创建 Redis Client，创建一个 `MCPCallManager`，再循环注册 Tool Handler。
- Redis 配置缺失或 Context 模块导入失败时，创建无 Store Manager 并继续注册业务、图表和编排工具。
- Context Runtime 可用时调用 `build_conversation_runtime(redis_client)` 复用同一 Client。
- 将 `make_handler(tool_name)` 改为 `make_handler(tool_name, call_manager)`，闭包继续忽略 Hermes 多余 kwargs。
- 保留现有 `on_session_end` 和所有 Hook 注册。

#### Step 2 — 将真实 MCP 调用封装为每次 attempt

- 扩展 `invoke_business_tool()` 接收注入的 `MCPCallManager`。
- 提取 bridge-local 配置校验 helper，在 Manager 前保留当前 `BridgeConfigurationError` 处理。
- 在 `invoke_business_tool()` 内创建 async attempt closure；每次执行时调用 `mint_attestation()`，创建新 metadata，建立 `streamable_http_client`/`ClientSession`，`initialize()` 后执行 `call_tool()`。
- attempt 不缓存 attestation 或 metadata；Trace ID 可复用，JTI 必须每次不同。
- Manager 参数直接来自当前 `ToolSpec`，不在 Bridge 重复维护 Server/降级映射。

#### Step 3 — 转换成功、空结果和 L1/L2/L3 降级输出

- 普通成功继续调用 `_successful_result(result.data)`，不修改现有业务负载。
- L1/L2 最终失败用 `tool_result()` 返回 Spec 约定的 `status/available/degrade_level/tool_name/failure_type/notice`。
- L3 用 `tool_error("关键数据服务暂不可用，请稍后重试。")`；当前无 L3 Tool，仅通过构造策略单测分支。
- L2 协议级合法空结果返回 `status="empty"/available=true/data=<原结果>/notice`信封；L1 合法空结果按原成功负载转发。
- 内部 `error_message` 只写日志/Span，用户输出只使用 Spec 安全 notice。

#### Step 4 — 锁定 timeline partial 和现有注册回归

- 构造 `trace_order_timeline` 的 MCP 成功 Result，其 `structuredContent` 包含 `partial=true/source_failures/timeline`，断言 Manager 返回 success 并由 `_successful_result()` 原样转发。
- 断言该结果不增加 `mcp-order-timeline` 熔断失败。
- 扩展插件注册测试，验证 9 个 MCP Handler 共享同一 Manager，且缺 Redis 时工具清单不变。

### 5. Tests

- T04：monkeypatch `mint_attestation()` 每次返回唯一 token，模拟首次超时后成功，断言两次 `meta["suning/authn"]` 不同。
- T16/T17/T18：构造 L1/L2/L3 策略并断言 `tool_result/tool_error` 转换。
- T20：`partial=true` 保持成功，`source_failures` 不丢失，不计熔断。
- 桥接集成覆盖 T01、T05、T06、T07、T08、T09、T21，确保纯 Manager 逻辑接入真实 Bridge 后不变。
- 需 async fake transport/ClientSession，monkeypatch `streamable_http_client`、`ClientSession`、`current_identity`、`mint_attestation`、`tool_result`、`tool_error`。
- 需保持：`test_plugin_keeps_rbac_tools_when_context_env_is_missing`、`test_hermes_bridge_tool_names_match_mcp_servers`、`test_auth_attestation.py`、`test_order_timeline.py`。

### 6. Acceptance Criteria Mapping

- AC1 → Phase 3 / Step 1、Step 2
- AC2 → Phase 3 / Step 1
- AC4 → Phase 3 / Step 2
- AC5 → Phase 3 / Step 2
- AC9 → Phase 3 / Step 2
- AC10 → Phase 3 / Step 3
- AC11 → Phase 3 / Step 3
- AC12 → Phase 3 / Step 4
- AC14 → Phase 3 / Step 1~4
- AC15 → Phase 3 / 相关单测与 docstring

### 7. Verification

```bash
cd backend
uv run pytest tests/test_mcp_resilience.py tests/test_conversation_hooks.py::test_plugin_keeps_rbac_tools_when_context_env_is_missing tests/test_mcp_rbac_wiring.py tests/test_auth_attestation.py
```

### 8. Risks

- attestation/JTI 必须每个 attempt 重新签发；不能将 metadata 放在 attempt 外复用。
- 四次 10 秒 attempt 加退避可超过复杂分析 30 秒子任务限制；上层取消必须立即传播，不承诺所有 retry 必然完成。
- `BridgeConfigurationError` 必须继续返回“配置不完整”，不进入业务 L1/L2 降级。
- timeline `partial=true` 是业务成功负载，不能被误判为 8106 Server 失败。
- `__init__.py` 有现存用户修改，实现时不得覆盖 `on_session_end`。

## Phase 4 — Agent 聚合与观测

### 1. Goal

让复杂分析子 Agent 和报告保留 L1/L2 降级标注，并在现有 MCP Span 上记录 retry/circuit/degrade 字段。

本阶段不新增聚合器、不修改 Hermes 子 Agent 终态、不改后端时间线 OTel 实现。

### 2. Files

- 新增：无
- 修改：`.hermes/plugins/suning-rbac-bridge/orchestration.py`
- 修改：`.hermes/plugins/suning-rbac-bridge/observability.py`
- 修改：`.hermes/plugins/suning-rbac-bridge/bridge.py`（仅传递新 Span 参数）
- 修改：`backend/tests/test_complex_analysis_orchestration.py`
- 修改：`backend/tests/test_observability.py`
- 不应修改：`backend/mcp_suning/observability.py`、Hermes 生命周期代码、MCP Server Tool

### 3. Existing Symbols

- `orchestration.py:_task_context()`、`TaskStatus`、`SubTask`、`TaskDAG`。
- `orchestration.py:TaskOrchestrator._run_task()`、`aggregate()`、`_fallback_report()`。
- `orchestration.py:AGGREGATION_TIMEOUT_SECONDS`、`MAX_SUMMARY_CHARS`。
- `observability.py:TraceSpan` dataclass、`AgentTrace` dataclass、`AgentObservability`。
- `AgentObservability.start_mcp_span()`、`end_mcp_span()`、`finish_trace()`、`_end_span()`。
- `test_complex_analysis_orchestration.py:_load_orchestration()`、`_FakeLlm`、`_FakeLifecycle`、`test_orchestrator_executes_dependency_layers_and_aggregates()`。
- `test_observability.py:_load_observability()`、`test_trace_aggregates_llm_mcp_failure_and_resets_context()`。

### 4. Changes

#### Step 1 — 保留子 Agent 降级标注

- 修改 `_task_context()`：明确要求子 Agent 在工具返回降级信封时原样保留 `degrade_level`、`tool_name`、`notice`，不改写为“未返回数据”。
- 修改 `aggregate()` system message：如 evidence 包含 L2 notice，报告必须列入“暂不可用”，不得依据缺失维度下结论。
- 保留 `_run_task()` 对成功子 Agent 的 `TaskStatus.SUCCESS`；不解析 Hermes 内部 Tool 轨迹。
- `_fallback_report()` 已原样输出成功摘要，只增加回归断言，不重写。

#### Step 2 — 扩展现有 MCP Span 属性

- 扩展 `AgentObservability.end_mcp_span()` 的可选关键字参数：`retry_count`、`failure_type`、`circuit_state`、`degrade_level`；提供与现有调用兼容的默认值。
- 把字段写入已有 `TraceSpan.attributes` 和 OTel Span；不新建 Span 类型或观测 Client。
- 在 `bridge.py` 结束 Span 时从 `MCPCallResult` 传入该四个字段。
- `finish_trace()` 现有总调用/总失败统计保持不变；单次 retry attempt 不误算为多个 Agent MCP Tool 调用。

### 5. Tests

- T19：扩展 `_FakeLifecycle` 或新建最小降级 Lifecycle fake，返回包含 `L2_CORE/tool_name/notice` 的成功摘要；断言后续依赖任务和 aggregate evidence 都保留 notice。
- 扩展 `_FakeLlm` 记录聚合 messages，断言 system message 要求 L2 暂不可用语义，最终 fake 报告保留 notice。
- 断言 `_fallback_report()` 不丢失成功子任务中的 L1/L2 notice。
- `test_observability.py` 新增/扩展断言：`TraceSpan.attributes` 包含 `retry_count/failure_type/circuit_state/degrade_level`，现有 `total_mcp_calls/total_mcp_failures` 不变。
- 需保持：`test_aggregate_falls_back_when_outer_timeout_expires`、`test_handler_runs_multi_im_analysis`、`test_timeline_trace_reuses_bridge_id_for_parallel_mcp_spans`。
- 本阶段不需要 Fake Redis 或 MCP transport。

### 6. Acceptance Criteria Mapping

- AC10 → Phase 4 / Step 1
- AC13 → Phase 4 / Step 2
- AC14 → Phase 4 / Step 1、Step 2
- AC15 → Phase 4 / 聚合、观测和 docstring 回归

### 7. Verification

```bash
cd backend
uv run pytest tests/test_complex_analysis_orchestration.py tests/test_observability.py tests/test_function_documentation.py
```

### 8. Risks

- L1/L2 降级只能通过子 Agent 摘要传递；不能声称修改了 Hermes 终态或获得了原始 Tool Result。
- 聚合 LLM 可能不遵循指令；当前仓库可保证的最小边界是降级 notice 已进入摘要和 aggregate evidence。
- retry 是一个逻辑 MCP Tool 调用，现有 `total_mcp_calls` 不应因 attempt 数膨胀。

## Phase 5 — 回归和文档同步

### 1. Goal

同步最终实现行为与运维说明，运行完整测试，确认没有修改 Hermes、MCP Server Tool 或依赖锁文件。

本阶段不新增功能，不调整 retry/circuit/degrade 策略。

### 2. Files

- 新增：无
- 修改：`backend/README.md`
- 修改：`docs/05-技术重难点/05m-MCP调用失败重试与降级.md`
- 核对但预计不修改：`backend/.env.example`（策略是 Spec 固定常量，没有新环境变量）
- 不应修改：`backend/pyproject.toml`、`backend/uv.lock`、`.hermes/plugins/suning-rbac-bridge/requirements.txt`、`backend/mcp_suning/*_server.py`、Hermes 源码

### 3. Existing Symbols

- `backend/README.md` 中“MCP 调用链路观测”、“跨系统订单全链路”、“测试”章节。
- `backend/.env.example:REDIS_URL`、`SUNING_MCP_*_URL`、`ORDER_TIMELINE_TIMEOUT_SECONDS`；仅校验，不新增未使用变量。
- `backend/pyproject.toml` 现有 `fastmcp`、`httpx`、`redis`、`pytest`、`pytest-asyncio` 依赖。
- `test_function_documentation.py:test_every_python_function_documents_input_output_and_purpose()`。
- Phase 1~4 涉及的全部新增和回归测试。

### 4. Changes

#### Step 1 — 文档、差异边界和全量验证

- 更新 `backend/README.md`：说明仅超时重试、5/60s 熔断、30s 冷却、Redis fail-open Circuit Store、L1/L2 返回语义和 timeline partial 保留。
- 将需求文档中的示例说明同步为实际文件路径和已实现边界；不把示例代码复制成第二套实现。
- 运行完整 pytest，修复仅与本需求差异直接相关的回归问题。
- 检查 git diff：不应出现 Hermes 源码、Server Tool、`pyproject.toml`、lockfile 或 plugin requirements 变更；保留任务开始前的用户修改。

### 5. Tests

- 执行 T01~T22 所属的全部测试。
- 必须保持：`test_order_timeline.py`、`test_mcp_rbac_wiring.py`、`test_auth_attestation.py`、`test_conversation_hooks.py`、`test_complex_analysis_orchestration.py`、`test_observability.py`、`test_function_documentation.py`。
- 不连接真实 Redis/MCP；完整 suite 继续使用 Fake/monkeypatch。

### 6. Acceptance Criteria Mapping

- AC1~AC13 → Phase 5 / Step 1 全量回归确认
- AC14 → Phase 5 / Step 1 差异边界检查
- AC15 → Phase 5 / Step 1 完整 pytest

AC1~AC15 在 Phase 1~4 都有实现落点，Phase 5 负责最终全量证明。

### 7. Verification

```bash
cd backend
uv run pytest
```

### 8. Risks

- 全量测试失败时只修复与本需求直接相关的问题，不借机重构无关模块。
- 仓库已有未提交用户修改，git diff 审查必须区分任务差异与既有差异。

## Acceptance Criteria Coverage Summary

| AC | 主要落点 |
|---|---|
| AC1 | Phase 3 / Step 1~2 |
| AC2 | Phase 2 / Step 3 + Phase 3 / Step 1 |
| AC3 | Phase 1 / Step 3 |
| AC4 | Phase 3 / Step 2 |
| AC5 | Phase 1 / Step 2~3 + Phase 3 / Step 2 |
| AC6 | Phase 2 / Step 1 |
| AC7 | Phase 2 / Step 2 |
| AC8 | Phase 2 / Step 3 |
| AC9 | Phase 1 / Step 1 + Phase 3 / Step 2 |
| AC10 | Phase 3 / Step 3 + Phase 4 / Step 1 |
| AC11 | Phase 1 / Step 2 + Phase 2 / Step 1 + Phase 3 / Step 3 |
| AC12 | Phase 3 / Step 4 |
| AC13 | Phase 4 / Step 2 |
| AC14 | Phase 1~5 边界检查 |
| AC15 | Phase 5 / Step 1 |

## Recommended Implementation Order

### Task 1 — Phase 1 / Step 1：建立 resilience 类型和 Tool 策略映射

- 文件：新增 `mcp_resilience.py`、新增 `test_mcp_resilience.py`、修改 `schemas.py`、修改 `test_mcp_rbac_wiring.py`。
- 测试：T22 + `MCPCallResult` 字段/默认值契约测试。

### Task 2 — Phase 1 / Step 2：实现单次调用和 Failure 分类

- 文件：`mcp_resilience.py`、`test_mcp_resilience.py`。
- 测试：T01、T05/T06/T07/T08/T09、T21 的 attempt 取消分支。

### Task 3 — Phase 1 / Step 3：实现超时重试和指数退避

- 文件：`mcp_resilience.py`、`test_mcp_resilience.py`。
- 测试：T02、T03、T21 的退避取消分支。

### Task 4 — Phase 2 / Step 1：实现 CLOSED 失败计数和 OPEN 快速失败

- 文件：`mcp_resilience.py`、`test_mcp_resilience.py`。
- 测试：T03 计数补充、T10、T11。

### Task 5 — Phase 2 / Step 2：实现 HALF_OPEN 跨进程探测租约

- 文件：`mcp_resilience.py`、`test_mcp_resilience.py`。
- 测试：T12、T13、T14。

### Task 6 — Phase 2 / Step 3：复用 Redis Client 并实现 Store fail-open

- 文件：`mcp_resilience.py`、`context_hooks.py`、`test_mcp_resilience.py`、`test_conversation_hooks.py`。
- 测试：T15、Redis Client 复用测试、缺 Redis 现有回归。

### Task 7 — Phase 3 / Step 1：在插件注册时注入唯一 Manager

- 文件：`__init__.py`、`bridge.py`、`test_conversation_hooks.py`、`test_mcp_resilience.py`。
- 测试：AC1/AC2 注入契约、缺 Redis 仍注册所有工具。

### Task 8 — Phase 3 / Step 2：接入真实 MCP attempt 并每次重签 JTI

- 文件：`bridge.py`、`test_mcp_resilience.py`。
- 测试：T04，以及 T01/T05/T06/T07/T08/T21 的 Bridge 集成分支。

### Task 9 — Phase 3 / Step 3：实现 L1/L2/L3 和 L2 empty 输出转换

- 文件：`bridge.py`、`test_mcp_resilience.py`。
- 测试：T09、T16、T17、T18。

### Task 10 — Phase 3 / Step 4：锁定 timeline partial 与桥接回归

- 文件：`test_mcp_resilience.py`，必要时仅扩展 `test_mcp_rbac_wiring.py`断言。
- 测试：T20、`test_order_timeline.py`、`test_auth_attestation.py`。

### Task 11 — Phase 4 / Step 1：在复杂分析中传递降级 notice

- 文件：`orchestration.py`、`test_complex_analysis_orchestration.py`。
- 测试：T19、聚合超时 fallback 回归。

### Task 12 — Phase 4 / Step 2：扩展现有 MCP Span 属性

- 文件：`observability.py`、`bridge.py`、`test_observability.py`。
- 测试：AC13 属性断言、现有 Trace 统计回归。

### Task 13 — Phase 5 / Step 1：同步文档并执行全量回归

- 文件：`backend/README.md`、`docs/05-技术重难点/05m-MCP调用失败重试与降级.md`；仅核对 `backend/.env.example`。
- 测试：T01~T22 和 `cd backend && uv run pytest`。

## Plan Summary

1. Implementation tasks 总数：**13**。
2. 第一个推荐执行的 Task：**Task 1 — 建立 resilience 类型和 Tool 策略映射**。
3. Task 1 修改文件：新增 `.hermes/plugins/suning-rbac-bridge/mcp_resilience.py`、新增 `backend/tests/test_mcp_resilience.py`、修改 `.hermes/plugins/suning-rbac-bridge/schemas.py`、修改 `backend/tests/test_mcp_rbac_wiring.py`。
4. Task 1 对应测试：**T22**，以及 `FailureType/DegradeLevel/CircuitState/MCPCallResult` 字段与默认值契约测试。
