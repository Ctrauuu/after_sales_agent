"""构建并注册苏宁业务长期记忆的回答前后 Hooks。"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from typing import Any

from suning_context_runtime import (
    ContextManager,
    DashScopeEmbeddingClient,
    LongMemoryRetriever,
    LongTermMemoryPipeline,
    MemoryExtractor,
    MilvusVectorStore,
    RetrievalResult,
    SQLiteMemoryStore,
    create_model,
)


logger = logging.getLogger(__name__)


def _required_env(name: str) -> str:
    """输入：长期记忆运行所需的环境变量名称 ``name``。

    输出：去除首尾空白后的配置值；缺失时抛出 ``RuntimeError``。
    功能：在 Hook 注册阶段阻止模型密钥等关键配置以空值继续运行。
    """

    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}")
    return value


def _slot_context(context: Any) -> dict[str, Any]:
    """输入：从 Redis 恢复的 ``ConversationContext`` 或兼容对象。

    输出：只含当前话题、规范业务实体和其余筛选条件的可复制字典。
    功能：移除回复摘要并按稳定字段拆分槽位，供召回增强和长期记忆唯一键复用。
    """

    slots = getattr(context, "slots", None)
    filters = dict(getattr(slots, "filters", {}) or {})
    filters.pop("last_reply_summary", None)
    entity_fields = {
        "brand",
        "category",
        "order_id",
        "reason",
        "region",
        "return_id",
        "sku_code",
    }
    entities = {
        key: value
        for key, value in filters.items()
        if key in entity_fields
    }
    return {
        "topic": str(getattr(slots, "topic", "") or ""),
        "entities": entities,
        "filters": {
            key: value
            for key, value in filters.items()
            if key not in entity_fields
        },
    }


def _resolved_user_id(context: Any, sender_id: str) -> str:
    """输入：已加载会话上下文和本轮可能缺失的 Hermes 发送者标识。

    输出：优先使用当前 Hook 可信发送者，否则返回 pre Hook 已绑定到 Redis 的用户标识。
    功能：兼容 Hermes 的 post Hook 不传 ``sender_id``，同时不从消息正文推断身份。
    """

    normalized_sender = sender_id.strip()
    if normalized_sender:
        return normalized_sender
    return str(getattr(context, "user_id", "") or "").strip()


def format_memory_context(results: list[RetrievalResult]) -> str:
    """输入：已经完成融合排序和用户隔离的长期记忆结果。

    输出：供 Hermes ``pre_llm_call`` 注入的中文参考文本；无结果时返回空字符串。
    功能：以固定安全提示和紧凑结构呈现最多三条历史结论，强调实时结果优先。
    """

    if not results:
        return ""
    lines = [
        "以下是该用户过去形成的业务分析结论，仅作为历史参考。",
        "如果历史结论与本次实时查询结果冲突，应以实时查询结果为准。",
    ]
    for index, result in enumerate(results[:3], start=1):
        record = result.record
        conditions = {
            "entities": record.entities,
            "filters": record.filters,
        }
        lines.extend(
            [
                "",
                f"{index}. 主题：{record.topic}",
                "   条件："
                + json.dumps(conditions, ensure_ascii=False, sort_keys=True),
                f"   结论：{record.conclusion}",
                "   证据："
                + json.dumps(record.evidence, ensure_ascii=False, sort_keys=True),
                f"   更新时间：{record.updated_at.isoformat()}",
            ]
        )
    return "\n".join(lines)


class LongTermMemoryHooks:
    """在回答前召回用户长期记忆，并在回答后异步提交结构化沉淀。"""

    def __init__(
        self,
        manager: ContextManager,
        pipeline: LongTermMemoryPipeline,
    ) -> None:
        """输入：现有 Redis 上下文管理器和长期记忆编排管道。

        输出：可注册到 Hermes 生命周期的 Hook 集合；不执行召回或写入。
        功能：复用短期上下文中的可信用户及最新槽位，把生命周期参数接入长期记忆链路。
        """

        self.manager = manager
        self.pipeline = pipeline
        self._turn_states: dict[
            tuple[str, str],
            tuple[str, dict[str, Any]],
        ] = {}
        self._turn_states_lock = threading.RLock()

    def _remember_turn(
        self,
        session_id: str,
        turn_id: str,
        user_id: str,
        slot_context: dict[str, Any],
    ) -> None:
        """输入：会话、轮次、pre 阶段可信用户和当时已更新的槽位。

        输出：无；轮次有效时把用户与槽位快照保存到最多 1024 项的进程内映射。
        功能：让不含发送者的 post Hook 按 turn 精确恢复身份，避免并发会话绑定覆盖。
        """

        normalized_turn = str(turn_id).strip()
        if not normalized_turn:
            return
        key = (session_id, normalized_turn)
        with self._turn_states_lock:
            self._turn_states[key] = (user_id, copy.deepcopy(slot_context))
            while len(self._turn_states) > 1024:
                self._turn_states.pop(next(iter(self._turn_states)))

    def _take_turn(
        self,
        session_id: str,
        turn_id: str,
    ) -> tuple[str, dict[str, Any]] | None:
        """输入：post 阶段的会话标识和 Hermes 轮次标识。

        输出：pre 阶段保存的可信用户与槽位快照；缺失时返回 ``None``。
        功能：以一次性读取方式回收轮次身份，限制状态增长并防止旧轮次被重复消费。
        """

        normalized_turn = str(turn_id).strip()
        if not normalized_turn:
            return None
        with self._turn_states_lock:
            return self._turn_states.pop((session_id, normalized_turn), None)

    def pre_llm_call(
        self,
        *,
        session_id: str = "",
        sender_id: str = "",
        turn_id: str = "",
        user_message: Any = "",
        **_kwargs: Any,
    ) -> dict[str, str] | None:
        """输入：Hermes 会话、轮次、可信发送者、本轮用户消息及其他生命周期元数据。

        输出：最多三条历史业务结论组成的上下文；缺少用户、无结果或故障时返回 ``None``。
        功能：读取短期槽位增强查询，并在回答前执行 FTS5 与 Milvus 双通道召回。
        """

        normalized_session = session_id.strip()
        if not normalized_session:
            return None
        try:
            context = self.manager.load_context(normalized_session)
            user_id = _resolved_user_id(context, sender_id)
            if not user_id:
                return None
            slot_context = _slot_context(context)
            self._remember_turn(
                normalized_session,
                turn_id,
                user_id,
                slot_context,
            )
            results = self.pipeline.retrieve(
                user_id=user_id,
                user_message=str(user_message),
                slot_context=slot_context,
            )
            rendered = format_memory_context(results)
            return {"context": rendered} if rendered else None
        except Exception:
            logger.exception("长期记忆召回失败: session=%s", normalized_session)
            return None

    def post_llm_call(
        self,
        *,
        session_id: str = "",
        sender_id: str = "",
        turn_id: str = "",
        user_message: Any = "",
        assistant_response: Any = "",
        conversation_history: Any = None,
        **_kwargs: Any,
    ) -> None:
        """输入：Hermes 会话、轮次、本轮问答、历史消息及可能缺失的可信发送者。

        输出：无；用户有效时只提交后台沉淀任务，不等待 LLM、Embedding 或索引写入。
        功能：读取短期 Hook 刚保存的最新槽位和用户绑定，构造不可变任务快照并快速返回。
        """

        normalized_session = session_id.strip()
        if not normalized_session:
            return
        try:
            turn_state = self._take_turn(normalized_session, turn_id)
            try:
                context = self.manager.load_context(normalized_session)
            except Exception:
                if turn_state is None:
                    raise
                context = None
            captured_user = turn_state[0] if turn_state is not None else ""
            user_id = sender_id.strip() or captured_user or _resolved_user_id(
                context,
                "",
            )
            if not user_id:
                return
            if turn_state is not None and user_id == captured_user:
                slot_context = turn_state[1]
            elif str(getattr(context, "user_id", "") or "").strip() == user_id:
                slot_context = _slot_context(context)
            else:
                slot_context = {"topic": "", "entities": {}, "filters": {}}
            self.pipeline.submit_turn(
                user_id=user_id,
                user_message=str(user_message),
                assistant_response=str(assistant_response),
                conversation_history=(
                    conversation_history
                    if conversation_history is not None
                    else list(getattr(context, "recent_turns", []) or [])
                ),
                slot_context=slot_context,
            )
        except Exception:
            logger.exception("提交长期记忆任务失败: session=%s", normalized_session)


def build_memory_hooks(manager: ContextManager) -> LongTermMemoryHooks:
    """输入：已由短期上下文 Hook 使用的共享 ``ContextManager``。

    输出：根据环境变量装配完成的长期记忆 Hooks；关键模型配置无效时抛出异常。
    功能：创建 DeepSeek 提取器、SQLite/FTS5、DashScope、Milvus、召回器和单线程管道。
    """

    min_confidence = float(os.getenv("LONG_MEMORY_MIN_CONFIDENCE", "0.7"))
    top_k = int(os.getenv("LONG_MEMORY_TOP_K", "3"))
    half_life_days = float(os.getenv("LONG_MEMORY_HALF_LIFE_DAYS", "30"))
    extraction_model = create_model(
        api_key=(
            os.getenv("DEEPSEEK_API", "")
            or os.getenv("DEEPSEEK_API_KEY", "")
        ),
        base_url=os.getenv("DEEPSEEK_BASE_URL") or None,
        model=os.getenv("DEEPSEEK_MODEL") or None,
        timeout_seconds=float(
            os.getenv("LONG_MEMORY_LLM_TIMEOUT_SECONDS", "20")
        ),
        max_tokens=1000,
    )
    store = SQLiteMemoryStore(
        os.getenv(
            "MEMORY_DB_PATH",
            "~/.hermes/state/suning_business_memory.db",
        )
    )
    embedder = DashScopeEmbeddingClient(
        api_key=_required_env("DASHSCOPE_API_KEY"),
        model=os.getenv("MEMORY_EMBEDDING_MODEL", "text-embedding-v3"),
        timeout_seconds=float(
            os.getenv("MEMORY_EMBEDDING_TIMEOUT_SECONDS", "10")
        ),
    )
    vector_store = MilvusVectorStore(
        uri=os.getenv("MILVUS_URI", "http://127.0.0.1:19530"),
        collection_name=os.getenv("MILVUS_COLLECTION", "memory_vectors"),
        timeout_seconds=float(os.getenv("MILVUS_TIMEOUT_SECONDS", "5")),
    )
    extractor = MemoryExtractor(
        extraction_model,
        min_confidence=min_confidence,
    )
    retriever = LongMemoryRetriever(
        store,
        embedder,
        vector_store,
        top_k=top_k,
        half_life_days=half_life_days,
    )
    pipeline = LongTermMemoryPipeline(
        store,
        extractor,
        embedder,
        vector_store,
        retriever,
    )
    return LongTermMemoryHooks(manager, pipeline)


def register_memory_hooks(
    plugin_context: Any,
    manager: ContextManager,
    hooks: LongTermMemoryHooks | None = None,
) -> LongTermMemoryHooks:
    """输入：Hermes 插件上下文、共享上下文管理器及可选已构建长期记忆 Hooks。

    输出：按回答前、回答后顺序注册完成的 ``LongTermMemoryHooks``。
    功能：在短期 Hooks 之后追加长期召回和异步沉淀回调，保证槽位状态已经是本轮最新版。
    """

    resolved_hooks = hooks or build_memory_hooks(manager)
    plugin_context.register_hook("pre_llm_call", resolved_hooks.pre_llm_call)
    plugin_context.register_hook("post_llm_call", resolved_hooks.post_llm_call)
    return resolved_hooks


__all__ = [
    "LongTermMemoryHooks",
    "build_memory_hooks",
    "format_memory_context",
    "register_memory_hooks",
]
