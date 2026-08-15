"""验证长期记忆写入管道及 Hermes 回答前后 Hook 集成。"""

from __future__ import annotations

import runpy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call

import pytest

from suning_context_runtime import (
    ContextManager,
    ConversationContext,
    ConversationSlot,
    LongTermMemoryPipeline,
    MemoryExtraction,
    MemoryRecord,
    RetrievalResult,
    SQLiteMemoryStore,
    build_memory_key,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = PROJECT_ROOT / ".hermes" / "plugins" / "suning-rbac-bridge"
FIXED_NOW = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)


def _fixed_now() -> datetime:
    """输入：无；使用模块级固定 UTC 时刻。

    输出：供长期记忆管道创建记录的确定性时间。
    功能：消除真实时钟对持久化断言的影响。
    """

    return FIXED_NOW


def _accepted_extraction() -> MemoryExtraction:
    """输入：无；使用固定退单分析业务场景。

    输出：满足主题、置信度、结论和记录意愿门禁的结构化提取结果。
    功能：为管道成功写入及向量降级测试提供统一候选。
    """

    return MemoryExtraction(
        should_record=True,
        topic="return_analysis",
        entities={"category": "空调"},
        filters={"region": "华东", "date_range_days": 30},
        conclusion="空调退单中安装问题长期占比最高。",
        evidence={"sample_count": 120},
        confidence=0.92,
    )


def _record(index: int) -> MemoryRecord:
    """输入：用于区分测试记忆的正整数 ``index``。

    输出：属于 ``user-a`` 且更新时间固定的长期记忆记录。
    功能：构造格式化 Hook 上下文所需的多条可排序召回结果。
    """

    entities = {"category": "空调", "sequence": index}
    filters = {"region": "华东"}
    return MemoryRecord(
        id=f"memory-{index}",
        memory_key=build_memory_key(
            "user-a",
            "return_analysis",
            entities,
            filters,
        ),
        user_id="user-a",
        topic="return_analysis",
        entities=entities,
        filters=filters,
        conclusion=f"历史结论{index}",
        evidence={"rank": index},
        confidence=0.9,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
        version=1,
    )


def _configured_extractor() -> Mock:
    """输入：无；不连接真实结构化模型。

    输出：固定返回可记录提取结果的 ``MemoryExtractor`` 测试替身。
    功能：让管道测试只验证持久化和向量同步编排。
    """

    extractor = Mock()
    extractor.extract.return_value = _accepted_extraction()
    extractor.is_recordable.return_value = True
    return extractor


def _manager_with_context(context: ConversationContext) -> ContextManager:
    """输入：需要由 Hook 恢复的 ``ConversationContext``。

    输出：使用 Mock Redis 且已保存指定上下文的真实 ``ContextManager``。
    功能：验证 Hook 经现有上下文序列化边界恢复用户和槽位，而非直接读取测试对象。
    """

    redis_client = Mock()
    redis_client.get.return_value = None
    manager = ContextManager(redis_client)
    manager.save_context(context)
    serialized = redis_client.set.call_args.args[1]
    redis_client.get.return_value = serialized
    return manager


def _load_memory_hooks_namespace() -> dict[str, Any]:
    """输入：项目内真实 ``memory_hooks.py`` 文件路径。

    输出：执行插件长期记忆 Hook 模块后得到的全局命名空间。
    功能：绕过含连字符的插件目录名，直接测试真实注册与生命周期实现。
    """

    return runpy.run_path(str(PLUGIN_ROOT / "memory_hooks.py"))


def _load_context_hooks_namespace() -> dict[str, Any]:
    """输入：项目内真实 ``context_hooks.py`` 文件路径。

    输出：执行插件短期上下文 Hook 模块后得到的全局命名空间。
    功能：取得真实注册函数，以核对短期与长期回调的追加顺序。
    """

    return runpy.run_path(str(PLUGIN_ROOT / "context_hooks.py"))


def test_pipeline_persists_record_and_upserts_embedding_vector(
    tmp_path: Path,
) -> None:
    """输入：可记录提取结果、内存隔离 SQLite 文件及固定 Embedding 向量。

    输出：无；断言记录写入 SQLite/FTS，随后以同一记录和向量执行 Milvus upsert。
    功能：验证长期记忆管道的完整同步写入顺序和字段组装。
    """

    store = SQLiteMemoryStore(tmp_path / "memory.db")
    extractor = _configured_extractor()
    embedder = Mock()
    embedder.embed.return_value = [0.1, 0.2, 0.3]
    vector_store = Mock()
    pipeline = LongTermMemoryPipeline(
        store,
        extractor,
        embedder,
        vector_store,
        Mock(),
        executor=Mock(),
        clock=_fixed_now,
    )

    saved = pipeline.record_turn(
        user_id="user-a",
        user_message="空调退单的长期主要原因是什么？",
        assistant_response="多轮数据均显示安装问题占比最高。",
        conversation_history=[],
        slot_context={
            "topic": "return_analysis",
            "entities": {"category": "空调", "region": "HD"},
            "filters": {"date_range_days": 90},
        },
    )

    assert saved is not None
    assert saved.user_id == "user-a"
    assert saved.created_at == FIXED_NOW
    assert saved.entities == {"category": "空调", "region": "HD"}
    assert saved.filters == {"date_range_days": 90}
    assert store.get_by_key(saved.memory_key) == saved
    embedding_text = embedder.embed.call_args.args[0]
    assert "空调退单中安装问题长期占比最高" in embedding_text
    vector_store.upsert.assert_called_once_with(saved, [0.1, 0.2, 0.3])


@pytest.mark.parametrize("failure_point", ["embedding", "milvus"])
def test_pipeline_keeps_sqlite_record_when_vector_sync_fails(
    tmp_path: Path,
    failure_point: str,
) -> None:
    """输入：分别在 Embedding 和 Milvus upsert 阶段注入的异常。

    输出：无；断言管道仍返回并保留 SQLite 记录，不向调用者传播向量异常。
    功能：验证双存储部分失败时以本地 FTS 可召回记录作为降级结果。
    """

    store = SQLiteMemoryStore(tmp_path / f"memory-{failure_point}.db")
    embedder = Mock()
    embedder.embed.return_value = [0.1, 0.2]
    vector_store = Mock()
    if failure_point == "embedding":
        embedder.embed.side_effect = RuntimeError("embedding unavailable")
    else:
        vector_store.upsert.side_effect = RuntimeError("milvus unavailable")
    pipeline = LongTermMemoryPipeline(
        store,
        _configured_extractor(),
        embedder,
        vector_store,
        Mock(),
        executor=Mock(),
        clock=_fixed_now,
    )

    saved = pipeline.record_turn(
        user_id="user-a",
        user_message="分析空调退单趋势",
        assistant_response="安装问题长期占比最高。",
        conversation_history=[],
        slot_context={"topic": "return_analysis", "filters": {}},
    )

    assert saved is not None
    assert store.get_by_key(saved.memory_key) == saved
    if failure_point == "embedding":
        vector_store.upsert.assert_not_called()
    else:
        vector_store.upsert.assert_called_once()


def test_submit_turn_only_submits_copied_snapshot_to_injected_executor() -> None:
    """输入：不执行任务的可控 executor，以及提交后会被修改的历史和槽位对象。

    输出：无；断言 ``submit_turn`` 立即返回任务句柄、仅调用 submit 且参数为深复制快照。
    功能：验证 post Hook 不同步执行 LLM/Embedding，并避免调用方后续修改污染后台任务。
    """

    executor = Mock()
    task_handle = object()
    executor.submit.return_value = task_handle
    extractor = Mock()
    pipeline = LongTermMemoryPipeline(
        Mock(),
        extractor,
        Mock(),
        Mock(),
        Mock(),
        executor=executor,
        clock=_fixed_now,
    )
    history = [{"role": "user", "content": "原始问题"}]
    slot_context = {"topic": "return_analysis", "filters": {"category": "空调"}}

    returned = pipeline.submit_turn(
        user_id=" user-a ",
        user_message="问题",
        assistant_response="回答",
        conversation_history=history,
        slot_context=slot_context,
    )
    history[0]["content"] = "已修改"
    slot_context["filters"]["category"] = "冰箱"

    assert returned is task_handle
    executor.submit.assert_called_once()
    submitted = executor.submit.call_args.args
    assert submitted[0] == pipeline._record_safely
    assert submitted[1:4] == ("user-a", "问题", "回答")
    assert submitted[4] == [{"role": "user", "content": "原始问题"}]
    assert submitted[5] == {
        "topic": "return_analysis",
        "filters": {"category": "空调"},
    }
    extractor.extract.assert_not_called()


def test_post_hook_restores_user_without_sender_and_removes_reply_summary() -> None:
    """输入：Redis 中已绑定用户和含回复摘要的最新槽位，以及不带 sender_id 的 post 事件。

    输出：无；断言后台任务使用恢复的用户，并从提交槽位中移除 ``last_reply_summary``。
    功能：兼容真实 Hermes post 参数契约，同时防止逐轮摘要污染长期记忆稳定键。
    """

    context = ConversationContext(
        session_id="session-1",
        user_id="user-a",
        slots=ConversationSlot(
            topic="return_analysis",
            filters={
                "category": "空调",
                "last_reply_summary": "本轮临时摘要",
            },
        ),
    )
    manager = _manager_with_context(context)
    pipeline = Mock()
    hooks_class = _load_memory_hooks_namespace()["LongTermMemoryHooks"]
    hooks = hooks_class(manager, pipeline)
    history = [{"role": "user", "content": "问题"}]

    hooks.post_llm_call(
        session_id="session-1",
        user_message="问题",
        assistant_response="回答",
        conversation_history=history,
    )

    pipeline.submit_turn.assert_called_once_with(
        user_id="user-a",
        user_message="问题",
        assistant_response="回答",
        conversation_history=history,
        slot_context={
            "topic": "return_analysis",
            "entities": {"category": "空调"},
            "filters": {},
        },
    )


def test_hooks_keep_turn_user_when_session_context_is_overwritten() -> None:
    """输入：同一会话 pre 属于用户 A、post 前 Redis 上下文已变为用户 B 的并发边缘场景。

    输出：无；断言 post 仍以 ``turn_id`` 捕获的用户 A 和其槽位提交长期记忆。
    功能：避免真实 post 缺少发送者时由共享会话的可变用户绑定造成跨用户记忆归属。
    """

    context_a = ConversationContext(
        session_id="shared-session",
        user_id="user-a",
        slots=ConversationSlot(
            topic="return_analysis",
            filters={"category": "空调"},
        ),
    )
    context_b = ConversationContext(
        session_id="shared-session",
        user_id="user-b",
        slots=ConversationSlot(
            topic="order_query",
            filters={"order_id": 20260811001},
        ),
    )
    manager = Mock()
    manager.load_context.side_effect = [context_a, context_b]
    pipeline = Mock()
    pipeline.retrieve.return_value = []
    hooks_class = _load_memory_hooks_namespace()["LongTermMemoryHooks"]
    hooks = hooks_class(manager, pipeline)

    hooks.pre_llm_call(
        session_id="shared-session",
        turn_id="turn-a",
        sender_id="user-a",
        user_message="分析空调退单",
    )
    hooks.post_llm_call(
        session_id="shared-session",
        turn_id="turn-a",
        user_message="分析空调退单",
        assistant_response="安装问题长期占比最高。",
        conversation_history=[],
    )

    pipeline.submit_turn.assert_called_once_with(
        user_id="user-a",
        user_message="分析空调退单",
        assistant_response="安装问题长期占比最高。",
        conversation_history=[],
        slot_context={
            "topic": "return_analysis",
            "entities": {"category": "空调"},
            "filters": {},
        },
    )


def test_pre_hook_injects_at_most_three_memories_and_fails_open() -> None:
    """输入：四条已排序长期记忆以及随后抛出异常的召回管道。

    输出：无；断言首次只注入前三条，召回异常时返回 ``None`` 而不中断回答。
    功能：验证长期记忆 Prompt 上限和回答前 Hook 的故障降级边界。
    """

    context = ConversationContext(
        session_id="session-1",
        user_id="user-a",
        slots=ConversationSlot(
            topic="return_analysis",
            filters={"category": "空调"},
        ),
    )
    manager = _manager_with_context(context)
    pipeline = Mock()
    pipeline.retrieve.return_value = [
        RetrievalResult(record=_record(index), score=1.0 / index, source="both")
        for index in range(1, 5)
    ]
    hooks_class = _load_memory_hooks_namespace()["LongTermMemoryHooks"]
    hooks = hooks_class(manager, pipeline)

    injected = hooks.pre_llm_call(
        session_id="session-1",
        sender_id="user-a",
        user_message="上次结论是什么？",
    )

    assert injected is not None
    assert all(f"历史结论{index}" in injected["context"] for index in range(1, 4))
    assert "历史结论4" not in injected["context"]
    assert injected["context"].count(". 主题：") == 3

    pipeline.retrieve.side_effect = RuntimeError("retrieval unavailable")
    assert (
        hooks.pre_llm_call(
            session_id="session-1",
            sender_id="user-a",
            user_message="继续",
        )
        is None
    )


def test_short_and_long_memory_hooks_register_in_required_order() -> None:
    """输入：真实短期与长期注册函数、插件上下文替身及两组已构建 Hooks。

    输出：无；断言每个事件均先注册短期回调，再注册长期记忆回调。
    功能：锁定槽位先更新、长期记忆随后召回或沉淀的 Hermes 生命周期顺序。
    """

    context_namespace = _load_context_hooks_namespace()
    memory_namespace = _load_memory_hooks_namespace()
    plugin_context = Mock()
    short_hooks = Mock()
    short_hooks.manager = Mock()
    memory_hooks = Mock()

    context_namespace["register_context_hooks"](plugin_context, short_hooks)
    memory_namespace["register_memory_hooks"](
        plugin_context,
        short_hooks.manager,
        memory_hooks,
    )

    assert plugin_context.register_hook.call_args_list == [
        call("pre_llm_call", short_hooks.pre_llm_call),
        call("post_llm_call", short_hooks.post_llm_call),
        call("pre_llm_call", memory_hooks.pre_llm_call),
        call("post_llm_call", memory_hooks.post_llm_call),
    ]
