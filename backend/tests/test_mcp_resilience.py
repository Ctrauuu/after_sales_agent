"""验证苏宁 MCP resilience 基础类型和静态 Tool 策略。"""

import asyncio
import importlib.util
import sys
import threading
from dataclasses import fields
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest


PLUGIN_ROOT = (
    Path(__file__).resolve().parents[2]
    / ".hermes"
    / "plugins"
    / "suning-rbac-bridge"
)


def _load_plugin_modules() -> tuple[ModuleType, ModuleType]:
    """输入：插件目录中的 resilience 和 schema 源文件。

    输出：按临时 package 加载的 ``mcp_resilience`` 与 ``schemas`` 模块。
    功能：在未安装 Hermes 插件包的测试环境中保留相对导入语义。
    """

    package_name = "_suning_rbac_bridge_resilience_test"
    package = ModuleType(package_name)
    package.__path__ = [str(PLUGIN_ROOT)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package

    loaded: list[ModuleType] = []
    for filename in ("mcp_resilience", "schemas"):
        module_name = f"{package_name}.{filename}"
        spec = importlib.util.spec_from_file_location(
            module_name,
            PLUGIN_ROOT / f"{filename}.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        loaded.append(module)
    return loaded[0], loaded[1]


RESILIENCE, SCHEMAS = _load_plugin_modules()


def _fake_tool_result(value: Any) -> tuple[str, Any]:
    """输入：Bridge 交给 Hermes 的成功或降级 payload。

    输出：带 ``result`` 标记的可断言二元组。
    功能：替代 Hermes registry 的 tool_result，保留原始 payload 供单测检查。
    """

    return "result", value


def _fake_tool_error(value: Any) -> tuple[str, Any]:
    """输入：Bridge 交给 Hermes 的安全错误文本。

    输出：带 ``error`` 标记的可断言二元组。
    功能：替代 Hermes registry 的 tool_error，区分 L3 与可继续降级结果。
    """

    return "error", value


def _load_bridge(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """输入：pytest monkeypatch 与 Task 1 创建的临时插件 package。

    输出：使用 Hermes gateway/tools 替身加载的真实 Bridge 模块。
    功能：在后端测试环境执行 Phase 3 生产转换和 attempt 代码。
    """

    gateway = ModuleType("gateway")
    gateway.__path__ = []  # type: ignore[attr-defined]
    session_context = ModuleType("gateway.session_context")
    session_context.get_session_env = Mock(return_value="")  # type: ignore[attr-defined]
    tools = ModuleType("tools")
    tools.__path__ = []  # type: ignore[attr-defined]
    registry = ModuleType("tools.registry")
    registry.tool_result = _fake_tool_result  # type: ignore[attr-defined]
    registry.tool_error = _fake_tool_error  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.registry", registry)

    module_name = f"{RESILIENCE.__package__}.bridge"
    spec = importlib.util.spec_from_file_location(
        module_name,
        PLUGIN_ROOT / "bridge.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bridge_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """输入：自动回滚模块替身的 pytest monkeypatch fixture。

    输出：加载完成并配置固定身份、环境和观测替身的 Bridge 模块。
    功能：为 Phase 3 测试提供不连接 Hermes、MCP 或 OTel 的统一边界。
    """

    monkeypatch.setenv(
        "SUNING_MCP_BRIDGE_SECRET",
        "test-secret-with-at-least-thirty-two-bytes",
    )
    monkeypatch.setenv("SUNING_IDENTITY_ISSUER", "suning-feishu-primary")
    bridge = _load_bridge(monkeypatch)
    monkeypatch.setattr(
        bridge,
        "current_identity",
        Mock(
            return_value={
                "platform": "feishu",
                "external_subject": "ou_test",
                "chat_type": "dm",
                "message_id": "om_test",
            }
        ),
    )
    observability = Mock()
    observability.ensure_trace.return_value = (
        SimpleNamespace(trace_id="trace-test"),
        False,
    )
    observability.start_mcp_span.return_value = object()
    monkeypatch.setattr(bridge, "observability", observability)
    monkeypatch.setattr(bridge, "_result_rows", Mock(return_value=1))
    return bridge


class FakeStreamContext:
    """返回固定 read/write/session_id 的异步 MCP transport context。"""

    async def __aenter__(self) -> tuple[object, object, str]:
        """输入：无；不建立真实 HTTP 连接。

        输出：供 ClientSession 构造的三个固定 transport 值。
        功能：模拟 ``streamable_http_client`` 进入上下文。
        """

        return object(), object(), "session-test"

    async def __aexit__(self, *_args: Any) -> None:
        """输入：异步上下文退出时的异常信息。

        输出：无；不吞掉异常。
        功能：模拟关闭 MCP transport，不执行外部副作用。
        """


class FakeClientSession:
    """消费 Factory outcomes 并记录每次 call_tool metadata 的会话替身。"""

    def __init__(self, factory: "FakeClientSessionFactory") -> None:
        """输入：共享结果序列和 metadata 记录器的 Factory。

        输出：绑定 Factory 的单次 MCP Session。
        功能：让每次 retry 创建新 Session，同时共享断言数据。
        """

        self.factory = factory

    async def __aenter__(self) -> "FakeClientSession":
        """输入：无；Session 已由 Factory 构造。

        输出：当前 Session 替身。
        功能：模拟 ``ClientSession`` 异步进入。
        """

        return self

    async def __aexit__(self, *_args: Any) -> None:
        """输入：异步上下文退出时的异常信息。

        输出：无；不吞掉异常。
        功能：模拟关闭 ClientSession。
        """

    async def initialize(self) -> None:
        """输入：无；不执行真实 MCP 握手。

        输出：无。
        功能：保留 Bridge attempt 的 initialize 调用顺序。
        """

    async def call_tool(
        self,
        _tool_name: str,
        *,
        arguments: dict[str, Any],
        meta: dict[str, Any],
    ) -> Any:
        """输入：Tool 名、业务参数和本次新签发的 metadata。

        输出：Factory 下一个结果；异常 outcome 原样抛出。
        功能：记录 attestation 并驱动 timeout retry 的真实 Bridge attempt。
        """

        self.factory.arguments.append(arguments)
        self.factory.metadata.append(dict(meta))
        outcome = self.factory.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClientSessionFactory:
    """为每次 Bridge retry 创建独立 FakeClientSession。"""

    def __init__(self, *outcomes: Any) -> None:
        """输入：每次 call_tool 依次返回或抛出的 outcomes。

        输出：空 metadata/arguments 记录和结果队列。
        功能：验证每个 attempt 都重新建 Session 并携带新凭证。
        """

        self.outcomes = list(outcomes)
        self.metadata: list[dict[str, Any]] = []
        self.arguments: list[dict[str, Any]] = []

    def __call__(self, _read: Any, _write: Any) -> FakeClientSession:
        """输入：Bridge transport 提供的 read/write 对象。

        输出：绑定当前 Factory 的新 Session。
        功能：模拟生产 ``ClientSession(read, write)`` 构造。
        """

        return FakeClientSession(self)


def _response(
    *,
    structured: Any = None,
    text: str | None = None,
    is_error: bool = False,
) -> SimpleNamespace:
    """输入：可选结构化负载、文本负载和 MCP 错误标记。

    输出：与 ``CallToolResult`` 必要字段一致的轻量测试响应。
    功能：构造无需真实 MCP 依赖的 Manager attempt 返回值。
    """

    content = [] if text is None else [SimpleNamespace(text=text)]
    return SimpleNamespace(
        structuredContent=structured,
        content=content,
        isError=is_error,
    )


class SequenceAttempt:
    """按顺序返回结果或抛出异常的异步 MCP attempt 替身。"""

    def __init__(self, *outcomes: Any) -> None:
        """输入：依次消费的返回值或异常对象。

        输出：记录零次调用的 callable 测试替身。
        功能：为重试和 FailureType 测试提供确定的 attempt 序列。
        """

        self.outcomes = list(outcomes)
        self.calls = 0

    async def __call__(self) -> Any:
        """输入：构造时保存的下一个 outcome。

        输出：普通 outcome；异常 outcome 原样抛出。
        功能：记录真实 attempt 次数并推进测试序列。
        """

        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class BlockingAttempt:
    """由事件控制完成时机的 HALF_OPEN probe 替身。"""

    def __init__(self, result: Any) -> None:
        """输入：解除阻塞后返回的 MCP 结果。

        输出：带 started/release 事件和调用计数的异步替身。
        功能：稳定复现一个 probe 尚未完成时第二个进程竞争租约的场景。
        """

        self.result = result
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def __call__(self) -> Any:
        """输入：由测试控制的 ``release`` 事件。

        输出：事件释放后返回构造时的 MCP 结果。
        功能：标记 probe 已开始，并保持租约直到并发断言完成。
        """

        self.calls += 1
        self.started.set()
        await self.release.wait()
        return self.result


class FakeCircuitRedis:
    """线程安全模拟 CircuitStore 所需的同步 Redis 原语。"""

    def __init__(self) -> None:
        """输入：无；不访问真实 Redis。

        输出：空键空间、TTL 记录和 INCR 计数器。
        功能：支持 get/set/incr/expire/delete/eval 及并发 probe 单测。
        """

        self.values: dict[str, Any] = {}
        self.expirations: dict[str, int] = {}
        self.incr_calls = 0
        self.lock = threading.RLock()

    def get(self, key: str) -> Any:
        """输入：Redis 键 ``key``。

        输出：内存值；键不存在时返回 ``None``。
        功能：模拟同步 Redis GET。
        """

        with self.lock:
            return self.values.get(key)

    def set(
        self,
        key: str,
        value: Any,
        *,
        ex: int,
        nx: bool = False,
    ) -> bool:
        """输入：键、值、TTL 和可选 NX 标记。

        输出：写入成功为 ``True``；NX 冲突为 ``False``。
        功能：模拟带 EX/NX 的同步 Redis SET。
        """

        with self.lock:
            if nx and key in self.values:
                return False
            self.values[key] = value
            self.expirations[key] = ex
            return True

    def incr(self, key: str) -> int:
        """输入：失败计数 Redis 键。

        输出：原子加一后的整数。
        功能：模拟 Redis INCR，并记录逻辑失败实际累计次数。
        """

        with self.lock:
            self.incr_calls += 1
            value = int(self.values.get(key, 0)) + 1
            self.values[key] = value
            return value

    def expire(self, key: str, seconds: int) -> bool:
        """输入：已有 Redis 键及 TTL 秒数。

        输出：键存在时为 ``True``，否则为 ``False``。
        功能：记录失败窗口 TTL，不模拟真实时间流逝。
        """

        with self.lock:
            if key not in self.values:
                return False
            self.expirations[key] = seconds
            return True

    def delete(self, *keys: str) -> int:
        """输入：一个或多个待删除 Redis 键。

        输出：实际删除的键数量。
        功能：同步清理值和对应 TTL 记录。
        """

        with self.lock:
            deleted = 0
            for key in keys:
                deleted += key in self.values
                self.values.pop(key, None)
                self.expirations.pop(key, None)
            return deleted

    def eval(self, script: str, numkeys: int, *args: Any) -> int:
        """输入：CircuitStore probe Lua、键数量、四个键和 owner 参数。

        输出：owner 匹配且完成状态转换时为正数，否则为零。
        功能：原子模拟 HALF_OPEN probe 成功关闭或失败重新 OPEN。
        """

        keys = args[:numkeys]
        arguments = args[numkeys:]
        owner = arguments[0]
        with self.lock:
            if self.values.get(keys[3]) != owner:
                return 0
            if "probe_success" in script:
                return self.delete(*keys)
            self.values[keys[1]] = "open"
            self.expirations[keys[1]] = int(arguments[1])
            self.values[keys[2]] = "open"
            self.expirations[keys[2]] = int(arguments[2])
            self.delete(keys[0], keys[3])
            return 1


class BrokenRedis:
    """对 CircuitStore 读写均抛出连接错误的 Redis 替身。"""

    def get(self, _key: str) -> None:
        """输入：任意 Redis 键。

        输出：不返回，始终抛出 ``ConnectionError``。
        功能：模拟熔断状态读取期间 Redis 不可用。
        """

        raise ConnectionError("redis unavailable")

    def delete(self, *_keys: str) -> None:
        """输入：任意 Redis 键集合。

        输出：不返回，始终抛出 ``ConnectionError``。
        功能：模拟真实 MCP 成功后清理熔断计数失败。
        """

        raise ConnectionError("redis unavailable")


def _key(suffix: str, server_id: str = "mcp-order") -> str:
    """输入：熔断键后缀和可选稳定 Server ID。

    输出：与生产 CircuitStore 一致的完整 Redis Key。
    功能：让状态机测试直接准备和检查 Fake Redis 键空间。
    """

    return f"suning:mcp:cb:{server_id}:{suffix}"


async def _manager_call(
    manager: Any,
    attempt: Any,
    *,
    retry_on_timeout: bool = True,
    empty_result_is_success: bool = False,
) -> Any:
    """输入：Manager、async attempt 以及可覆盖的超时和空结果策略。

    输出：``search_orders`` 对应的 MCPCallResult。
    功能：集中测试调用的固定 ToolSpec 参数，避免每个 T Case 重复装配。
    """

    return await manager.call(
        server_id="mcp-order",
        tool_name="search_orders",
        degrade_level=RESILIENCE.DegradeLevel.L2_CORE,
        retry_on_timeout=retry_on_timeout,
        empty_result_is_success=empty_result_is_success,
        attempt=attempt,
    )


def test_failure_type_enum_contract() -> None:
    """输入：Spec 定义的 MCP 失败类型集合。

    输出：无；枚举成员缺失、多余或字符串值变化时断言失败。
    功能：锁定 FailureType 的对外契约，避免后续分类产生非标准值。
    """

    assert {member.value for member in RESILIENCE.FailureType} == {
        "TIMEOUT",
        "CONNECTION_REFUSED",
        "PERMISSION_DENIED",
        "EMPTY_RESULT",
        "MALFORMED_RESPONSE",
        "REMOTE_TOOL_ERROR",
        "CIRCUIT_OPEN",
        "UNKNOWN",
    }


def test_degrade_level_enum_contract() -> None:
    """输入：Spec 定义的三个 MCP 降级等级。

    输出：无；等级成员或字符串值偏离 Spec 时断言失败。
    功能：锁定 L1、L2、L3 降级等级的序列化契约。
    """

    assert {member.value for member in RESILIENCE.DegradeLevel} == {
        "L1_NON_CRITICAL",
        "L2_CORE",
        "L3_CRITICAL",
    }


def test_circuit_state_enum_contract() -> None:
    """输入：Spec 定义的熔断器状态集合。

    输出：无；状态成员或字符串值偏离 Spec 时断言失败。
    功能：锁定 CLOSED、OPEN、HALF_OPEN 三态契约，不实现状态转换。
    """

    assert {member.value for member in RESILIENCE.CircuitState} == {
        "CLOSED",
        "OPEN",
        "HALF_OPEN",
    }


def test_mcp_call_result_fields_and_defaults() -> None:
    """输入：最小必填的 Server、Tool、成功状态和降级等级。

    输出：无；字段集合或结果元数据默认值不符合 Spec 时断言失败。
    功能：验证 MCPCallResult 保留完整结果契约并提供安全的初始状态。
    """

    result = RESILIENCE.MCPCallResult(
        server_id="mcp-order",
        tool_name="search_orders",
        success=False,
        degrade_level=RESILIENCE.DegradeLevel.L2_CORE,
    )

    assert [field.name for field in fields(result)] == [
        "server_id",
        "tool_name",
        "success",
        "data",
        "error_type",
        "error_message",
        "retry_count",
        "total_latency_ms",
        "degraded",
        "degrade_level",
        "degrade_note",
        "circuit_state",
    ]
    assert result.data is None
    assert result.error_type is None
    assert result.error_message == ""
    assert result.retry_count == 0
    assert result.total_latency_ms == 0.0
    assert result.degraded is False
    assert result.degrade_note == ""
    assert result.circuit_state is RESILIENCE.CircuitState.CLOSED


def test_retry_policy_constants_match_spec() -> None:
    """输入：Task 1 声明的固定 retry policy 常量。

    输出：无；重试次数、超时、退避或抖动常量偏离 Spec 时断言失败。
    功能：只验证静态策略值，不执行任何重试行为。
    """

    assert RESILIENCE.MAX_RETRIES == 3
    assert RESILIENCE.ATTEMPT_TIMEOUT_SECONDS == 10
    assert RESILIENCE.BASE_BACKOFF_SECONDS == 1
    assert RESILIENCE.MAX_JITTER_SECONDS == 0.2


def test_tool_policy_fields_can_be_declared_and_read() -> None:
    """输入：一个显式声明全部 resilience 字段的 ToolSpec。

    输出：无；策略字段无法构造或读取时断言失败。
    功能：验证 ToolSpec 以稳定 Server ID 和显式策略表达调用语义。
    """

    tool_spec = SCHEMAS.ToolSpec(
        server_id="mcp-order",
        degrade_level=RESILIENCE.DegradeLevel.L2_CORE,
        retry_on_timeout=True,
        empty_result_is_success=True,
        endpoint_env="SUNING_MCP_ORDER_URL",
        default_endpoint="http://127.0.0.1:8101/mcp",
        schema={"name": "search_orders"},
    )

    assert tool_spec.server_id == "mcp-order"
    assert tool_spec.degrade_level is RESILIENCE.DegradeLevel.L2_CORE
    assert tool_spec.retry_on_timeout is True
    assert tool_spec.empty_result_is_success is True


@pytest.mark.asyncio
async def test_t01_first_attempt_succeeds() -> None:
    """输入：首次返回结构化 MCP 结果的 attempt。

    输出：无；结果不成功、发生重试或数据被改写时断言失败。
    功能：覆盖 T01，验证无 Redis Manager 的最短成功路径。
    """

    response = _response(structured={"orders": [{"order_id": 1}]})
    attempt = SequenceAttempt(response)

    result = await _manager_call(RESILIENCE.MCPCallManager(), attempt)

    assert result.success is True
    assert result.data is response
    assert result.error_type is None
    assert result.retry_count == 0
    assert result.circuit_state is RESILIENCE.CircuitState.CLOSED
    assert attempt.calls == 1


@pytest.mark.asyncio
async def test_t02_two_timeouts_then_success_use_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：两次超时后成功的 attempt，以及 sleep/jitter 替身。

    输出：无；调用次数、重试计数或 1s/2s 退避不正确时断言失败。
    功能：覆盖 T02，不进行真实等待地验证 timeout retry 主流程。
    """

    sleep = AsyncMock()
    monkeypatch.setattr(RESILIENCE.asyncio, "sleep", sleep)
    monkeypatch.setattr(RESILIENCE.random, "uniform", Mock(return_value=0.1))
    attempt = SequenceAttempt(
        TimeoutError("first"),
        TimeoutError("second"),
        _response(structured={"ok": True}),
    )

    result = await _manager_call(RESILIENCE.MCPCallManager(), attempt)

    assert result.success is True
    assert result.retry_count == 2
    assert attempt.calls == 3
    assert [call.args[0] for call in sleep.await_args_list] == [1.1, 2.1]


@pytest.mark.asyncio
async def test_t03_four_timeouts_count_one_logical_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：连续四次超时、Fake Redis 和无等待退避替身。

    输出：无；未耗尽三次重试或 INCR 多于一次时断言失败。
    功能：覆盖 T03，确保 attempt 次数不放大 Server 熔断失败计数。
    """

    redis_client = FakeCircuitRedis()
    monkeypatch.setattr(RESILIENCE.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(RESILIENCE.random, "uniform", Mock(return_value=0.0))
    attempt = SequenceAttempt(*(TimeoutError("slow") for _ in range(4)))

    result = await _manager_call(
        RESILIENCE.MCPCallManager(redis_client),
        attempt,
    )

    assert result.success is False
    assert result.error_type is RESILIENCE.FailureType.TIMEOUT
    assert result.retry_count == 3
    assert attempt.calls == 4
    assert redis_client.incr_calls == 1
    assert redis_client.get(_key("failures")) == 1
    assert redis_client.expirations[_key("failures")] == 60


@pytest.mark.asyncio
async def test_timeout_retry_can_be_disabled_by_tool_policy() -> None:
    """输入：显式关闭 timeout retry 的 Tool 策略和首次超时 attempt。

    输出：无；Manager 继续第二次 attempt 或重试计数非零时断言失败。
    功能：验证幂等性由 ToolSpec 策略传入，不根据工具名称推断。
    """

    attempt = SequenceAttempt(
        TimeoutError("slow"),
        _response(structured={"unexpected": True}),
    )

    result = await _manager_call(
        RESILIENCE.MCPCallManager(),
        attempt,
        retry_on_timeout=False,
    )

    assert result.error_type is RESILIENCE.FailureType.TIMEOUT
    assert result.retry_count == 0
    assert attempt.calls == 1


@pytest.mark.asyncio
async def test_t05_connection_refused_is_not_retried_and_is_counted() -> None:
    """输入：首次抛出 ConnectionRefusedError 的 attempt 和 Fake Redis。

    输出：无；分类、调用次数或熔断计数不符合 Spec 时断言失败。
    功能：覆盖 T05，验证明确连接拒绝直接产生一次可计数失败。
    """

    redis_client = FakeCircuitRedis()
    attempt = SequenceAttempt(ConnectionRefusedError("refused"))

    result = await _manager_call(
        RESILIENCE.MCPCallManager(redis_client),
        attempt,
    )

    assert result.error_type is RESILIENCE.FailureType.CONNECTION_REFUSED
    assert result.retry_count == 0
    assert attempt.calls == 1
    assert redis_client.get(_key("failures")) == 1


@pytest.mark.asyncio
async def test_t06_permission_denied_is_not_retried_or_counted() -> None:
    """输入：首次抛出 PermissionError 的 attempt 和 Fake Redis。

    输出：无；权限分类被重试或写入失败计数时断言失败。
    功能：覆盖 T06，保持身份权限问题与 Server 健康度隔离。
    """

    redis_client = FakeCircuitRedis()
    attempt = SequenceAttempt(PermissionError("denied"))

    result = await _manager_call(
        RESILIENCE.MCPCallManager(redis_client),
        attempt,
    )

    assert result.error_type is RESILIENCE.FailureType.PERMISSION_DENIED
    assert result.retry_count == 0
    assert attempt.calls == 1
    assert redis_client.get(_key("failures")) is None


@pytest.mark.asyncio
async def test_t07_remote_tool_error_is_not_retried_or_counted() -> None:
    """输入：带 ``isError=true`` 的 MCP Result 和 Fake Redis。

    输出：无；远端业务拒绝被重试或计入熔断时断言失败。
    功能：覆盖 T07，将正常协议错误归类为 REMOTE_TOOL_ERROR。
    """

    redis_client = FakeCircuitRedis()
    attempt = SequenceAttempt(_response(text="business rejected", is_error=True))

    result = await _manager_call(
        RESILIENCE.MCPCallManager(redis_client),
        attempt,
    )

    assert result.error_type is RESILIENCE.FailureType.REMOTE_TOOL_ERROR
    assert attempt.calls == 1
    assert redis_client.get(_key("failures")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [_response(), _response(text="not-json")],
    ids=["missing-content", "invalid-json"],
)
async def test_t08_malformed_response_is_counted(response: Any) -> None:
    """输入：无可用 content 或包含非法 JSON 的 MCP Result。

    输出：无；未分类为可计数 MALFORMED_RESPONSE 时断言失败。
    功能：覆盖 T08 的两种协议脏响应，确认都不执行 retry。
    """

    redis_client = FakeCircuitRedis()
    attempt = SequenceAttempt(response)

    result = await _manager_call(
        RESILIENCE.MCPCallManager(redis_client),
        attempt,
    )

    assert result.error_type is RESILIENCE.FailureType.MALFORMED_RESPONSE
    assert result.retry_count == 0
    assert attempt.calls == 1
    assert redis_client.get(_key("failures")) == 1


@pytest.mark.asyncio
async def test_t09_declared_empty_result_is_success_and_clears_failures() -> None:
    """输入：允许空结果的 Tool 策略、JSON 空列表响应及已有失败计数。

    输出：无；空查询失败、降级或未清除计数时断言失败。
    功能：覆盖 T09，区分合法 EMPTY_RESULT 与 Server 故障。
    """

    redis_client = FakeCircuitRedis()
    redis_client.set(_key("failures"), 2, ex=60)
    response = _response(text="[]")

    result = await _manager_call(
        RESILIENCE.MCPCallManager(redis_client),
        SequenceAttempt(response),
        empty_result_is_success=True,
    )

    assert result.success is True
    assert result.data is response
    assert result.error_type is RESILIENCE.FailureType.EMPTY_RESULT
    assert result.degraded is False
    assert redis_client.get(_key("failures")) is None


@pytest.mark.asyncio
async def test_t10_fifth_countable_failure_opens_server_circuit() -> None:
    """输入：同一 server_id 的五次 CONNECTION_REFUSED 逻辑调用。

    输出：无；OPEN 键、TTL 或失败计数清理不正确时断言失败。
    功能：覆盖 T10，验证 60 秒窗口阈值按 Server 而不是 Tool 累计。
    """

    redis_client = FakeCircuitRedis()
    manager = RESILIENCE.MCPCallManager(redis_client)

    for index in range(5):
        result = await _manager_call(
            manager,
            SequenceAttempt(ConnectionRefusedError(str(index))),
        )
        assert result.error_type is RESILIENCE.FailureType.CONNECTION_REFUSED

    assert redis_client.get(_key("failures")) is None
    assert redis_client.get(_key("state")) == "open"
    assert redis_client.get(_key("cooldown")) == "open"
    assert redis_client.expirations[_key("state")] == 120
    assert redis_client.expirations[_key("cooldown")] == 30


@pytest.mark.asyncio
async def test_t11_open_circuit_skips_real_attempt() -> None:
    """输入：具有 state 和 cooldown 键的 OPEN Server 及成功 attempt。

    输出：无；真实闭包被执行或结果未标记 CIRCUIT_OPEN 时断言失败。
    功能：覆盖 T11，在冷却期快速失败且不发起网络调用。
    """

    redis_client = FakeCircuitRedis()
    redis_client.set(_key("state"), "open", ex=120)
    redis_client.set(_key("cooldown"), "open", ex=30)
    attempt = SequenceAttempt(_response(structured={"ok": True}))

    result = await _manager_call(
        RESILIENCE.MCPCallManager(redis_client),
        attempt,
    )

    assert result.error_type is RESILIENCE.FailureType.CIRCUIT_OPEN
    assert result.circuit_state is RESILIENCE.CircuitState.OPEN
    assert result.retry_count == 0
    assert attempt.calls == 0


@pytest.mark.asyncio
async def test_t12_half_open_allows_only_one_concurrent_probe() -> None:
    """输入：cooldown 已结束的 HALF_OPEN Server 和两个并发 Manager。

    输出：无；超过一个 attempt 获得 probe lease 时断言失败。
    功能：覆盖 T12，验证 Redis SET NX 在跨 Manager 场景只放行一个探测。
    """

    redis_client = FakeCircuitRedis()
    redis_client.set(_key("state"), "open", ex=120)
    first_attempt = BlockingAttempt(_response(structured={"ok": True}))
    first_task = asyncio.create_task(
        _manager_call(RESILIENCE.MCPCallManager(redis_client), first_attempt)
    )
    await first_attempt.started.wait()

    second_attempt = SequenceAttempt(_response(structured={"ok": True}))
    second_result = await _manager_call(
        RESILIENCE.MCPCallManager(redis_client),
        second_attempt,
    )
    first_attempt.release.set()
    first_result = await first_task

    assert first_result.success is True
    assert second_result.error_type is RESILIENCE.FailureType.CIRCUIT_OPEN
    assert first_attempt.calls == 1
    assert second_attempt.calls == 0


@pytest.mark.asyncio
async def test_t13_half_open_probe_success_closes_circuit() -> None:
    """输入：HALF_OPEN Server、已有失败键和成功 probe。

    输出：无；四类熔断键未全部清理时断言失败。
    功能：覆盖 T13，用 owner 校验后的原子操作恢复 CLOSED 状态。
    """

    redis_client = FakeCircuitRedis()
    redis_client.set(_key("state"), "open", ex=120)
    redis_client.set(_key("failures"), 4, ex=60)

    result = await _manager_call(
        RESILIENCE.MCPCallManager(redis_client),
        SequenceAttempt(_response(structured={"ok": True})),
    )

    assert result.success is True
    assert result.circuit_state is RESILIENCE.CircuitState.HALF_OPEN
    assert all(
        redis_client.get(_key(suffix)) is None
        for suffix in ("failures", "state", "cooldown", "probe")
    )


@pytest.mark.asyncio
async def test_t14_half_open_probe_failure_reopens_without_retry() -> None:
    """输入：HALF_OPEN Server 和一次连接拒绝 probe。

    输出：无；probe 被重试、未释放或 OPEN TTL 未重置时断言失败。
    功能：覆盖 T14，验证失败探测原子恢复 30 秒冷却期。
    """

    redis_client = FakeCircuitRedis()
    redis_client.set(_key("state"), "open", ex=120)
    attempt = SequenceAttempt(ConnectionRefusedError("probe failed"))

    result = await _manager_call(
        RESILIENCE.MCPCallManager(redis_client),
        attempt,
    )

    assert result.error_type is RESILIENCE.FailureType.CONNECTION_REFUSED
    assert result.retry_count == 0
    assert attempt.calls == 1
    assert redis_client.get(_key("state")) == "open"
    assert redis_client.get(_key("cooldown")) == "open"
    assert redis_client.get(_key("probe")) is None
    assert redis_client.expirations[_key("state")] == 120
    assert redis_client.expirations[_key("cooldown")] == 30


@pytest.mark.asyncio
async def test_t15_redis_failure_is_fail_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """输入：所有读写均失败的 Redis 和首次成功的真实 attempt。

    输出：无；MCP 调用被 Redis 中断或缺少告警时断言失败。
    功能：覆盖 T15，确保 Store 故障不伪装成 MCP Server failure。
    """

    attempt = SequenceAttempt(_response(structured={"ok": True}))

    with caplog.at_level("WARNING"):
        result = await _manager_call(
            RESILIENCE.MCPCallManager(BrokenRedis()),
            attempt,
        )

    assert result.success is True
    assert attempt.calls == 1
    assert "circuit_store_unavailable" in caplog.text


@pytest.mark.asyncio
async def test_t21_cancelled_error_propagates_from_attempt_and_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：attempt 取消和首次超时后的 backoff 取消两个场景。

    输出：无；CancelledError 被转换或取消后继续 attempt 时断言失败。
    功能：覆盖 T21，保留上层任务取消语义并立即终止重试。
    """

    cancelled_attempt = SequenceAttempt(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await _manager_call(RESILIENCE.MCPCallManager(), cancelled_attempt)
    assert cancelled_attempt.calls == 1

    backoff_attempt = SequenceAttempt(
        TimeoutError("slow"),
        _response(structured={"unexpected": True}),
    )
    monkeypatch.setattr(
        RESILIENCE.asyncio,
        "sleep",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )
    with pytest.raises(asyncio.CancelledError):
        await _manager_call(RESILIENCE.MCPCallManager(), backoff_attempt)
    assert backoff_attempt.calls == 1


@pytest.mark.asyncio
async def test_t04_bridge_retry_mints_a_new_attestation_per_attempt(
    bridge_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：首次超时、第二次成功的真实 Bridge attempt 和两个固定 token。

    输出：无；retry metadata 复用 token/JTI 或成功 payload 改变时断言失败。
    功能：覆盖 T04，验证 attestation 在 attempt 内每次重新签发。
    """

    response = _response(structured={"orders": [{"order_id": 1}]})
    sessions = FakeClientSessionFactory(TimeoutError("slow"), response)
    mint_attestation = Mock(side_effect=["token-jti-a", "token-jti-b"])
    monkeypatch.setattr(bridge_module, "mint_attestation", mint_attestation)
    monkeypatch.setattr(
        bridge_module,
        "streamable_http_client",
        Mock(return_value=FakeStreamContext()),
    )
    monkeypatch.setattr(bridge_module, "ClientSession", sessions)
    monkeypatch.setattr(RESILIENCE.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(RESILIENCE.random, "uniform", Mock(return_value=0.0))

    output = await bridge_module.invoke_business_tool(
        "search_orders",
        {"limit": 1},
        RESILIENCE.MCPCallManager(),
    )

    assert output == ("result", {"orders": [{"order_id": 1}]})
    assert mint_attestation.call_count == 2
    assert [item["suning/authn"] for item in sessions.metadata] == [
        "token-jti-a",
        "token-jti-b",
    ]
    assert sessions.metadata[0] is not sessions.metadata[1]
    span_attributes = bridge_module.observability.end_mcp_span.call_args.kwargs
    assert span_attributes["retry_count"] == 1
    assert span_attributes["failure_type"] == ""
    assert span_attributes["circuit_state"] == "CLOSED"
    assert span_attributes["degrade_level"] == "L2_CORE"
    assert span_attributes["success"] is True
    assert span_attributes["degraded"] is False


@pytest.mark.asyncio
async def test_bridge_uses_tool_spec_policy_and_preserves_normal_success(
    bridge_module: ModuleType,
) -> None:
    """输入：search_orders ToolSpec、成功 MCPCallResult 和记录参数的 Manager。

    输出：无；Bridge 复制策略、未透传 ToolSpec 或改写成功 payload 时断言失败。
    功能：验证 Manager 参数直接来自 schemas，并继续复用 _successful_result。
    """

    response = _response(structured={"orders": []})
    manager = SimpleNamespace(
        call=AsyncMock(
            return_value=RESILIENCE.MCPCallResult(
                server_id="mcp-order",
                tool_name="search_orders",
                success=True,
                data=response,
                degrade_level=RESILIENCE.DegradeLevel.L2_CORE,
            )
        )
    )

    output = await bridge_module.invoke_business_tool("search_orders", {}, manager)

    assert output == ("result", {"orders": []})
    call = manager.call.await_args
    spec = SCHEMAS.TOOL_SPECS["search_orders"]
    assert call.kwargs["server_id"] == spec.server_id
    assert call.kwargs["degrade_level"] is spec.degrade_level
    assert call.kwargs["retry_on_timeout"] is spec.retry_on_timeout
    assert call.kwargs["empty_result_is_success"] is spec.empty_result_is_success
    assert callable(call.kwargs["attempt"])


def test_t16_l1_failure_returns_continuable_tool_result(
    bridge_module: ModuleType,
) -> None:
    """输入：L1 CONNECTION_REFUSED 最终失败结果。

    输出：无；返回 tool_error 或缺少 available=false/notice 时断言失败。
    功能：覆盖 T16，将非核心数据失败转换为可继续分析的降级 tool_result。
    """

    result = RESILIENCE.MCPCallResult(
        server_id="mcp-logistics",
        tool_name="query_logistics",
        success=False,
        error_type=RESILIENCE.FailureType.CONNECTION_REFUSED,
        error_message="internal endpoint refused",
        degraded=True,
        degrade_level=RESILIENCE.DegradeLevel.L1_NON_CRITICAL,
        degrade_note="辅助数据「query_logistics」暂未获取，其他分析可继续。",
    )

    kind, payload = bridge_module._hermes_result(result)

    assert kind == "result"
    assert payload == {
        "status": "degraded",
        "available": False,
        "degrade_level": "L1_NON_CRITICAL",
        "tool_name": "query_logistics",
        "failure_type": "CONNECTION_REFUSED",
        "notice": "辅助数据「query_logistics」暂未获取，其他分析可继续。",
    }


def test_t17_l2_failure_returns_incomplete_tool_result_without_internal_error(
    bridge_module: ModuleType,
) -> None:
    """输入：含敏感内部诊断文本的 L2 TIMEOUT 最终失败结果。

    输出：无；未提示结果不完整、返回 tool_error 或泄露内部文本时断言失败。
    功能：覆盖 T17，通过安全 notice 表达核心数据缺失。
    """

    result = RESILIENCE.MCPCallResult(
        server_id="mcp-order",
        tool_name="search_orders",
        success=False,
        error_type=RESILIENCE.FailureType.TIMEOUT,
        error_message="secret=abc sql=select * from private",
        degraded=True,
        degrade_level=RESILIENCE.DegradeLevel.L2_CORE,
        degrade_note="核心数据「search_orders」暂不可用；当前结果不完整，请稍后重试该部分。",
    )

    kind, payload = bridge_module._hermes_result(result)

    assert kind == "result"
    assert payload["available"] is False
    assert payload["degrade_level"] == "L2_CORE"
    assert "当前结果不完整" in payload["notice"]
    assert "secret" not in str(payload)
    assert "select" not in str(payload)


def test_t18_l3_failure_returns_only_safe_tool_error(
    bridge_module: ModuleType,
) -> None:
    """输入：无实际 Tool 映射的构造 L3 UNKNOWN 最终失败结果。

    输出：无；返回降级 payload 或泄露内部错误时断言失败。
    功能：覆盖 T18 的行为分支，不人为新增 L3 TOOL_SPECS。
    """

    result = RESILIENCE.MCPCallResult(
        server_id="mcp-critical-test",
        tool_name="critical_test",
        success=False,
        error_type=RESILIENCE.FailureType.UNKNOWN,
        error_message="private stack trace",
        degraded=True,
        degrade_level=RESILIENCE.DegradeLevel.L3_CRITICAL,
        degrade_note="关键数据服务暂不可用，请稍后重试。",
    )

    assert bridge_module._hermes_result(result) == (
        "error",
        "关键数据服务暂不可用，请稍后重试。",
    )


def test_l2_empty_result_returns_available_empty_envelope(
    bridge_module: ModuleType,
) -> None:
    """输入：查询成功但 JSON 负载为空列表的 L2 MCPCallResult。

    输出：无；空结果被标记不可用或转换为 tool_error 时断言失败。
    功能：保留合法 EMPTY_RESULT，并附加可区分服务故障的结构化 empty 提示。
    """

    result = RESILIENCE.MCPCallResult(
        server_id="mcp-order",
        tool_name="search_orders",
        success=True,
        data=_response(text="[]"),
        error_type=RESILIENCE.FailureType.EMPTY_RESULT,
        degrade_level=RESILIENCE.DegradeLevel.L2_CORE,
    )

    kind, payload = bridge_module._hermes_result(result)

    assert kind == "result"
    assert payload["status"] == "empty"
    assert payload["available"] is True
    assert payload["failure_type"] == "EMPTY_RESULT"
    assert payload["data"] == []


@pytest.mark.asyncio
async def test_t20_timeline_partial_is_success_and_does_not_count_failure(
    bridge_module: ModuleType,
) -> None:
    """输入：包含 partial/source_failures/timeline 的成功时间线结果。

    输出：无；顶层 Server 被重试、计数或丢失部分结果字段时断言失败。
    功能：覆盖 T20，保持业务 partial 与 MCP Server failure 的语义隔离。
    """

    redis_client = FakeCircuitRedis()
    payload = {
        "partial": True,
        "source_failures": [{"source": "logistics", "reason": "timeout"}],
        "timeline": [{"source": "order", "status": "CREATED"}],
    }
    response = _response(structured=payload)
    result = await RESILIENCE.MCPCallManager(redis_client).call(
        server_id="mcp-order-timeline",
        tool_name="trace_order_timeline",
        degrade_level=RESILIENCE.DegradeLevel.L2_CORE,
        retry_on_timeout=True,
        empty_result_is_success=False,
        attempt=SequenceAttempt(response),
    )

    assert result.success is True
    assert result.error_type is None
    assert redis_client.get(_key("failures", "mcp-order-timeline")) is None
    assert bridge_module._hermes_result(result) == ("result", payload)
