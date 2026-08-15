"""召回并融合 SQLite FTS5 与 Milvus 中的长期记忆。"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

from .long_memory_models import MemoryRecord, RetrievalResult


logger = logging.getLogger(__name__)


class LongMemoryRetriever:
    """按用户检索长期记忆，并融合关键词和向量结果。"""

    def __init__(
        self,
        store: Any,
        embedder: Any,
        vector_store: Any,
        top_k: int = 3,
        half_life_days: float = 30,
    ) -> None:
        """输入：SQLite 存储、Embedding 客户端、Milvus 存储、数量和半衰期配置。

        输出：可执行双通道召回的对象。
        功能：保存查询依赖，并规范 TopK 与时间衰减参数。
        """

        self.store = store
        self.embedder = embedder
        self.vector_store = vector_store
        self.top_k = max(1, int(top_k))
        self.half_life_days = max(float(half_life_days), 1e-9)

    @staticmethod
    def build_query_text(user_message: str, slot_context: dict[str, Any]) -> str:
        """输入：本轮用户消息和包含 topic、entities、filters 的槽位上下文。

        输出：可同时用于 FTS5 与 Embedding 的查询文本。
        功能：用当前槽位补全用户省略的业务对象和筛选条件。
        """

        context = slot_context or {}
        parts = [f"用户问题：{user_message.strip()}"]
        for label, key in (("主题", "topic"), ("实体", "entities"), ("筛选条件", "filters")):
            value = context.get(key)
            if value:
                rendered = (
                    json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
                    if isinstance(value, dict)
                    else str(value)
                )
                parts.append(f"{label}：{rendered}")
        return "\n".join(parts)

    @staticmethod
    def _normalize(scores: dict[str, float], lower_is_better: bool) -> dict[str, float]:
        """输入：单个通道的原始分数字典和其排序方向。

        输出：缩放至 0 到 1 的相关度分数字典。
        功能：消除 BM25 与余弦相似度量纲不同对等权融合的影响。
        """

        scores = {key: value for key, value in scores.items() if math.isfinite(value)}
        if not scores:
            return {}
        low, high = min(scores.values()), max(scores.values())
        if math.isclose(low, high):
            return {key: 1.0 for key in scores}
        if lower_is_better:
            return {key: (high - value) / (high - low) for key, value in scores.items()}
        return {key: (value - low) / (high - low) for key, value in scores.items()}

    def _decay(self, score: float, updated_at: datetime, now: datetime) -> float:
        """输入：融合分数、记忆更新时间和本次召回时刻。

        输出：应用半衰期后的非负分数。
        功能：降低陈旧业务结论的排名，保证最新结论优先。
        """

        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now - updated_at).total_seconds() / 86400)
        return score * math.exp(-math.log(2) * age_days / self.half_life_days)

    def retrieve(
        self,
        *,
        user_id: str,
        user_message: str,
        slot_context: dict[str, Any],
        now: datetime | None = None,
    ) -> list[RetrievalResult]:
        """输入：可信用户、本轮消息、当前槽位和可选计算时刻。

        输出：按等权融合和时间衰减排序的至多 TopK 用户私有记忆。
        功能：执行 FTS5 与 Milvus 召回；向量服务失败时继续使用本地 FTS5 结果。
        """

        user_id = user_id.strip()
        if not user_id or not user_message.strip():
            return []
        query_text = self.build_query_text(user_message, slot_context)
        limit = self.top_k * 2

        try:
            fts_hits = self.store.search_fts(user_id, query_text, limit)
        except Exception:
            logger.exception("长期记忆 FTS5 召回失败: user_id=%s", user_id)
            fts_hits = []
        fts_records = {record.id: record for record, _ in fts_hits}
        fts_scores = {record.id: float(score) for record, score in fts_hits}

        vector_records: dict[str, MemoryRecord] = {}
        vector_scores: dict[str, float] = {}
        try:
            vector = self.embedder.embed(query_text)
            vector_hits = self.vector_store.search(user_id, vector, limit)
            vector_scores = {memory_id: float(score) for memory_id, score in vector_hits}
            vector_records = self.store.get_many(user_id, list(vector_scores))
            vector_scores = {
                memory_id: score
                for memory_id, score in vector_scores.items()
                if memory_id in vector_records
            }
        except Exception:
            logger.exception("长期记忆向量召回失败，降级使用 FTS5: user_id=%s", user_id)

        keyword_scores = self._normalize(fts_scores, lower_is_better=True)
        semantic_scores = self._normalize(vector_scores, lower_is_better=False)
        current_time = now or datetime.now(timezone.utc)
        results: list[RetrievalResult] = []
        for memory_id in keyword_scores.keys() | semantic_scores.keys():
            record = fts_records.get(memory_id) or vector_records.get(memory_id)
            if record is None:
                continue
            in_fts = memory_id in keyword_scores
            in_vector = memory_id in semantic_scores
            source = "both" if in_fts and in_vector else "fts5" if in_fts else "vector"
            score = 0.5 * keyword_scores.get(memory_id, 0.0) + 0.5 * semantic_scores.get(memory_id, 0.0)
            results.append(
                RetrievalResult(
                    record=record,
                    score=self._decay(score, record.updated_at, current_time),
                    source=source,
                )
            )
        return sorted(results, key=lambda item: item.score, reverse=True)[: self.top_k]


__all__ = ["LongMemoryRetriever"]
