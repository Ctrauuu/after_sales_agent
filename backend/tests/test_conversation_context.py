"""验证多轮会话的槽位继承、话题切换和历史压缩。"""

import json
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from suning_hermes_agent.conversation_context import (
    CONTEXT_TTL_SECONDS,
    ContextManager,
    ConversationContext,
    ConversationSlot,
)


def test_load_context_creates_empty_context_and_restores_nested_slot() -> None:
    """输入：空 Redis 结果以及随后保存的嵌套会话数据。

    输出：无；断言新建默认值和反序列化后的槽位类型、内容正确。
    功能：验证上下文可以按 ``conv:<session_id>`` 在多轮之间完整恢复。
    """

    redis_client = Mock()
    redis_client.get.return_value = None
    manager = ContextManager(redis_client)

    context = manager.load_context("session-1")
    assert context == ConversationContext(session_id="session-1", user_id="")

    context.user_id = "user-1"
    context.slots = ConversationSlot(
        topic="return_analysis",
        filters={"category": "空调"},
    )
    manager.save_context(context)

    key, serialized = redis_client.set.call_args.args[:2]
    assert key == "conv:session-1"
    assert redis_client.set.call_args.kwargs == {"ex": CONTEXT_TTL_SECONDS}
    redis_client.get.return_value = serialized.encode("utf-8")

    restored = manager.load_context("session-1")
    assert isinstance(restored.slots, ConversationSlot)
    assert restored.user_id == "user-1"
    assert restored.slots.filters == {"category": "空调"}


def test_prepare_turn_inherits_filters_for_follow_up() -> None:
    """输入：同一退单话题的已有槽位和本轮新增分组条件。

    输出：无；断言旧筛选被继承且本轮条件覆盖同名槽位。
    功能：验证省略时间与品类的连续追问仍得到完整 MCP 查询条件。
    """

    manager = ContextManager(Mock())
    context = ConversationContext(
        session_id="session-1",
        user_id="user-1",
        slots=ConversationSlot(
            topic="return_analysis",
            filters={
                "date_range_days": 30,
                "category": "空调",
                "last_reply_summary": "空调退单量最高",
            },
        ),
    )

    filters = manager.prepare_turn(
        context,
        "return_analysis",
        {"group_by": "reason", "date_range_days": 60},
    )

    assert filters == {
        "date_range_days": 60,
        "category": "空调",
        "group_by": "reason",
    }
    assert context.slots.filters["last_reply_summary"] == "空调退单量最高"


def test_prepare_turn_clears_filters_when_topic_changes() -> None:
    """输入：退单分析槽位以及切换到维修统计的新意图和条件。

    输出：无；断言旧退单筛选被清空，仅保留新话题条件。
    功能：验证话题边界阻止无关条件跨业务主题泄漏。
    """

    manager = ContextManager(Mock())
    context = ConversationContext(
        session_id="session-1",
        user_id="user-1",
        slots=ConversationSlot(
            topic="return_analysis",
            filters={"category": "空调", "date_range_days": 30},
        ),
    )

    filters = manager.prepare_turn(
        context,
        "repair_stats",
        {"status": "pending"},
    )

    assert context.slots.topic == "repair_stats"
    assert filters == {"status": "pending"}


def test_complete_turn_compresses_only_after_ten_turns() -> None:
    """输入：已有十轮历史、第十一轮回复和模拟的 LangChain 模型响应。

    输出：无；断言早期八轮被压缩、最近三轮保留并写回 Redis。
    功能：验证压缩阈值、模型摘要写入、保留窗口和结论槽位更新。
    """

    redis_client = Mock()
    summary_model = Mock()
    summary_model.invoke.return_value = AIMessage(
        content="前八轮围绕退单分析形成了结论。"
    )
    manager = ContextManager(redis_client, summary_model=summary_model)
    context = ConversationContext(
        session_id="session-1",
        user_id="user-1",
        slots=ConversationSlot(topic="return_analysis"),
        turn_count=10,
        recent_turns=[
            {
                "topic": "return_analysis",
                "user_msg": f"问题{i}",
                "assistant_reply": f"回答{i}",
                "reply_summary": f"结论{i}",
            }
            for i in range(1, 11)
        ],
    )

    manager.complete_turn(context, "问题11", "回答11", "结论11")

    assert context.turn_count == 11
    assert len(context.recent_turns) == 3
    assert [turn["user_msg"] for turn in context.recent_turns] == [
        "问题9",
        "问题10",
        "问题11",
    ]
    assert context.compressed_history == ["前八轮围绕退单分析形成了结论。"]
    assert context.slots.filters["last_reply_summary"] == "结论11"
    redis_client.set.assert_called_once()
    messages = summary_model.invoke.call_args.args[0]
    assert "多轮会话上下文压缩助手" in messages[0].content
    assert "问题1" in messages[1].content
    assert "结论8" in messages[1].content


def test_summarize_turns_requires_deepseek_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：清空的 DeepSeek 环境变量和一轮待压缩对话。

    输出：无；断言缺少密钥时抛出配置错误且不访问网络。
    功能：验证真实摘要调用不会在未配置鉴权信息时静默生成伪摘要。
    """

    monkeypatch.delenv("DEEPSEEK_API", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    manager = ContextManager(Mock(), api_key="")

    with pytest.raises(RuntimeError, match="DEEPSEEK_API"):
        manager._summarize_turns([{"user_msg": "查询退单量"}])


def test_compress_history_keeps_all_raw_turns_at_threshold() -> None:
    """输入：轮次恰好达到十轮阈值的会话上下文。

    输出：无；断言第十轮尚不压缩历史，也不生成摘要。
    功能：锁定“超过十轮”而不是“达到十轮”的文档边界语义。
    """

    manager = ContextManager(Mock())
    context = ConversationContext(
        session_id="session-1",
        user_id="user-1",
        turn_count=10,
        recent_turns=[{"user_msg": f"问题{i}"} for i in range(1, 11)],
    )

    manager.compress_history(context)

    assert len(context.recent_turns) == 10
    assert context.compressed_history == []


def test_build_system_context_selects_three_summaries_and_recent_turns() -> None:
    """输入：超过注入上限的压缩摘要、槽位和最近对话。

    输出：无；断言 Prompt 只包含最近三条摘要、三轮原文及当前筛选。
    功能：验证选择性上下文拼装不会重新塞入全部历史记录。
    """

    manager = ContextManager(Mock())
    context = ConversationContext(
        session_id="session-1",
        user_id="user-1",
        slots=ConversationSlot(
            topic="return_analysis",
            filters={"category": "空调", "group_by": "reason"},
        ),
        compressed_history=["摘要1", "摘要2", "摘要3", "摘要4"],
        recent_turns=[
            {
                "user_msg": f"问题{i}",
                "assistant_reply": "答" * 220,
            }
            for i in range(1, 5)
        ],
    )

    prompt = manager.build_system_context(context)

    assert "摘要1" not in prompt
    assert all(summary in prompt for summary in ("摘要2", "摘要3", "摘要4"))
    assert "问题1" not in prompt
    assert all(question in prompt for question in ("问题2", "问题3", "问题4"))
    assert json.dumps(context.slots.filters, ensure_ascii=False) in prompt
    assistant_lines = [
        line.removeprefix("助手: ")
        for line in prompt.splitlines()
        if line.startswith("助手: ")
    ]
    assert all(len(line) == 200 for line in assistant_lines)
