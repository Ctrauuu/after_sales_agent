"""验证多 IM 统一身份、私聊会话延续与 Hook 转发。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool


IDENTITY_PATH = (
    Path(__file__).resolve().parents[2]
    / ".hermes"
    / "plugins"
    / "suning-rbac-bridge"
    / "identity_session.py"
)


class _FakeRedis:
    """支持活跃会话原子首写的最小 Redis 替身。"""

    def __init__(self) -> None:
        """输入：无。

        输出：初始化空键值和过期时间记录。
        功能：为身份会话路由提供不依赖外部 Redis 的确定性测试存储。
        """

        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        """输入：Redis 键 ``key``。

        输出：键已保存的字符串或 ``None``。
        功能：模拟活跃会话读取，不自动模拟真实时间流逝。
        """

        return self.values.get(key)

    def set(self, key: str, value: str, *, ex: int, nx: bool = False) -> bool:
        """输入：键、值、过期秒数 ``ex`` 与仅首次写入标志 ``nx``。

        输出：写入成功返回 ``True``；``nx`` 命中已有键时返回 ``False``。
        功能：模拟 Redis ``SET ... NX EX``，覆盖并发创建活跃会话的关键分支。
        """

        if nx and key in self.values:
            return False
        self.values[key] = value
        self.expirations[key] = ex
        return True

    def expire(self, key: str, seconds: int) -> bool:
        """输入：已存在 Redis 键 ``key`` 与新的过期秒数 ``seconds``。

        输出：键存在时返回 ``True``，否则返回 ``False``。
        功能：记录每次私聊消息刷新 30 分钟活跃会话 TTL 的行为。
        """

        if key not in self.values:
            return False
        self.expirations[key] = seconds
        return True


class _RecordingHooks:
    """记录被统一身份 Hook 转发参数的短期或长期 Hook 替身。"""

    def __init__(self, context: str) -> None:
        """输入：回答前需要返回的上下文文本 ``context``。

        输出：初始化空的 pre/post 调用记录。
        功能：验证下游 Hook 收到逻辑会话和 ``hermes_user_id``，不需要真实模型或存储。
        """

        self.context = context
        self.pre_calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []

    def pre_llm_call(self, **kwargs: Any) -> dict[str, str]:
        """输入：统一身份层转发的回答前参数 ``kwargs``。

        输出：固定上下文字典。
        功能：记录路由结果，模拟既有短期上下文或长期记忆的回答前注入。
        """

        self.pre_calls.append(kwargs)
        return {"context": self.context}

    def post_llm_call(self, **kwargs: Any) -> None:
        """输入：统一身份层转发的回答后参数 ``kwargs``。

        输出：无；原地追加调用记录。
        功能：验证拒绝请求不会落库，正常请求会将统一用户与逻辑会话传给下游持久化。
        """

        self.post_calls.append(kwargs)

    def pre_tool_call(self, **kwargs: Any) -> None:
        """输入：统一身份层转发的工具调用参数 ``kwargs``。

        输出：无；原地保存本次工具调用记录。
        功能：模拟 Skill 自进化 Hook，验证可信路由覆盖原始会话后不会产生重复关键字参数。
        """

        self.tool_calls.append(kwargs)


class _RecordingObservability:
    """记录统一身份 Hook 创建和结束的模型 API Span。"""

    def __init__(self) -> None:
        """输入：无。

        输出：带空 Trace、Span 和完成记录的观测替身。
        功能：避免测试依赖真实 OTel Provider，同时验证 Hook 使用 Hermes API 生命周期字段而非整回合耗时。
        """

        self.trace = object()
        self.started: list[str] = []
        self.ended: list[dict[str, Any]] = []
        self.finished: list[tuple[Any, str]] = []

    def ensure_trace(self, **_kwargs: Any) -> tuple[object, bool]:
        """输入：统一身份 Hook 提供的 Trace 元数据。

        输出：固定 Trace 对象及“新建”标记。
        功能：模拟观测客户端为一次 Agent 回合建立根 Trace。
        """

        return self.trace, True

    def start_llm_span(self, name: str) -> str:
        """输入：待记录的 LLM Span 名 ``name``。

        输出：可回传给结束方法的固定 Span 标识。
        功能：记录 Hook 是否在真实 API 请求开始时创建模型 Span。
        """

        self.started.append(name)
        return name

    def end_llm_span(self, span: str, **kwargs: Any) -> None:
        """输入：Span 标识及模型用量、耗时等结束字段。

        输出：无；原地保存结束记录。
        功能：验证 Hook 传入 Hermes 原生 Token 和 API 耗时，而不是把工具调用时长计为模型耗时。
        """

        self.ended.append({"span": span, **kwargs})

    def finish_trace(self, trace_record: Any, response: str) -> None:
        """输入：完成的 Trace 对象和最终回复文本。

        输出：无；原地保存完成记录。
        功能：验证整回合 Trace 仍会在 post LLM 生命周期完成时收尾。
        """

        self.finished.append((trace_record, response))


def _load_identity_session(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, dict[str, str]]:
    """输入：pytest 补丁器 ``monkeypatch``。

    输出：已加载真实身份路由模块及可原地更新的 Hermes 会话字段字典。
    功能：替换 Gateway ContextVar 读取边界，使统一身份测试不需要启动 Hermes。
    """

    session_values: dict[str, str] = {}
    gateway = ModuleType("gateway")
    gateway.__path__ = []  # type: ignore[attr-defined]
    session_context = ModuleType("gateway.session_context")
    session_context.get_session_env = lambda name, default="": session_values.get(name, default)
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)
    module_name = "_suning_identity_session_test"
    spec = importlib.util.spec_from_file_location(module_name, IDENTITY_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载身份会话模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module, session_values


def _identity_engine() -> sa.Engine:
    """输入：无。

    输出：含启用、停用和跨平台绑定样例的内存 SQLite Engine。
    功能：模拟现有 ``user_identity`` 与 ``user_platform_binding`` 表，验证路由不新建身份数据结构。
    """

    engine = sa.create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE user_identity (hermes_user_id TEXT, is_active INTEGER)"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TABLE user_platform_binding (hermes_user_id TEXT, platform TEXT, platform_user_id TEXT)"
            )
        )
        connection.execute(
            sa.text("INSERT INTO user_identity VALUES ('U-H001', 1), ('U-H002', 0)")
        )
        connection.execute(
            sa.text(
                "INSERT INTO user_platform_binding VALUES "
                "('U-H001', 'feishu', 'ou_zhang'), "
                "('U-H001', 'wecom', 'zhangsan'), "
                "('U-H001', 'dingtalk', 'ding_zhang'), "
                "('U-H002', 'feishu', 'ou_disabled')"
            )
        )
    return engine


def test_router_reuses_private_session_across_platforms_and_isolates_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：pytest 补丁器、跨平台绑定样例和内存 Redis。

    输出：无；私聊未复用、TTL 不正确或群聊恢复私聊时通过断言报告失败。
    功能：验证 30 分钟活跃路由仅对私聊生效，群聊仍按用户和群会话隔离。
    """

    identity, _session_values = _load_identity_session(monkeypatch)
    redis_client = _FakeRedis()
    router = identity.IdentitySessionRouter(_identity_engine(), redis_client)

    feishu_route = router.resolve(
        session_id="feishu-session",
        sender_id="ou_zhang",
        platform="feishu",
        chat_type="dm",
    )
    wecom_route = router.resolve(
        session_id="wecom-session",
        sender_id="zhangsan",
        platform="wecom",
        chat_type="direct",
    )
    group_route = router.resolve(
        session_id="wecom-group-1",
        sender_id="zhangsan",
        platform="wecom",
        chat_type="group",
    )

    assert feishu_route.hermes_user_id == "U-H001"
    assert wecom_route.session_id == feishu_route.session_id
    assert redis_client.expirations["im:active-session:U-H001"] == 1800
    assert group_route.session_id == "im:group:wecom:wecom-group-1:U-H001"
    assert group_route.session_id != feishu_route.session_id


def test_router_rejects_unbound_and_disabled_users(monkeypatch: pytest.MonkeyPatch) -> None:
    """输入：pytest 补丁器、内存身份表及未绑定和停用账号。

    输出：无；无效身份未抛出 ``IdentityResolutionError`` 时通过断言报告失败。
    功能：验证统一身份层按文档对未绑定和已停用账号默认拒绝，绝不从名称或手机号合并身份。
    """

    identity, _session_values = _load_identity_session(monkeypatch)
    router = identity.IdentitySessionRouter(_identity_engine(), _FakeRedis())

    with pytest.raises(identity.IdentityResolutionError):
        router.resolve(
            session_id="session-1",
            sender_id="unknown",
            platform="feishu",
            chat_type="dm",
        )
    with pytest.raises(identity.IdentityResolutionError):
        router.resolve(
            session_id="session-2",
            sender_id="ou_disabled",
            platform="feishu",
            chat_type="dm",
        )


def test_unified_hooks_forward_hermes_identity_and_reject_unknown_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：pytest 补丁器、跨平台可信会话字段和记录型下游 Hooks。

    输出：无；下游未收到内部用户、跨平台会话或未知用户仍产生副作用时通过断言报告失败。
    功能：验证短期上下文和长期记忆统一使用 ``hermes_user_id``，并由输出 Hook 强制返回未授权提示。
    """

    identity, session_values = _load_identity_session(monkeypatch)
    conversation = _RecordingHooks("短期上下文")
    memory = _RecordingHooks("长期记忆")
    skill = _RecordingHooks("")
    hooks = identity.UnifiedIdentityHooks(
        identity.IdentitySessionRouter(_identity_engine(), _FakeRedis()),
        conversation,
        memory,
        skill_hooks=skill,
    )
    session_values.update(
        {
            "HERMES_SESSION_PLATFORM": "feishu",
            "HERMES_SESSION_USER_ID": "ou_zhang",
            "HERMES_SESSION_CHAT_TYPE": "dm",
        }
    )

    injected = hooks.pre_llm_call(
        session_id="feishu-session",
        turn_id="turn-1",
        user_message="查看退单趋势",
    )
    allowed_tool = hooks.pre_tool_call(
        session_id="untrusted-tool-session",
        tool_name="query_return_stats_nl2sql",
    )
    hooks.post_llm_call(
        session_id="feishu-session",
        turn_id="turn-1",
        user_message="查看退单趋势",
        assistant_response="结果",
    )

    assert injected == {"context": "短期上下文\n\n长期记忆"}
    assert allowed_tool is None
    assert conversation.pre_calls[0]["sender_id"] == "U-H001"
    assert memory.pre_calls[0]["sender_id"] == "U-H001"
    assert conversation.post_calls[0]["session_id"].startswith("im:dm:U-H001:")
    assert memory.post_calls[0]["session_id"] == conversation.post_calls[0]["session_id"]
    assert skill.tool_calls[0]["session_id"] == conversation.post_calls[0]["session_id"]

    session_values["HERMES_SESSION_USER_ID"] = "unknown"
    denied = hooks.pre_llm_call(session_id="unknown-session", user_message="查询数据")
    tool_directive = hooks.pre_tool_call(tool_name="send_aftersale_chart")
    transformed = hooks.transform_llm_output(response_text="任意回答")
    hooks.post_llm_call(
        session_id="unknown-session",
        user_message="查询数据",
        assistant_response="不应保存",
    )

    assert denied is not None
    assert tool_directive == {
        "action": "block",
        "message": f"工具 send_aftersale_chart 已被阻止：{identity.UNAUTHORIZED_REPLY}",
    }
    assert transformed == identity.UNAUTHORIZED_REPLY
    assert len(conversation.post_calls) == 1
    assert len(memory.post_calls) == 1


def test_unified_hooks_record_real_api_usage_not_turn_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：可信飞书会话、记录型上下文 Hook 与模型 API 生命周期字段。

    输出：无；Span 名称、Token 映射或 API 毫秒时长错误时通过断言报告。
    功能：验证 LLM 可观测性绑定 ``pre/post_api_request``，使工具链路耗时不再被错误记为模型耗时。
    """

    identity, session_values = _load_identity_session(monkeypatch)
    observed = _RecordingObservability()
    hooks = identity.UnifiedIdentityHooks(
        identity.IdentitySessionRouter(_identity_engine(), _FakeRedis()),
        _RecordingHooks(""),
        observability_client=observed,
    )
    session_values.update(
        {
            "HERMES_SESSION_PLATFORM": "feishu",
            "HERMES_SESSION_USER_ID": "ou_zhang",
            "HERMES_SESSION_CHAT_TYPE": "dm",
        }
    )

    hooks.pre_llm_call(session_id="feishu-session", user_message="查询退单原因")
    hooks.pre_api_request(api_request_id="api-1", api_call_count=1)
    hooks.post_api_request(
        api_request_id="api-1",
        response_model="deepseek-v4-flash",
        api_duration=0.42,
        usage={"input_tokens": 120, "output_tokens": 45},
    )
    hooks.post_llm_call(session_id="feishu-session", assistant_response="结果")

    assert observed.started == ["api_request"]
    assert observed.ended == [
        {
            "span": "api_request",
            "prompt_tokens": 120,
            "completion_tokens": 45,
            "model": "deepseek-v4-flash",
            "duration_ms": 420.0,
        }
    ]
    assert observed.finished == [(observed.trace, "结果")]
