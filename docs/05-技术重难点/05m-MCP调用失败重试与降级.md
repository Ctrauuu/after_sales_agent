# 05m · MCP 调用失败的重试与降级策略

> Agent 调用 MCP Tool 时可能遇到超时、连接失败、权限拒绝、远端工具错误、空结果或响应格式错误。
> 本实现把重试、熔断和降级集中在 Hermes 插件的 MCP 调用边界，避免单个依赖故障中断整个分析。

## 为什么难

1. 不同故障的处理不同：只有超时且 Tool 明确允许时才能重试，权限、远端业务错误和格式错误不能盲目重试。
2. 熔断状态需要在多个 Hermes 进程间共享，半开探测还必须跨进程互斥。
3. 每次重试都必须重新签发 attestation/JTI，否则 MCP Server 的防重放校验会拒绝请求。
4. “查询成功但为空”与“服务失败导致不可用”必须保持不同语义，并在子 Agent 聚合后继续可见。

## 实际调用链

```text
plugin register
  → 复用 Redis Client
  → 创建并复用 MCPCallManager
  → make_handler(manager)
  → invoke_business_tool(...)
  → MCPCallManager.call(tool_spec, attempt)
  → circuit 检查
  → attempt 内重新获取身份、签发 attestation 并调用 ClientSession.call_tool
  → MCPCallResult
  → Hermes tool_result / tool_error
```

`MCPCallManager` 不感知 Hermes Tool Registry、用户身份或 attestation schema；这些职责仍由
`bridge.py` 承担。MCP Server 的 `@mcp.tool` 业务实现不包含 retry、circuit breaker 或 degrade。

## 重试和故障分类

- 单次调用超时为 10 秒。
- 只有 `FailureType.TIMEOUT` 且 `ToolSpec.retry_on_timeout=true` 时重试。
- 最多额外重试 3 次，退避为 1 秒、2 秒、4 秒，并附加 0～200ms jitter。
- 每个真实 attempt 都重新签发 attestation，因此不会复用 JTI。
- `CONNECTION_REFUSED`、`PERMISSION_DENIED`、`REMOTE_TOOL_ERROR`、
  `MALFORMED_RESPONSE`、`EMPTY_RESULT` 和 `UNKNOWN` 不重试。
- `asyncio.CancelledError` 始终继续向上抛出。

合法空结果由 `ToolSpec.empty_result_is_success` 声明。成功返回的
`trace_order_timeline.partial=true` 及其 `source_failures` 属于时间线内部的部分结果，不会被顶层
Manager 认定为 MCP Server 失败。

## Redis 熔断状态

熔断粒度是稳定的逻辑 `server_id`，不按 Tool、用户或 endpoint URL 拆分。

- 60 秒窗口内累计 5 次可计数失败后进入 `OPEN`。
- `OPEN` 冷却 30 秒，状态 TTL 为 120 秒。
- 冷却后通过 15 秒的 Redis `SET NX` lease 只允许一个跨进程 `HALF_OPEN` probe。
- probe 成功回到 `CLOSED`；失败重新 `OPEN` 30 秒；probe 本身不重试。
- 仅 `TIMEOUT`、`CONNECTION_REFUSED`、`MALFORMED_RESPONSE` 计入熔断。
- Redis 操作通过 `asyncio.to_thread()` 执行；Redis 故障时 fail-open，且不计为 MCP Server 故障。

## Tool 策略与降级

每个 MCP Tool 的 `server_id`、`degrade_level`、`retry_on_timeout` 和空结果语义只在
`schemas.py` 的 `TOOL_SPECS` 中声明，不在 bridge 或 Manager 中按工具名重复映射。

- L1（非核心）：返回结构化 `tool_result`，`available=false`，其他分析继续。
- L2（核心）：同样返回 `tool_result`，并明确提示对应维度暂不可用、当前结果不完整；子 Agent
  保持成功状态，摘要和最终聚合报告保留 notice。
- L3（关键）：返回安全的 `tool_error`。当前没有实际配置为 L3 的 Tool。

Agent 输出只包含安全 notice，不泄露内部异常、attestation、JTI、SQL、credential 或 stack trace。

## 可观测性

现有 MCP Span 会记录 `retry_count`、`failure_type`、`circuit_state`、`degrade_level`、最终
`success` 和 `degraded`。实现复用项目原有 telemetry client，没有新增观测后端。

## 实现位置

- `.hermes/plugins/suning-rbac-bridge/mcp_resilience.py`：基础类型、调用管理器及 Redis 熔断状态。
- `.hermes/plugins/suning-rbac-bridge/schemas.py`：MCP Tool 策略声明。
- `.hermes/plugins/suning-rbac-bridge/__init__.py`：Redis Client 和 Manager 的注册期生命周期。
- `.hermes/plugins/suning-rbac-bridge/bridge.py`：真实 MCP attempt、结果校验及 Hermes 返回转换。
- `.hermes/plugins/suning-rbac-bridge/orchestration.py`：子 Agent 摘要和聚合报告的降级语义。
- `.hermes/plugins/suning-rbac-bridge/observability.py`：resilience Span 属性。
- `backend/tests/test_mcp_resilience.py`、`test_mcp_rbac_wiring.py`、
  `test_complex_analysis_orchestration.py`、`test_observability.py`：T01～T22 及相关回归覆盖。

本实现复用已有 `REDIS_URL` 和 MCP endpoint 配置，没有新增环境变量或第三方依赖。

## 涉及业务模块

- M3 · MCP 数据网关
- 复杂售后分析及其子 Agent 聚合链路
- M8 · 定时报告推送（调用同一桥接边界时沿用相同降级语义）
