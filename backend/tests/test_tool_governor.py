"""验证场景化工具裁剪、参数拦截、预算与缓存复用。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


GOVERNOR_PATH = (
    Path(__file__).resolve().parents[2]
    / ".hermes"
    / "plugins"
    / "suning-rbac-bridge"
    / "tool_governor.py"
)


class FakeRedis:
    """提供治理器使用的最小同步 Redis 接口。"""

    def __init__(self) -> None:
        """输入：无。

        输出：初始化内存键值、过期时间和自增计数。
        功能：让治理器测试验证缓存与预算，不依赖真实 Redis 服务。
        """

        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        """输入：Redis 键 ``key``。

        输出：已保存文本；键不存在时返回 ``None``。
        功能：模拟缓存读取和预算读取所需的 Redis 查询语义。
        """

        return self.values.get(key)

    def setex(self, key: str, seconds: int, value: str) -> None:
        """输入：缓存键 ``key``、过期秒数 ``seconds`` 和结果文本 ``value``。

        输出：无；原地保存值及其过期时间。
        功能：模拟 Redis 对成功 MCP 结果的带 TTL 写入。
        """

        self.values[key] = value
        self.expirations[key] = seconds

    def incr(self, key: str) -> int:
        """输入：预算计数键 ``key``。

        输出：自增后的整数计数。
        功能：模拟 Redis 的原子 INCR，供单回合预算判断使用。
        """

        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value

    def expire(self, key: str, seconds: int) -> bool:
        """输入：已存在键 ``key`` 和过期秒数 ``seconds``。

        输出：键存在时返回 ``True``，否则返回 ``False``。
        功能：模拟预算计数首次写入后的短期 TTL 设置。
        """

        if key not in self.values:
            return False
        self.expirations[key] = seconds
        return True


def _load_governor() -> ModuleType:
    """输入：无；读取项目内真实治理模块路径。

    输出：可使用相对导入的临时工具治理模块。
    功能：仅装配治理器及其 schemas 依赖，避免插件入口依赖 Hermes Runtime。
    """

    package_name = "_suning_tool_governor_test"
    package = ModuleType(package_name)
    package.__path__ = [str(GOVERNOR_PATH.parent)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.tool_governor",
        GOVERNOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载工具治理模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_tool_governor_restricts_scene_and_hides_schema_details() -> None:
    """输入：退单分析场景、越权订单详情调用及包含虚构字段的统计参数。

    输出：无；断言场景外工具和非法参数均在 MCP 网络调用前被拒绝。
    功能：验证场景白名单和 JSON Schema 校验共同阻止工具滥用与幻觉参数。
    """

    governor_module = _load_governor()
    redis_client = FakeRedis()
    governor = governor_module.ToolGovernor(redis_client)
    whitelist = governor.begin_turn(
        session_id="session-1", turn_id="turn-1", scene="return_analysis"
    )

    assert "get_order_detail" not in whitelist.tools
    assert "get_order_detail" in governor.build_tool_prompt(
        governor.get_scene_tools("order_query")
    )
    assert governor.preflight(
        tool_name="get_order_detail",
        params={"order_id": 1},
        cache_scope="feishu:user-1",
    ).error == "当前场景不可用此工具"
    invalid = governor.preflight(
        tool_name="query_return_stats_nl2sql",
        params={"status_code": 1},
        cache_scope="feishu:user-1",
    )
    assert invalid.error == (
        "参数不合法。query_return_stats_nl2sql 接受的参数: group_by, date_range_days, category"
    )
    assert "additionalProperties" not in invalid.error
    redis_client.values["mcp:tool:enabled:query_return_stats_nl2sql"] = "0"
    assert governor.preflight(
        tool_name="query_return_stats_nl2sql",
        params={},
        cache_scope="feishu:user-1",
    ).error == "该工具已被管理员禁用"


def test_tool_governor_reuses_cache_and_enforces_redis_budget() -> None:
    """输入：同用户同参数的两次统计请求，以及通用场景的四次不同合法请求。

    输出：无；断言第二次复用缓存且第四次真实调用超过三次预算后被拒绝。
    功能：验证缓存命中不消耗预算，并使用 Redis 计数限制单回合 MCP 资源消耗。
    """

    governor_module = _load_governor()
    redis_client = FakeRedis()
    governor = governor_module.ToolGovernor(redis_client)
    governor.begin_turn(session_id="session-1", turn_id="turn-1", scene="return_analysis")
    first = governor.preflight(
        tool_name="query_return_stats_nl2sql",
        params={"date_range_days": 7},
        cache_scope="feishu:user-1",
    )
    assert first == governor_module.ToolDecision()
    governor.cache_success(
        tool_name="query_return_stats_nl2sql",
        params={"date_range_days": 7},
        cache_scope="feishu:user-1",
        result='{"data": []}',
    )
    cached = governor.preflight(
        tool_name="query_return_stats_nl2sql",
        params={"date_range_days": 7},
        cache_scope="feishu:user-1",
    )
    assert cached.cached_result == '{"data": []}'
    assert governor_module.CACHE_TTL_SECONDS in redis_client.expirations.values()

    governor.begin_turn(session_id="session-1", turn_id="turn-2", scene="general")
    for tool_name, params in (
        ("search_orders", {}),
        ("query_return_stats_nl2sql", {}),
        ("get_product_info", {"sku_code": "SKU-1"}),
    ):
        assert governor.preflight(
            tool_name=tool_name,
            params=params,
            cache_scope="feishu:user-1",
        ) == governor_module.ToolDecision()
    over_budget = governor.preflight(
        tool_name="get_order_detail",
        params={"order_id": 1},
        cache_scope="feishu:user-1",
    )
    assert over_budget.error == (
        "本轮MCP调用已达上限(3次)，请缩小查询范围。"
        "剩余可用工具: ['search_orders', 'query_return_stats_nl2sql', 'get_product_info', "
        "'get_order_detail', 'get_aftersale_workflow', 'query_logistics', 'get_refund_status']"
    )
    assert governor_module.TURN_TTL_SECONDS in redis_client.expirations.values()
