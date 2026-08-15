"""编排长期记忆的结构化提取、双存储写入和语义召回。"""

from __future__ import annotations

import copy
import logging
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable

from .long_memory_extractor import MemoryExtractor
from .long_memory_models import (
    MemoryRecord,
    RetrievalResult,
    build_embedding_text,
    build_memory_key,
    normalize_memory_topic,
    utc_now,
)
from .long_memory_retriever import LongMemoryRetriever
from .long_memory_store import (
    DashScopeEmbeddingClient,
    MilvusVectorStore,
    SQLiteMemoryStore,
)


logger = logging.getLogger(__name__)


class LongTermMemoryPipeline:
    """以单线程后台写入长期记忆，并为回答前 Hook 提供同步召回。"""

    def __init__(
        self,
        store: SQLiteMemoryStore,
        extractor: MemoryExtractor,
        embedder: DashScopeEmbeddingClient,
        vector_store: MilvusVectorStore,
        retriever: LongMemoryRetriever,
        *,
        executor: Any | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """输入：SQLite、提取器、Embedding、Milvus、召回器及可选执行器和时钟。

        输出：可提交后台沉淀任务并执行前置召回的管道；初始化时不调用外部模型。
        功能：集中保存长期记忆链路依赖，并默认创建单工作线程保证写入顺序。
        """

        self.store = store
        self.extractor = extractor
        self.embedder = embedder
        self.vector_store = vector_store
        self.retriever = retriever
        self.executor = executor or ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="suning-long-memory",
        )
        self._owns_executor = executor is None
        self.clock = clock

    @staticmethod
    def _memory_dimensions(
        extraction: Any,
        slot_context: dict[str, Any],
    ) -> tuple[dict[str, object], dict[str, object]]:
        """输入：LLM 提取结果和数据库白名单规范过的当前槽位上下文。

        输出：用于持久化及唯一键的 ``(entities, filters)`` 深复制字典。
        功能：有明确槽位值时以服务端规范值为准，否则保留 LLM 从历史结论提取的维度。
        """

        slot_entities = dict(slot_context.get("entities") or {})
        slot_filters = dict(slot_context.get("filters") or {})
        if slot_entities or slot_filters:
            return copy.deepcopy(slot_entities), copy.deepcopy(slot_filters)
        return (
            copy.deepcopy(dict(extraction.entities or {})),
            copy.deepcopy(dict(extraction.filters or {})),
        )

    def submit_turn(
        self,
        *,
        user_id: str,
        user_message: str,
        assistant_response: str,
        conversation_history: Any,
        slot_context: dict[str, Any],
    ) -> Future[MemoryRecord | None] | Any | None:
        """输入：可信用户、本轮问答、历史消息和回答后最新结构化槽位。

        输出：后台任务句柄；用户标识为空时返回 ``None``，且不产生写入副作用。
        功能：复制本轮快照并立即提交单线程任务，使 post Hook 不等待模型和索引服务。
        """

        normalized_user = user_id.strip()
        if not normalized_user:
            return None
        return self.executor.submit(
            self._record_safely,
            normalized_user,
            str(user_message),
            str(assistant_response),
            copy.deepcopy(conversation_history),
            copy.deepcopy(slot_context),
        )

    def _record_safely(
        self,
        user_id: str,
        user_message: str,
        assistant_response: str,
        conversation_history: Any,
        slot_context: dict[str, Any],
    ) -> MemoryRecord | None:
        """输入：已复制且已校验用户标识的单轮长期记忆候选数据。

        输出：成功写入的记忆或 ``None``；提取及 SQLite 异常会被记录并吞掉。
        功能：隔离后台任务异常，确保任何沉淀失败都不会反向影响已生成的用户回答。
        """

        try:
            return self.record_turn(
                user_id=user_id,
                user_message=user_message,
                assistant_response=assistant_response,
                conversation_history=conversation_history,
                slot_context=slot_context,
            )
        except Exception:
            logger.exception("长期记忆提取或写入失败: user=%s", user_id)
            return None

    def record_turn(
        self,
        *,
        user_id: str,
        user_message: str,
        assistant_response: str,
        conversation_history: Any,
        slot_context: dict[str, Any],
    ) -> MemoryRecord | None:
        """输入：可信用户、本轮完整问答、对话历史和最新结构化槽位。

        输出：通过门禁后写入 SQLite 的当前版本记忆；不应沉淀时返回 ``None``。
        功能：调用 LLM 提取并校验记忆，按稳定键增量更新，再尽力同步 Embedding 和 Milvus。
        """

        normalized_user = user_id.strip()
        if not normalized_user:
            return None
        extraction = self.extractor.extract(
            user_message=user_message,
            assistant_response=assistant_response,
            conversation_history=conversation_history,
            slot_context=slot_context,
        )
        if not self.extractor.is_recordable(extraction):
            return None

        topic = normalize_memory_topic(extraction.topic)
        if topic is None:
            return None
        conclusion = str(extraction.conclusion or "").strip()
        entities, filters = self._memory_dimensions(extraction, slot_context)
        timestamp = self.clock()
        candidate = MemoryRecord(
            id=str(uuid.uuid4()),
            memory_key=build_memory_key(
                normalized_user,
                topic,
                entities,
                filters,
            ),
            user_id=normalized_user,
            topic=topic,
            entities=entities,
            filters=filters,
            conclusion=conclusion,
            evidence=copy.deepcopy(extraction.evidence),
            confidence=extraction.confidence,
            created_at=timestamp,
            updated_at=timestamp,
            version=1,
        )
        saved = self.store.upsert(candidate)
        try:
            vector = self.embedder.embed(build_embedding_text(saved))
            self.vector_store.upsert(saved, vector)
        except Exception:
            logger.exception(
                "长期记忆向量同步失败，已保留 SQLite/FTS5 记录: memory=%s",
                saved.id,
            )
        return saved

    def retrieve(
        self,
        *,
        user_id: str,
        user_message: str,
        slot_context: dict[str, Any],
    ) -> list[RetrievalResult]:
        """输入：可信用户、本轮问题和回答前最新结构化槽位。

        输出：按融合分数及时间衰减排序的用户私有 TopN 长期记忆；空用户返回空列表。
        功能：把 Hook 的输入适配给双通道召回器，并维持用户隔离的入口校验。
        """

        normalized_user = user_id.strip()
        if not normalized_user:
            return []
        return self.retriever.retrieve(
            user_id=normalized_user,
            user_message=str(user_message),
            slot_context=copy.deepcopy(slot_context),
        )

    def shutdown(self, *, wait: bool = True) -> None:
        """输入：是否等待已提交任务完成的 ``wait`` 标记。

        输出：无；仅对管道自行创建的执行器停止接收任务，并按标记等待或取消排队任务。
        功能：为测试和受控进程退出提供显式的后台线程清理入口。
        """

        if self._owns_executor:
            self.executor.shutdown(wait=wait, cancel_futures=not wait)


__all__ = ["LongTermMemoryPipeline"]
