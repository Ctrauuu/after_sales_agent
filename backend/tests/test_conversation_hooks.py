"""验证结构化槽位提取、数据库白名单和 Hermes Hooks 接入。"""

from __future__ import annotations

import json
import importlib
import importlib.util
import runpy
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import Mock

import sqlalchemy as sa
from sqlalchemy.pool import StaticPool
import suning_context_runtime.model as context_model_module

from suning_context_runtime import (
    BusinessWhitelist,
    ContextManager,
    ConversationHooks,
    DatabaseWhitelistLoader,
    SlotExtraction,
    SlotExtractor,
    ValidatedSlotExtraction,
    create_model,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_HOOKS = (
    PROJECT_ROOT
    / ".hermes"
    / "plugins"
    / "suning-rbac-bridge"
    / "context_hooks.py"
)


class FakeRedis:
    """在内存中模拟上下文 Hooks 使用的 Redis get/set 接口。"""

    def __init__(self) -> None:
        """输入：无；不读取外部服务。

        输出：持有空字典的 Redis 测试替身。
        功能：为多轮 Hooks 测试保存序列化后的会话上下文。
        """

        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        """输入：Redis 键 ``key``。

        输出：此前写入的字符串；键不存在时返回 ``None``。
        功能：模拟同步 Redis 客户端的读取语义。
        """

        return self.values.get(key)

    def set(self, key: str, value: str, *, ex: int) -> None:
        """输入：Redis 键、序列化值和过期秒数 ``ex``。

        输出：无；原地记录值和过期配置。
        功能：模拟同步 Redis 客户端带 TTL 的写入语义。
        """

        self.values[key] = value
        self.expirations[key] = ex


class FakePluginContext:
    """记录 Hermes 插件注册的生命周期 Hooks。"""

    def __init__(self) -> None:
        """输入：无；不依赖真实 Hermes Runtime。

        输出：带空 Hook 映射及编排依赖替身的插件上下文。
        功能：供注册契约测试核对事件名称、工具和主机托管编排能力的绑定关系。
        """

        self.hooks: dict[str, Any] = {}
        self.tools: list[str] = []
        self.llm = object()
        self.subagent_lifecycle = object()

    def register_hook(self, event: str, callback: Any) -> None:
        """输入：Hermes 生命周期事件名和回调对象。

        输出：无；把回调原地写入事件映射。
        功能：模拟插件 API 的 Hook 注册行为。
        """

        self.hooks[event] = callback

    def register_tool(self, *, name: str, **_kwargs: Any) -> None:
        """输入：工具名称 ``name`` 和测试不关心的其他注册参数。

        输出：无；把工具名追加到已注册列表。
        功能：模拟插件 API 的工具注册行为，验证上下文降级不影响 RBAC 工具。
        """

        self.tools.append(name)


def _load_plugin_package(monkeypatch: Any) -> ModuleType:
    """输入：pytest 环境补丁器和项目内真实插件包路径。

    输出：支持相对导入的临时插件模块；加载失败时抛出导入异常。
    功能：补齐 Hermes 专属模块替身，并在后端测试进程中执行真实插件入口。
    """

    gateway = ModuleType("gateway")
    gateway.__path__ = []  # type: ignore[attr-defined]
    session_context = ModuleType("gateway.session_context")
    session_context.get_session_env = Mock(return_value="")  # type: ignore[attr-defined]
    tools = ModuleType("tools")
    tools.__path__ = []  # type: ignore[attr-defined]
    registry = ModuleType("tools.registry")
    registry.tool_error = Mock()  # type: ignore[attr-defined]
    registry.tool_result = Mock()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.registry", registry)

    package_name = "_suning_context_hooks_plugin_test"
    spec = importlib.util.spec_from_file_location(
        package_name,
        PLUGIN_HOOKS.parent / "__init__.py",
        submodule_search_locations=[str(PLUGIN_HOOKS.parent)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法创建插件测试模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return module


def _whitelist_engine() -> sa.Engine:
    """输入：无；使用内存 SQLite 构造固定业务数据。

    输出：包含白名单加载器所需六张表和样例值的 SQLAlchemy Engine。
    功能：提供与 MySQL 查询结构兼容、不会依赖外部容器的数据库测试夹具。
    """

    engine = sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
    )
    statements = (
        "CREATE TABLE t_product_category (category_code TEXT, category_name TEXT)",
        "CREATE TABLE t_product_brand (brand_id INTEGER, brand_name TEXT)",
        "CREATE TABLE t_order_main (region_code TEXT, order_status TEXT)",
        "CREATE TABLE t_aftersale_return "
        "(return_reason_code TEXT, return_reason_desc TEXT, return_status TEXT)",
        "CREATE TABLE t_payment_refund (refund_status TEXT)",
        "CREATE TABLE t_product_sku (sku_code TEXT, product_name TEXT)",
        "INSERT INTO t_product_category VALUES ('C1-AC', '空调')",
        "INSERT INTO t_product_brand VALUES (101, '美的')",
        "INSERT INTO t_order_main VALUES ('HD', 'PAID')",
        "INSERT INTO t_aftersale_return VALUES ('QUALITY', '质量问题', 'APPROVED')",
        "INSERT INTO t_payment_refund VALUES ('REFUNDED')",
        "INSERT INTO t_product_sku VALUES ('SKU-AC-001', '一级能效空调')",
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(sa.text(statement))
    return engine


def test_database_whitelist_loads_aliases_and_uses_cache() -> None:
    """输入：包含实际业务候选值的内存数据库和五分钟缓存加载器。

    输出：无；断言名称、编码、地域别名均可规范化，且第二次读取复用快照。
    功能：验证槽位白名单确实来自数据库数据而非 LLM 自由生成。
    """

    loader = DatabaseWhitelistLoader(_whitelist_engine(), cache_ttl_seconds=300)

    first = loader.load()
    second = loader.load()

    assert first is second
    assert first.normalize("category", "C1-AC") == "空调"
    assert first.normalize("category", "空调") == "空调"
    assert first.normalize("region", "华东") == "HD"
    assert first.normalize("brand", "101") == "美的"
    assert first.normalize("reason", "质量问题") == "QUALITY"
    assert first.normalize("status", "refunded") == "REFUNDED"
    assert first.normalize("sku_code", "一级能效空调") == "SKU-AC-001"
    assert first.normalize("category", "火箭") is None
    assert "质量问题 -> QUALITY" in first.prompt_payload()["reason"]


def test_create_model_disables_deepseek_thinking_for_structured_output(
    monkeypatch: Any,
) -> None:
    """输入：DeepSeek 测试密钥和替代 LangChain 初始化函数。

    输出：无；断言公共模型工厂显式关闭 Thinking 模式。
    功能：防止 DeepSeek Thinking 与结构化输出的 ``tool_choice`` 冲突并返回 400。
    """

    expected_model = Mock()
    initializer = Mock(return_value=expected_model)
    monkeypatch.setattr(context_model_module, "init_chat_model", initializer)

    model = create_model(api_key="test-key", model="deepseek-v4-flash")

    assert model is expected_model
    assert initializer.call_args.kwargs["extra_body"] == {
        "thinking": {"type": "disabled"}
    }


def test_slot_extractor_drops_values_outside_database_whitelist() -> None:
    """输入：模型返回合法结构，但其中品牌不存在于数据库白名单。

    输出：无；断言有效值被规范化、未知品牌被丢弃且数字槽位被保留。
    功能：验证 Pydantic 结构化输出之后仍执行数据库值域限制。
    """

    whitelist = BusinessWhitelist(
        categories={"空调": "空调", "C1-AC": "空调"},
        regions={"华东": "HD", "HD": "HD"},
    )
    loader = Mock()
    loader.load.return_value = whitelist
    structured_model = Mock()
    structured_model.invoke.return_value = SlotExtraction(
        topic="return_analysis",
        new_filters={
            "category": "C1-AC",
            "region": "华东",
            "brand": "不存在品牌",
            "order_id": 20260810001,
            "date_range_days": 30,
            "group_by": "reason",
        },
        confidence=0.93,
    )
    model = Mock()
    model.with_structured_output.return_value = structured_model
    extractor = SlotExtractor(model, loader, confidence_threshold=0.7)

    result = extractor.extract(
        current_topic="return_analysis",
        current_filters={"category": "空调"},
        user_message="看华东最近30天，按原因分组，品牌是不存在品牌",
    )

    assert result.topic == "return_analysis"
    assert result.confidence == 0.93
    assert result.new_filters == {
        "category": "空调",
        "region": "HD",
        "date_range_days": 30,
        "group_by": "reason",
    }
    messages = structured_model.invoke.call_args.args[0]
    payload = json.loads(messages[1].content)
    assert payload["current_topic"] == "return_analysis"
    assert payload["database_whitelist"]["category"] == [
        "C1-AC -> 空调",
        "空调",
    ]


def test_slot_extractor_rejects_low_confidence_update() -> None:
    """输入：模型把模糊追问识别成新话题但置信度低于门槛。

    输出：无；断言沿用原话题且不写入任何新筛选条件。
    功能：验证不确定提取不会清空旧槽位或污染后续工具参数。
    """

    loader = Mock()
    loader.load.return_value = BusinessWhitelist()
    structured_model = Mock()
    structured_model.invoke.return_value = SlotExtraction(
        topic="order_query",
        new_filters={"date_range_days": 90},
        confidence=0.42,
    )
    model = Mock()
    model.with_structured_output.return_value = structured_model

    result = SlotExtractor(model, loader, confidence_threshold=0.7).extract(
        current_topic="return_analysis",
        current_filters={"category": "空调"},
        user_message="那个再看看",
    )

    assert result == ValidatedSlotExtraction(
        topic="return_analysis",
        new_filters={},
        confidence=0.42,
    )


def test_hooks_inject_inherited_slots_and_persist_turn() -> None:
    """输入：同一用户连续两轮退单分析消息及固定结构化提取结果。

    输出：无；断言第二轮注入继承品类、时间、分组和上一轮回答摘要。
    功能：验证 pre/post Hooks 把已实现的结构化槽位真正应用于后续回答。
    """

    redis_client = FakeRedis()
    manager = ContextManager(redis_client)
    extractor = Mock()
    extractor.extract.side_effect = (
        ValidatedSlotExtraction(
            topic="return_analysis",
            new_filters={"category": "空调", "date_range_days": 30},
            confidence=0.95,
        ),
        ValidatedSlotExtraction(
            topic="return_analysis",
            new_filters={"group_by": "reason"},
            confidence=0.91,
        ),
    )
    hooks = ConversationHooks(manager, extractor)

    first = hooks.pre_llm_call(
        session_id="fs-chat-1",
        sender_id="ou-user-1",
        user_message="查最近30天空调退单",
    )
    assert first is not None
    hooks.post_llm_call(
        session_id="fs-chat-1",
        sender_id="ou-user-1",
        user_message="查最近30天空调退单",
        assistant_response="空调退单共12笔。",
    )
    second = hooks.pre_llm_call(
        session_id="fs-chat-1",
        sender_id="ou-user-1",
        user_message="按原因拆一下",
    )

    assert second is not None
    injected = second["context"]
    assert '"category": "空调"' in injected
    assert '"date_range_days": 30' in injected
    assert '"group_by": "reason"' in injected
    assert "空调退单共12笔" in injected
    stored = manager.load_context("fs-chat-1")
    assert stored.user_id == "ou-user-1"
    assert stored.turn_count == 1
    assert stored.slots.topic == "return_analysis"
    assert stored.slot_confidence == 0.91


def test_hooks_reset_context_when_session_sender_changes() -> None:
    """输入：同一会话标识先后出现两个不同的可信飞书发送者。

    输出：无；断言第二个用户的提取输入不含第一个用户的槽位。
    功能：验证异常会话复用时进行用户隔离，防止上下文跨用户泄漏。
    """

    redis_client = FakeRedis()
    manager = ContextManager(redis_client)
    extractor = Mock()
    extractor.extract.side_effect = (
        ValidatedSlotExtraction(
            topic="return_analysis",
            new_filters={"category": "空调"},
            confidence=0.9,
        ),
        ValidatedSlotExtraction(
            topic="order_query",
            new_filters={"status": "PAID"},
            confidence=0.9,
        ),
    )
    hooks = ConversationHooks(manager, extractor)

    hooks.pre_llm_call(
        session_id="shared-session",
        sender_id="ou-user-1",
        user_message="看空调退单",
    )
    hooks.pre_llm_call(
        session_id="shared-session",
        sender_id="ou-user-2",
        user_message="看已支付订单",
    )

    second_call = extractor.extract.call_args_list[1].kwargs
    assert second_call["current_topic"] == ""
    assert second_call["current_filters"] == {}
    stored = manager.load_context("shared-session")
    assert stored.user_id == "ou-user-2"
    assert stored.slots.filters == {"status": "PAID"}


def test_post_hook_preserves_bound_user_when_sender_is_missing() -> None:
    """输入：pre 阶段绑定的可信用户和不含 ``sender_id`` 的真实 Hermes post 参数。

    输出：无；断言回答后仍保存到原用户上下文且槽位、轮次没有被清空。
    功能：锁定 Hermes post Hook 的实际调用契约，防止空发送者被误判成用户切换。
    """

    redis_client = FakeRedis()
    manager = ContextManager(redis_client)
    extractor = Mock()
    extractor.extract.return_value = ValidatedSlotExtraction(
        topic="return_analysis",
        new_filters={"category": "空调"},
        confidence=0.95,
    )
    hooks = ConversationHooks(manager, extractor)
    hooks.pre_llm_call(
        session_id="real-hook-session",
        sender_id="ou-user-1",
        user_message="查看空调退单",
    )

    hooks.post_llm_call(
        session_id="real-hook-session",
        user_message="查看空调退单",
        assistant_response="空调退单的主要原因是质量问题。",
        conversation_history=[],
    )

    stored = manager.load_context("real-hook-session")
    assert stored.user_id == "ou-user-1"
    assert stored.turn_count == 1
    assert stored.slots.topic == "return_analysis"
    assert stored.slots.filters["category"] == "空调"


def test_hooks_do_not_inject_other_user_context_when_extraction_fails() -> None:
    """输入：已有用户槽位、不同发送者以及抛出异常的槽位提取器。

    输出：无；断言不注入原用户数据，并持久化新发送者的空隔离上下文。
    功能：验证数据库或 LLM 故障时仍为后续无发送者的 post Hook 保留可信用户边界。
    """

    redis_client = FakeRedis()
    manager = ContextManager(redis_client)
    original_extractor = Mock()
    original_extractor.extract.return_value = ValidatedSlotExtraction(
        topic="return_analysis",
        new_filters={"category": "空调"},
        confidence=0.9,
    )
    ConversationHooks(manager, original_extractor).pre_llm_call(
        session_id="reused-session",
        sender_id="ou-user-1",
        user_message="看空调退单",
    )
    failing_extractor = Mock()
    failing_extractor.extract.side_effect = RuntimeError("database unavailable")

    injected = ConversationHooks(manager, failing_extractor).pre_llm_call(
        session_id="reused-session",
        sender_id="ou-user-2",
        user_message="继续",
    )

    assert injected is None
    stored = manager.load_context("reused-session")
    assert stored.user_id == "ou-user-2"
    assert stored.slots.filters == {}


def test_plugin_registers_pre_and_post_llm_hooks() -> None:
    """输入：真实插件 Hook 模块、插件上下文替身和已构建 Hooks。

    输出：无；断言回答前后两个事件均注册到 Hermes 生命周期。
    功能：锁定插件部署入口，避免公共上下文模块存在但未被 Agent 使用。
    """

    module = runpy.run_path(str(PLUGIN_HOOKS))
    plugin_context = FakePluginContext()
    hooks = Mock()

    result = module["register_context_hooks"](plugin_context, hooks)

    assert result is hooks
    assert plugin_context.hooks == {
        "pre_llm_call": hooks.pre_llm_call,
        "post_llm_call": hooks.post_llm_call,
    }


def test_conversation_runtime_reuses_injected_redis_client(monkeypatch: Any) -> None:
    """输入：已构建 Redis Client、最小环境配置和构造器替身。

    输出：无；重复创建 Redis Client 或未返回注入实例时断言失败。
    功能：验证 Phase 2 提取的共享 Client 入口不会为 Context 再建连接池。
    """

    module = runpy.run_path(str(PLUGIN_HOOKS))
    build_runtime = module["build_conversation_runtime"]
    runtime_globals = build_runtime.__globals__
    existing_redis = object()
    redis_from_url = Mock(side_effect=AssertionError("不应重复创建 Redis Client"))
    monkeypatch.setattr(module["redis"].Redis, "from_url", redis_from_url)
    for name, value in {
        "MYSQL_USER": "user",
        "MYSQL_PASSWORD": "password",
        "MYSQL_HOST": "127.0.0.1",
        "MYSQL_PORT": "3306",
        "MYSQL_DATABASE": "suning",
    }.items():
        monkeypatch.setenv(name, value)

    database_engine = object()
    monkeypatch.setitem(
        runtime_globals,
        "create_engine",
        Mock(return_value=database_engine),
    )
    monkeypatch.setitem(runtime_globals, "create_model", Mock(return_value=object()))
    monkeypatch.setitem(runtime_globals, "ContextManager", Mock(return_value=object()))
    monkeypatch.setitem(
        runtime_globals,
        "DatabaseWhitelistLoader",
        Mock(return_value=object()),
    )
    monkeypatch.setitem(runtime_globals, "SlotExtractor", Mock(return_value=object()))
    monkeypatch.setitem(runtime_globals, "ConversationHooks", Mock(return_value=object()))

    _, returned_redis, returned_engine = build_runtime(existing_redis)

    assert returned_redis is existing_redis
    assert returned_engine is database_engine
    redis_from_url.assert_not_called()


def test_plugin_keeps_rbac_tools_when_context_env_is_missing(
    monkeypatch: Any,
) -> None:
    """输入：缺少 Redis 配置的环境、真实插件入口和插件上下文替身。

    输出：无；断言上下文 Hooks 降级时业务、图表和编排工具仍全部注册。
    功能：防止可选上下文依赖配置错误导致原 RBAC 插件、图表或复杂分析能力整体不可用。
    """

    monkeypatch.delenv("REDIS_URL", raising=False)
    plugin = _load_plugin_package(monkeypatch)
    plugin_context = FakePluginContext()

    plugin.register(plugin_context)

    assert set(plugin_context.tools) == {
        "search_orders",
        "get_order_detail",
        "query_return_stats_nl2sql",
        "query_aftersale_nl2sql",
        "query_sku_return_rate",
        "get_aftersale_workflow",
        "get_product_info",
        "query_logistics",
        "get_refund_status",
        "trace_order_timeline",
        "send_aftersale_chart",
        "orchestrate_aftersale_analysis",
    }
    assert plugin_context.hooks == {}


def test_plugin_reuses_one_manager_and_redis_client_for_all_mcp_handlers(
    monkeypatch: Any,
) -> None:
    """输入：真实插件入口、共享 Redis 替身和构造器 Mock。

    输出：无；Manager/Redis 重建或任一 MCP Handler 未获同一 Manager 时断言失败。
    功能：锁定 Phase 3 的 register → Redis → Manager → make_handler 生命周期。
    """

    plugin = _load_plugin_package(monkeypatch)
    context_hooks = importlib.import_module(f"{plugin.__name__}.context_hooks")
    redis_client = object()
    manager = object()
    build_redis_client = Mock(return_value=redis_client)
    build_runtime = Mock(side_effect=RuntimeError("stop after bridge registration"))
    manager_constructor = Mock(return_value=manager)
    make_handler = Mock(return_value=Mock())
    monkeypatch.setattr(context_hooks, "build_redis_client", build_redis_client)
    monkeypatch.setattr(context_hooks, "build_conversation_runtime", build_runtime)
    monkeypatch.setattr(plugin, "MCPCallManager", manager_constructor)
    monkeypatch.setattr(plugin, "make_handler", make_handler)

    plugin.register(FakePluginContext())

    build_redis_client.assert_called_once_with()
    manager_constructor.assert_called_once_with(redis_client)
    build_runtime.assert_called_once_with(redis_client)
    assert [call.args[0] for call in make_handler.call_args_list] == list(
        plugin.TOOL_SPECS
    )
    assert all(call.args[1] is manager for call in make_handler.call_args_list)
