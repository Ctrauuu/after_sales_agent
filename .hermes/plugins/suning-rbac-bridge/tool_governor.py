"""按会话场景限制苏宁 MCP 的调用、参数和缓存。"""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from jsonschema import ValidationError, validate

from .schemas import TOOL_SPECS, ToolSpec


CACHE_TTL_SECONDS = 300
TURN_TTL_SECONDS = 600
TOOL_STATUS_KEY_PREFIX = "mcp:tool:enabled:"


@dataclass(frozen=True)
class ToolWhiteList:
    """一个已识别业务场景可调用的 MCP 工具集合。"""

    scene: str
    tools: tuple[str, ...]
    max_calls_per_turn: int = 5


SCENE_TOOL_MAP: dict[str, ToolWhiteList] = {
    "return_analysis": ToolWhiteList(
        scene="退单分析",
        tools=(
            "search_orders",
            "query_return_stats_nl2sql",
            "get_product_info",
            "query_aftersale_nl2sql",
            "query_sku_return_rate",
        ),
    ),
    "return_case": ToolWhiteList(
        scene="订单追踪",
        tools=(
            "get_order_detail",
            "get_aftersale_workflow",
            "query_logistics",
            "get_refund_status",
            "trace_order_timeline",
        ),
        max_calls_per_turn=4,
    ),
    "order_query": ToolWhiteList(
        scene="订单追踪",
        tools=(
            "get_order_detail",
            "get_aftersale_workflow",
            "query_logistics",
            "get_refund_status",
            "trace_order_timeline",
        ),
        max_calls_per_turn=4,
    ),
    "product_query": ToolWhiteList(
        scene="商品查询",
        tools=("get_product_info",),
        max_calls_per_turn=3,
    ),
    "general": ToolWhiteList(
        scene="通用查询",
        tools=(
            "search_orders",
            "query_return_stats_nl2sql",
            "query_sku_return_rate",
            "get_product_info",
            "get_order_detail",
            "get_aftersale_workflow",
            "query_logistics",
            "get_refund_status",
        ),
        max_calls_per_turn=3,
    ),
}


@dataclass
class _TurnState:
    """当前异步 Agent 回合的场景、Redis 键和已发起调用。"""

    session_id: str
    turn_id: str
    whitelist: ToolWhiteList
    called: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class ToolDecision:
    """一次调用预检查后的允许、拒绝或缓存复用结果。"""

    error: str = ""
    cached_result: str | None = None


class ToolGovernor:
    """在 Hermes Hook 与 MCP Bridge 之间执行工具调用治理。"""

    def __init__(self, redis_client: Any, registry: dict[str, ToolSpec] = TOOL_SPECS) -> None:
        """输入：共享 Redis 客户端 ``redis_client`` 和工具规格映射 ``registry``。

        输出：保存配置与每个异步回合独立的 ContextVar 状态。
        功能：复用现有 Redis 连接实现跨调用缓存和单回合预算，不新建存储或路由依赖。
        """

        self._redis = redis_client
        self._registry = registry
        self._turn: ContextVar[_TurnState | None] = ContextVar(
            "suning_tool_governor_turn", default=None
        )

    def begin_turn(self, *, session_id: str, turn_id: str, scene: str) -> ToolWhiteList:
        """输入：逻辑会话 ID ``session_id``、当前回合 ID ``turn_id`` 和槽位场景 ``scene``。

        输出：本轮生效的场景工具白名单。
        功能：为随后同一 ContextVar 中的真实 MCP 调用建立白名单、去重记录和 Redis 预算命名空间。
        """

        whitelist = self.get_scene_tools(scene)
        self._turn.set(
            _TurnState(
                session_id=session_id or "anonymous",
                turn_id=turn_id or "current",
                whitelist=whitelist,
            )
        )
        return whitelist

    def get_scene_tools(self, scene: str) -> ToolWhiteList:
        """输入：会话槽位输出的场景名称 ``scene``。

        输出：匹配场景的工具白名单；未知或空场景回退至通用查询。
        功能：把已有意图槽位映射成有限的业务工具集，避免模型自由扩张调用范围。
        """

        return SCENE_TOOL_MAP.get(scene, SCENE_TOOL_MAP["general"])

    def build_tool_prompt(self, whitelist: ToolWhiteList) -> str:
        """输入：当前回合的 ``whitelist``。

        输出：仅列出白名单工具、简述和合法参数名的中文 Prompt 片段。
        功能：把场景约束注入模型决策上下文，并明确禁止调用未列出的工具。
        """

        lines = [
            f"当前场景：{whitelist.scene}。只能调用以下 MCP 工具，最多调用 {whitelist.max_calls_per_turn} 次："
        ]
        for tool_name in whitelist.tools:
            spec = self._registry.get(tool_name)
            if spec is None:
                continue
            parameters = spec.schema.get("parameters", {})
            names = ", ".join(parameters.get("properties", {})) or "无"
            lines.append(f"- {tool_name}: {spec.schema.get('description', '')}；参数：{names}")
        lines.append("无业务数据查询需求时不要调用工具；未列出工具不得调用。")
        return "\n".join(lines)

    def preflight(
        self,
        *,
        tool_name: str,
        params: dict[str, Any],
        cache_scope: str,
    ) -> ToolDecision:
        """输入：模型工具名 ``tool_name``、参数 ``params`` 和已验证身份的缓存范围 ``cache_scope``。

        输出：允许时返回空决策，缓存命中携带结果，拒绝时携带安全错误消息。
        功能：按白名单、JSON Schema、结果缓存、同回合去重和 Redis 调用预算依次拦截真实 MCP 调用。
        """

        if not self._tool_enabled(tool_name):
            return ToolDecision(error="该工具已被管理员禁用")
        state = self._turn.get()
        if state is None:
            valid, error = self.validate_params(tool_name, params)
            return ToolDecision(error=error if not valid else "")
        whitelist = state.whitelist
        if tool_name not in whitelist.tools:
            return ToolDecision(error="当前场景不可用此工具")
        valid, error = self.validate_params(tool_name, params)
        if not valid:
            return ToolDecision(error=error)
        invocation_key = self._invocation_key(tool_name, params)
        cached = self._get_cache(tool_name, params, cache_scope)
        if cached is not None:
            return ToolDecision(cached_result=cached)
        if invocation_key in state.called:
            return ToolDecision(error="本轮已执行相同查询，跳过")
        if not self._consume_budget(state):
            return ToolDecision(
                error=(
                    f"本轮MCP调用已达上限({whitelist.max_calls_per_turn}次)，请缩小查询范围。"
                    f"剩余可用工具: {list(whitelist.tools)}"
                )
            )
        state.called.add(invocation_key)
        return ToolDecision()

    def _tool_enabled(self, tool_name: str) -> bool:
        """输入：待调用工具名 ``tool_name``。

        输出：管理开关未明确禁用时返回 ``True``。
        功能：从共享 Redis 读取管理 API 发布的即时工具状态；Redis 故障时按既有注册配置 fail-open。
        """

        if self._redis is None:
            return True
        try:
            value = self._redis.get(f"{TOOL_STATUS_KEY_PREFIX}{tool_name}")
        except Exception:
            return True
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        return value != "0"

    def validate_params(self, tool_name: str, params: dict[str, Any]) -> tuple[bool, str]:
        """输入：白名单中的工具名 ``tool_name`` 与模型提供的参数字典 ``params``。

        输出：参数符合 JSON Schema 时返回 ``(True, '')``；否则返回安全错误说明。
        功能：在网络调用前验证类型、必填项和值域，仅向模型暴露可用参数名而不泄漏完整 Schema。
        """

        spec = self._registry.get(tool_name)
        if spec is None:
            return False, "未知工具"
        parameter_schema = spec.schema.get("parameters", {})
        try:
            validate(instance=params, schema=parameter_schema)
        except ValidationError:
            names = ", ".join(parameter_schema.get("properties", {})) or "无"
            return False, f"参数不合法。{tool_name} 接受的参数: {names}"
        return True, ""

    def cache_success(
        self,
        *,
        tool_name: str,
        params: dict[str, Any],
        cache_scope: str,
        result: str,
    ) -> None:
        """输入：成功工具名、参数、身份缓存范围和 Hermes 结果文本 ``result``。

        输出：无；Redis 可用时写入五分钟缓存，失败时忽略缓存写入。
        功能：只复用已成功的同用户查询结果，避免把失败或降级响应作为后续数据答案。
        """

        if self._redis is None:
            return
        try:
            self._redis.setex(
                self._cache_key(tool_name, params, cache_scope),
                CACHE_TTL_SECONDS,
                result,
            )
        except Exception:
            return

    def _consume_budget(self, state: _TurnState) -> bool:
        """输入：当前回合状态 ``state``，其中包含预算上限和稳定 Redis 键组成。

        输出：计数未超过场景预算返回 ``True``，超限或 Redis 不可用时返回 ``False``。
        功能：以 Redis INCR 跨协程累计本轮真实 MCP 调用数，并给计数键设置短 TTL 防止残留。
        """

        if self._redis is None:
            return False
        try:
            used = int(self._redis.incr(self._budget_key(state)))
            if used == 1:
                self._redis.expire(self._budget_key(state), TURN_TTL_SECONDS)
        except Exception:
            return False
        return used <= state.whitelist.max_calls_per_turn

    def _get_cache(
        self,
        tool_name: str,
        params: dict[str, Any],
        cache_scope: str,
    ) -> str | None:
        """输入：工具名、参数和经身份隔离的缓存范围。

        输出：缓存的 Hermes 工具结果文本；未命中或 Redis 失败时返回 ``None``。
        功能：在消耗本轮预算前复用同一用户、同一工具和同一参数的成功查询。
        """

        if self._redis is None:
            return None
        try:
            cached = self._redis.get(self._cache_key(tool_name, params, cache_scope))
        except Exception:
            return None
        if isinstance(cached, bytes):
            return cached.decode("utf-8", errors="replace")
        return cached if isinstance(cached, str) else None

    def _budget_key(self, state: _TurnState) -> str:
        """输入：当前回合状态 ``state``。

        输出：不含原始会话内容的稳定 Redis 预算键。
        功能：隔离每个逻辑会话和回合的调用计数，避免跨用户或跨轮次共享预算。
        """

        return f"mcp:budget:{self._digest([state.session_id, state.turn_id])}"

    def _cache_key(self, tool_name: str, params: dict[str, Any], cache_scope: str) -> str:
        """输入：工具名 ``tool_name``、参数 ``params`` 和身份缓存范围 ``cache_scope``。

        输出：可用于 Redis 的确定性缓存键。
        功能：把同工具同参数结果按调用主体隔离，防止 RBAC 查询结果跨用户复用。
        """

        return f"mcp:cache:{self._digest([cache_scope, tool_name, params])}"

    def _invocation_key(self, tool_name: str, params: dict[str, Any]) -> str:
        """输入：工具名 ``tool_name`` 与已验证参数 ``params``。

        输出：同回合去重用的确定性摘要。
        功能：区分不同工具的同形参数，避免错误地把无关查询视为重复调用。
        """

        return self._digest([tool_name, params])

    @staticmethod
    def _digest(value: list[Any]) -> str:
        """输入：由文本、字典或列表组成的可 JSON 序列化列表 ``value``。

        输出：键排序后的 SHA-256 十六进制摘要。
        功能：替代进程随机化的 ``hash``，让 Redis 键和去重在重启后仍保持一致。
        """

        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["SCENE_TOOL_MAP", "ToolDecision", "ToolGovernor", "ToolWhiteList"]
