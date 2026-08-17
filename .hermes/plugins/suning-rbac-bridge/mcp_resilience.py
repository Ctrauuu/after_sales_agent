"""苏宁 MCP Client 边界使用的 resilience 基础类型。"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx


MAX_RETRIES = 3
ATTEMPT_TIMEOUT_SECONDS = 10
BASE_BACKOFF_SECONDS = 1
MAX_JITTER_SECONDS = 0.2
CIRCUIT_FAILURE_THRESHOLD = 5
CIRCUIT_FAILURE_TTL_SECONDS = 60
CIRCUIT_OPEN_SECONDS = 30
CIRCUIT_STATE_TTL_SECONDS = 120
CIRCUIT_PROBE_TTL_SECONDS = 15

logger = logging.getLogger(__name__)
_UNAVAILABLE = object()
_PROBE_SUCCESS_SCRIPT = """
-- probe_success
if redis.call('get', KEYS[4]) == ARGV[1] then
  return redis.call('del', KEYS[1], KEYS[2], KEYS[3], KEYS[4])
end
return 0
"""
_PROBE_FAILURE_SCRIPT = """
-- probe_failure
if redis.call('get', KEYS[4]) == ARGV[1] then
  redis.call('set', KEYS[2], 'open', 'EX', ARGV[2])
  redis.call('set', KEYS[3], 'open', 'EX', ARGV[3])
  redis.call('del', KEYS[1], KEYS[4])
  return 1
end
return 0
"""


class FailureType(str, Enum):
    """MCP 逻辑调用的标准失败分类。"""

    TIMEOUT = "TIMEOUT"
    CONNECTION_REFUSED = "CONNECTION_REFUSED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    EMPTY_RESULT = "EMPTY_RESULT"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    REMOTE_TOOL_ERROR = "REMOTE_TOOL_ERROR"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    UNKNOWN = "UNKNOWN"


class DegradeLevel(str, Enum):
    """MCP Tool 最终失败时采用的业务降级等级。"""

    L1_NON_CRITICAL = "L1_NON_CRITICAL"
    L2_CORE = "L2_CORE"
    L3_CRITICAL = "L3_CRITICAL"


class CircuitState(str, Enum):
    """MCP Server 熔断器在调用决策时的状态。"""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(kw_only=True)
class MCPCallResult:
    """一次 MCP 逻辑调用的标准结果及降级元数据。"""

    server_id: str
    tool_name: str
    success: bool
    data: Any = None
    error_type: FailureType | None = None
    error_message: str = ""
    retry_count: int = 0
    total_latency_ms: float = 0.0
    degraded: bool = False
    degrade_level: DegradeLevel
    degrade_note: str = ""
    circuit_state: CircuitState = CircuitState.CLOSED


def _degrade_note(tool_name: str, level: DegradeLevel) -> str:
    """输入：MCP Tool 名 ``tool_name`` 和声明的降级等级 ``level``。

    输出：不包含内部异常细节的 Agent 可读缺失说明。
    功能：为失败结果填充 Spec 约定的 L1、L2 或 L3 安全提示。
    """

    if level is DegradeLevel.L1_NON_CRITICAL:
        return f"辅助数据「{tool_name}」暂未获取，其他分析可继续。"
    if level is DegradeLevel.L2_CORE:
        return f"核心数据「{tool_name}」暂不可用；当前结果不完整，请稍后重试该部分。"
    return "关键数据服务暂不可用，请稍后重试。"


def _content_text(result: Any) -> str:
    """输入：可能包含 MCP ``content`` 块的调用结果 ``result``。

    输出：所有非空文本块拼接后的字符串；没有文本时返回空字符串。
    功能：仅提取协议层文本，供错误诊断和 JSON 负载校验使用。
    """

    return "\n".join(
        text.strip()
        for block in (getattr(result, "content", None) or [])
        if isinstance((text := getattr(block, "text", None)), str) and text.strip()
    )


def _empty_result(
    result: Any,
    empty_result_is_success: bool,
) -> tuple[bool, FailureType, str]:
    """输入：原始调用结果和 Tool 是否允许协议级空结果的策略。

    输出：成功标记、空结果分类和内部诊断说明。
    功能：把同一协议级空响应按 ToolSpec 映射为合法 EMPTY_RESULT 或脏响应。
    """

    if empty_result_is_success:
        return True, FailureType.EMPTY_RESULT, ""
    return False, FailureType.MALFORMED_RESPONSE, "MCP response is empty"


def _classify_response(
    result: Any,
    empty_result_is_success: bool,
) -> tuple[bool, FailureType | None, str]:
    """输入：一次 MCP attempt 的返回值和空结果策略。

    输出：业务可用标记、FailureType 及内部诊断文本。
    功能：只按 MCP 协议字段识别错误、空结果和非法 JSON，不解释业务对象。
    """

    if result is None or result == []:
        return _empty_result(result, empty_result_is_success)
    if isinstance(result, (dict, list)):
        return True, None, ""
    if bool(getattr(result, "isError", False)):
        return False, FailureType.REMOTE_TOOL_ERROR, (
            _content_text(result) or "MCP tool returned isError=true"
        )

    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        if structured == []:
            return _empty_result(result, empty_result_is_success)
        return True, None, ""

    text = _content_text(result)
    if not text:
        return _empty_result(result, empty_result_is_success)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False, FailureType.MALFORMED_RESPONSE, (
            "MCP response content is not valid JSON"
        )
    if payload is None or payload == []:
        return _empty_result(result, empty_result_is_success)
    return True, None, ""


def _classify_exception(exc: Exception) -> FailureType:
    """输入：一次 MCP attempt 抛出的异常 ``exc``。

    输出：Spec 定义的 FailureType。
    功能：仅识别可机器判断的超时、连接和权限异常，其余保持 UNKNOWN。
    """

    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return FailureType.TIMEOUT
    if isinstance(exc, (ConnectionRefusedError, httpx.ConnectError)):
        return FailureType.CONNECTION_REFUSED
    if isinstance(exc, PermissionError):
        return FailureType.PERMISSION_DENIED
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403}:
        return FailureType.PERMISSION_DENIED
    return FailureType.UNKNOWN


class CircuitStore:
    """使用现有同步 Redis Client 保存苏宁 MCP Server 熔断状态。"""

    def __init__(self, redis_client: Any) -> None:
        """输入：已创建的同步 Redis Client ``redis_client``。

        输出：持有该 Client 的 Store 实例；不主动建立网络连接。
        功能：限定熔断键空间，并为异步 Manager 提供 fail-open Redis 操作。
        """

        self.redis = redis_client

    @staticmethod
    def _key(server_id: str, suffix: str) -> str:
        """输入：稳定 MCP Server ID 和键用途后缀。

        输出：隔离于现有业务键空间的 Redis Key。
        功能：确保同一 Server 的所有 Tool 共享同一组熔断状态。
        """

        return f"suning:mcp:cb:{server_id}:{suffix}"

    async def _redis_call(
        self,
        operation: str,
        server_id: str,
        *args: Any,
        default: Any = _UNAVAILABLE,
        **kwargs: Any,
    ) -> Any:
        """输入：Redis 方法名、Server ID、调用参数和故障默认值。

        输出：同步 Redis 方法结果；异常时记录告警并返回 ``default``。
        功能：统一通过 ``asyncio.to_thread`` 隔离阻塞 I/O 并实现 Store fail-open。
        """

        try:
            method = getattr(self.redis, operation)
            return await asyncio.to_thread(method, *args, **kwargs)
        except Exception:
            logger.warning(
                "circuit_store_unavailable: operation=%s server_id=%s",
                operation,
                server_id,
                exc_info=True,
            )
            return default

    def _keys(self, server_id: str) -> tuple[str, str, str, str]:
        """输入：稳定 MCP Server ID ``server_id``。

        输出：按 failures、state、cooldown、probe 排列的四个 Redis Key。
        功能：为状态读取和原子 probe 完成脚本提供一致键顺序。
        """

        return tuple(
            self._key(server_id, suffix)
            for suffix in ("failures", "state", "cooldown", "probe")
        )  # type: ignore[return-value]

    async def state(self, server_id: str) -> CircuitState:
        """输入：稳定 MCP Server ID ``server_id``。

        输出：当前 CLOSED、OPEN 或 HALF_OPEN；Redis 故障时返回 CLOSED。
        功能：用 state 与 cooldown 键判断调用前的逻辑熔断状态。
        """

        _, state_key, cooldown_key, _ = self._keys(server_id)
        state_value = await self._redis_call("get", server_id, state_key)
        if state_value is _UNAVAILABLE or state_value is None:
            return CircuitState.CLOSED
        cooldown = await self._redis_call("get", server_id, cooldown_key)
        if cooldown is _UNAVAILABLE:
            return CircuitState.CLOSED
        return CircuitState.OPEN if cooldown is not None else CircuitState.HALF_OPEN

    async def acquire_probe(self, server_id: str) -> str | None:
        """输入：处于 HALF_OPEN 的稳定 MCP Server ID。

        输出：取得租约时的 owner token；已被占用时为 ``None``；Redis 故障时 fail-open。
        功能：通过 ``SET NX EX`` 保证跨进程只放行一个半开探测调用。
        """

        probe_key = self._keys(server_id)[3]
        owner = uuid.uuid4().hex
        acquired = await self._redis_call(
            "set",
            server_id,
            probe_key,
            owner,
            nx=True,
            ex=CIRCUIT_PROBE_TTL_SECONDS,
        )
        if acquired is _UNAVAILABLE:
            return owner
        return owner if acquired else None

    async def record_success(self, server_id: str) -> None:
        """输入：正常 CLOSED 调用成功的 Server ID。

        输出：无；Redis 故障仅记录告警。
        功能：清除当前 Server 的连续失败计数。
        """

        failures_key = self._keys(server_id)[0]
        await self._redis_call("delete", server_id, failures_key, default=None)

    async def record_failure(self, server_id: str) -> None:
        """输入：发生一次可计数最终失败的 Server ID。

        输出：无；达到五次时原地写入 OPEN 状态和冷却键。
        功能：使用 Redis INCR 建立 60 秒窗口，并在阈值处打开 Server 熔断器。
        """

        failures_key, state_key, cooldown_key, _ = self._keys(server_id)
        count = await self._redis_call("incr", server_id, failures_key)
        if count is _UNAVAILABLE:
            return
        if int(count) == 1:
            await self._redis_call(
                "expire",
                server_id,
                failures_key,
                CIRCUIT_FAILURE_TTL_SECONDS,
                default=None,
            )
        if int(count) < CIRCUIT_FAILURE_THRESHOLD:
            return
        await self._redis_call(
            "set",
            server_id,
            state_key,
            "open",
            ex=CIRCUIT_STATE_TTL_SECONDS,
            default=None,
        )
        await self._redis_call(
            "set",
            server_id,
            cooldown_key,
            "open",
            ex=CIRCUIT_OPEN_SECONDS,
            default=None,
        )
        await self._redis_call("delete", server_id, failures_key, default=None)

    async def complete_probe(
        self,
        server_id: str,
        owner: str,
        healthy: bool,
    ) -> None:
        """输入：HALF_OPEN Server ID、probe owner 和 Server 健康判定。

        输出：无；Redis 故障仅记录告警，过期 owner 不修改新租约。
        功能：用 Lua 原子校验 owner，成功关闭熔断或失败重新 OPEN 30 秒。
        """

        keys = self._keys(server_id)
        script = _PROBE_SUCCESS_SCRIPT if healthy else _PROBE_FAILURE_SCRIPT
        args: tuple[Any, ...] = (owner,)
        if not healthy:
            args += (CIRCUIT_STATE_TTL_SECONDS, CIRCUIT_OPEN_SECONDS)
        await self._redis_call(
            "eval",
            server_id,
            script,
            len(keys),
            *keys,
            *args,
            default=None,
        )


class MCPCallManager:
    """集中执行苏宁 MCP attempt、超时重试和 Server 级熔断决策。"""

    def __init__(self, redis_client: Any | None = None) -> None:
        """输入：可选的现有同步 Redis Client。

        输出：可在无 Redis 时运行的 Manager；不执行网络调用。
        功能：有 Client 时启用 CircuitStore，无 Client 时保持 MCP 调用 fail-open。
        """

        self.circuit_store = CircuitStore(redis_client) if redis_client is not None else None

    @staticmethod
    def _result(
        *,
        server_id: str,
        tool_name: str,
        degrade_level: DegradeLevel,
        success: bool,
        data: Any,
        error_type: FailureType | None,
        error_message: str,
        retry_count: int,
        started_at: float,
        circuit_state: CircuitState,
    ) -> MCPCallResult:
        """输入：逻辑调用身份、分类结果、计时起点与熔断决策状态。

        输出：字段完整的 ``MCPCallResult``。
        功能：统一计算总耗时，并仅为最终失败填充安全降级说明。
        """

        return MCPCallResult(
            server_id=server_id,
            tool_name=tool_name,
            success=success,
            data=data if success else None,
            error_type=error_type,
            error_message=error_message,
            retry_count=retry_count,
            total_latency_ms=(time.perf_counter() - started_at) * 1000,
            degraded=not success,
            degrade_level=degrade_level,
            degrade_note="" if success else _degrade_note(tool_name, degrade_level),
            circuit_state=circuit_state,
        )

    async def call(
        self,
        *,
        server_id: str,
        tool_name: str,
        degrade_level: DegradeLevel,
        retry_on_timeout: bool,
        empty_result_is_success: bool,
        attempt: Callable[[], Awaitable[Any]],
    ) -> MCPCallResult:
        """输入：Tool 静态策略和每次重新建立真实 MCP 调用的 async ``attempt``。

        输出：归一化 ``MCPCallResult``；上层取消时继续抛出 ``CancelledError``。
        功能：在 Server 级熔断边界内执行单次超时、有限重试和最终健康状态更新。
        """

        started_at = time.perf_counter()
        circuit_state = CircuitState.CLOSED
        probe_owner: str | None = None
        if self.circuit_store is not None:
            circuit_state = await self.circuit_store.state(server_id)
            if circuit_state is CircuitState.OPEN:
                return self._result(
                    server_id=server_id,
                    tool_name=tool_name,
                    degrade_level=degrade_level,
                    success=False,
                    data=None,
                    error_type=FailureType.CIRCUIT_OPEN,
                    error_message="MCP circuit is open",
                    retry_count=0,
                    started_at=started_at,
                    circuit_state=circuit_state,
                )
            if circuit_state is CircuitState.HALF_OPEN:
                probe_owner = await self.circuit_store.acquire_probe(server_id)
                if probe_owner is None:
                    return self._result(
                        server_id=server_id,
                        tool_name=tool_name,
                        degrade_level=degrade_level,
                        success=False,
                        data=None,
                        error_type=FailureType.CIRCUIT_OPEN,
                        error_message="MCP half-open probe is already running",
                        retry_count=0,
                        started_at=started_at,
                        circuit_state=circuit_state,
                    )

        retry_count = 0
        while True:
            try:
                response = await asyncio.wait_for(
                    attempt(),
                    timeout=ATTEMPT_TIMEOUT_SECONDS,
                )
                success, error_type, error_message = _classify_response(
                    response,
                    empty_result_is_success,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                success = False
                response = None
                error_type = _classify_exception(exc)
                error_message = str(exc)

            can_retry = (
                error_type is FailureType.TIMEOUT
                and retry_on_timeout
                and circuit_state is not CircuitState.HALF_OPEN
                and retry_count < MAX_RETRIES
            )
            if can_retry:
                delay = BASE_BACKOFF_SECONDS * (2**retry_count)
                delay += random.uniform(0, MAX_JITTER_SECONDS)
                await asyncio.sleep(delay)
                retry_count += 1
                continue

            countable_failure = error_type in {
                FailureType.TIMEOUT,
                FailureType.CONNECTION_REFUSED,
                FailureType.MALFORMED_RESPONSE,
            }
            if self.circuit_store is not None:
                if circuit_state is CircuitState.HALF_OPEN and probe_owner is not None:
                    await self.circuit_store.complete_probe(
                        server_id,
                        probe_owner,
                        healthy=success or not countable_failure,
                    )
                elif success:
                    await self.circuit_store.record_success(server_id)
                elif countable_failure:
                    await self.circuit_store.record_failure(server_id)
            return self._result(
                server_id=server_id,
                tool_name=tool_name,
                degrade_level=degrade_level,
                success=success,
                data=response,
                error_type=error_type,
                error_message=error_message,
                retry_count=retry_count,
                started_at=started_at,
                circuit_state=circuit_state,
            )


__all__ = [
    "ATTEMPT_TIMEOUT_SECONDS",
    "BASE_BACKOFF_SECONDS",
    "CIRCUIT_FAILURE_THRESHOLD",
    "CIRCUIT_FAILURE_TTL_SECONDS",
    "CIRCUIT_OPEN_SECONDS",
    "CIRCUIT_PROBE_TTL_SECONDS",
    "CIRCUIT_STATE_TTL_SECONDS",
    "CircuitStore",
    "CircuitState",
    "DegradeLevel",
    "FailureType",
    "MAX_JITTER_SECONDS",
    "MAX_RETRIES",
    "MCPCallManager",
    "MCPCallResult",
]
