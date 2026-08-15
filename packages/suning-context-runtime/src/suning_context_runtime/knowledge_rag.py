"""提供售后政策知识库的 Milvus 入库、混合检索和引用上下文构造。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pymilvus import DataType, MilvusClient


class DocType(str, Enum):
    """定义知识片段的来源类型及其排序优先级键。"""

    NATIONAL_LAW = "national_law"
    BRAND_POLICY = "brand_policy"
    SUNING_POLICY = "suning_policy"
    REPAIR_MANUAL = "repair_manual"
    FAQ = "faq"


PRIORITY_WEIGHTS = {
    DocType.BRAND_POLICY: 1.0,
    DocType.SUNING_POLICY: 0.9,
    DocType.REPAIR_MANUAL: 0.8,
    DocType.NATIONAL_LAW: 0.7,
    DocType.FAQ: 0.5,
}
POLICY_KEYWORDS = (
    "保修",
    "质保",
    "三包",
    "退货",
    "换货",
    "政策",
    "条款",
    "维修",
    "安装",
    "时效",
    "SLA",
)
CATEGORY_KEYWORDS = (
    "壁挂式空调",
    "柜式空调",
    "中央空调",
    "滚筒洗衣机",
    "多门冰箱",
    "空调",
    "冰箱",
    "洗衣机",
    "电视",
    "手机",
)
BRAND_KEYWORDS = ("格力", "美的", "海尔", "海信", "TCL", "小米")
COMPONENT_KEYWORDS = ("压缩机", "整机", "主要部件", "铜管", "遥控器")
_CHINESE_TEXT = re.compile(r"[\u3400-\u9fff]{2,}")
_LATIN_TEXT = re.compile(r"[A-Za-z0-9_-]{2,}")


@dataclass(frozen=True)
class KnowledgeChunk:
    """表示带适用范围、时效和来源的一个售后知识片段。"""

    chunk_id: str
    content: str
    doc_type: DocType
    category: str = ""
    brand: str = ""
    effective_date: datetime | None = None
    expire_date: datetime | None = None
    source_title: str = ""
    article_number: str = ""


@dataclass(frozen=True)
class KnowledgeResult:
    """表示完成混合排序后的一个知识片段及其相关度。"""

    chunk: KnowledgeChunk
    score: float


def _utc_timestamp(value: datetime | None) -> int:
    """输入：可选的生效或失效时间 ``value``。

    输出：UTC Unix 秒；未提供时间时返回 ``0``。
    功能：统一知识元数据在 Milvus 中的时间表达，避免写入时混用时区文本。
    """

    if value is None:
        return 0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.astimezone(timezone.utc).timestamp())


def _from_timestamp(value: Any) -> datetime | None:
    """输入：Milvus 返回的 Unix 秒字段 ``value``。

    输出：UTC ``datetime``；零值、空值或非法值返回 ``None``。
    功能：将向量索引的轻量时间字段恢复为检索时效性比较使用的时间对象。
    """

    try:
        timestamp = int(value or 0)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc) if timestamp else None


def extract_entities(query: str, context_entities: Mapping[str, Any] | None = None) -> dict[str, str]:
    """输入：用户问题 ``query`` 及可选的会话实体上下文。

    输出：最多包含品类、品牌和部件的实体字典。
    功能：以确定性词典优先提取当前问题实体，并用已确认的会话槽位补全省略表达。
    """

    text = str(query or "")
    entities: dict[str, str] = {}
    for key, values in (
        ("category", CATEGORY_KEYWORDS),
        ("brand", BRAND_KEYWORDS),
        ("component", COMPONENT_KEYWORDS),
    ):
        matched = next((value for value in values if value.lower() in text.lower()), "")
        if matched:
            entities[key] = matched
    for key in ("category", "brand", "component"):
        value = str((context_entities or {}).get(key) or "").strip()
        if value and key not in entities:
            entities[key] = value
    return entities


def should_search_knowledge(query: str) -> bool:
    """输入：本轮用户消息 ``query``。

    输出：命中售后政策、保修或维修条款关键词时返回 ``True``。
    功能：避免普通订单查询也触发 Embedding 与 Milvus 检索，限制 RAG 仅服务知识类问题。
    """

    text = str(query or "").lower()
    return any(keyword.lower() in text for keyword in POLICY_KEYWORDS)


def _keyword_score(query: str, chunk: KnowledgeChunk) -> float:
    """输入：原始问题 ``query`` 和一个候选知识片段 ``chunk``。

    输出：0 到 1 的关键词覆盖率。
    功能：对向量候选补充中文二字片段与英文词的精确匹配信号，实现无需额外全文库的轻量混合排序。
    """

    terms: set[str] = set()
    for text in _CHINESE_TEXT.findall(query):
        terms.update(text[index : index + 2] for index in range(len(text) - 1))
    terms.update(word.lower() for word in _LATIN_TEXT.findall(query))
    if not terms:
        return 0.0
    searchable = "\n".join(
        (chunk.content, chunk.source_title, chunk.article_number, chunk.category, chunk.brand)
    ).lower()
    return sum(term.lower() in searchable for term in terms) / len(terms)


class KnowledgeMilvusStore:
    """管理售后知识片段的 Milvus collection。"""

    def __init__(
        self,
        uri: str,
        collection_name: str = "knowledge_chunks",
        client: Any | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        """输入：Milvus 地址、collection 名、可选测试客户端和超时秒数。

        输出：惰性知识向量存储对象。
        功能：保存连接配置，首次入库或检索时以实际 Embedding 维度创建独立知识 collection。
        """

        self.uri = uri.strip() or "http://127.0.0.1:19530"
        self.collection_name = collection_name.strip() or "knowledge_chunks"
        self.client = client
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.dimension: int | None = None

    def _client(self) -> Any:
        """输入：无；使用构造时的客户端或保存的 Milvus 地址。

        输出：可执行 Milvus collection 操作的客户端。
        功能：延迟建立向量服务连接，使 Hermes 插件在知识库未部署时仍可正常启动。
        """

        if self.client is None:
            self.client = MilvusClient(uri=self.uri, timeout=self.timeout_seconds)
        return self.client

    def _ensure_collection(self, dimension: int) -> None:
        """输入：本次 Embedding 向量维度 ``dimension``。

        输出：无；目标知识 collection 不存在时创建，存在时校验维度并加载。
        功能：让知识库与长期记忆使用不同 collection，同时保证向量模型切换不会静默写入错误维度。
        """

        if self.dimension == dimension:
            return
        client = self._client()
        if client.has_collection(
            collection_name=self.collection_name,
            timeout=self.timeout_seconds,
        ):
            fields = client.describe_collection(
                collection_name=self.collection_name,
                timeout=self.timeout_seconds,
            )["fields"]
            vector = next(field for field in fields if field["name"] == "vector")
            if int(vector["params"]["dim"]) != dimension:
                raise RuntimeError("知识库 collection 与 Embedding 维度不一致")
        else:
            schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
            schema.add_field("content", DataType.VARCHAR, max_length=4096)
            schema.add_field("doc_type", DataType.VARCHAR, max_length=32)
            schema.add_field("category", DataType.VARCHAR, max_length=64)
            schema.add_field("brand", DataType.VARCHAR, max_length=64)
            schema.add_field("effective_at", DataType.INT64)
            schema.add_field("expire_at", DataType.INT64)
            schema.add_field("source_title", DataType.VARCHAR, max_length=256)
            schema.add_field("article_number", DataType.VARCHAR, max_length=128)
            schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dimension)
            index = MilvusClient.prepare_index_params()
            index.add_index("vector", index_type="AUTOINDEX", metric_type="COSINE")
            client.create_collection(
                collection_name=self.collection_name,
                schema=schema,
                index_params=index,
                timeout=self.timeout_seconds,
            )
        client.load_collection(collection_name=self.collection_name, timeout=self.timeout_seconds)
        self.dimension = dimension

    def upsert(self, chunk: KnowledgeChunk, vector: Sequence[float]) -> None:
        """输入：已切分的知识片段 ``chunk`` 及其非空有限向量 ``vector``。

        输出：无；新增或覆盖同一 ``chunk_id`` 的知识索引记录。
        功能：同时写入原文、引用元数据和向量，使检索结果无需回查其他数据源即可生成引用上下文。
        """

        values = [float(value) for value in vector]
        if not values or not all(math.isfinite(value) for value in values):
            raise ValueError("知识库向量不能为空且必须为有限数值")
        if len(chunk.content) > 4096:
            raise ValueError("知识片段正文不能超过 4096 字符")
        self._ensure_collection(len(values))
        self._client().upsert(
            collection_name=self.collection_name,
            data=[
                {
                    "chunk_id": chunk.chunk_id,
                    "content": chunk.content,
                    "doc_type": chunk.doc_type.value,
                    "category": chunk.category,
                    "brand": chunk.brand,
                    "effective_at": _utc_timestamp(chunk.effective_date),
                    "expire_at": _utc_timestamp(chunk.expire_date),
                    "source_title": chunk.source_title,
                    "article_number": chunk.article_number,
                    "vector": values,
                }
            ],
            timeout=self.timeout_seconds,
        )

    def search(self, vector: Sequence[float], limit: int) -> list[tuple[KnowledgeChunk, float]]:
        """输入：查询向量 ``vector`` 和候选数量上限 ``limit``。

        输出：Milvus COSINE 命中的 ``(知识片段, 相似度)`` 列表。
        功能：读取重排序所需的全文和元数据字段，不在向量库层预先排除泛化法规或过期条款。
        """

        values = [float(value) for value in vector]
        if not values or limit <= 0:
            return []
        self._ensure_collection(len(values))
        hits = self._client().search(
            collection_name=self.collection_name,
            data=[values],
            limit=limit,
            output_fields=[
                "chunk_id",
                "content",
                "doc_type",
                "category",
                "brand",
                "effective_at",
                "expire_at",
                "source_title",
                "article_number",
            ],
            search_params={"metric_type": "COSINE", "params": {}},
            timeout=self.timeout_seconds,
        )[0]
        results: list[tuple[KnowledgeChunk, float]] = []
        for hit in hits:
            fields = hit.get("entity", hit)
            try:
                chunk = KnowledgeChunk(
                    chunk_id=str(fields.get("chunk_id") or hit.get("id")),
                    content=str(fields.get("content") or ""),
                    doc_type=DocType(str(fields.get("doc_type") or DocType.FAQ.value)),
                    category=str(fields.get("category") or ""),
                    brand=str(fields.get("brand") or ""),
                    effective_date=_from_timestamp(fields.get("effective_at")),
                    expire_date=_from_timestamp(fields.get("expire_at")),
                    source_title=str(fields.get("source_title") or ""),
                    article_number=str(fields.get("article_number") or ""),
                )
            except ValueError:
                continue
            results.append((chunk, float(hit.get("distance", 0.0))))
        return results


class KnowledgeRAG:
    """按语义、关键词、元数据和时效性检索售后知识。"""

    def __init__(self, vector_store: KnowledgeMilvusStore, embedder: Any, top_k: int = 3) -> None:
        """输入：知识 Milvus 存储、可生成向量的 Embedding 客户端和返回数量。

        输出：可执行知识库混合检索的对象。
        功能：保存两个外部依赖，并将最终引用片段数量限制在 1 到 5 条。
        """

        self.vector_store = vector_store
        self.embedder = embedder
        self.top_k = min(max(1, int(top_k)), 5)

    @staticmethod
    def _score(
        query: str,
        entities: Mapping[str, str],
        chunk: KnowledgeChunk,
        similarity: float,
        now: datetime,
    ) -> float:
        """输入：问题、已提取实体、候选片段、余弦相似度及当前时刻。

        输出：叠加关键词、适用范围、来源优先级和时效后的非负相关度。
        功能：将通用法规、品牌条款和过期政策按文档约定进行可解释的二次排序。
        0.7 × 语义相似度 + 0.3 × 关键词覆盖率
        然后继续乘上业务权重：
        品类相同：× 1.5
        品类不同：× 0.3
        品牌相同：× 1.3

        品牌政策：× 1.0
        苏宁制度：× 0.9
        维修手册：× 0.8
        国家法规：× 0.7
        FAQ：× 0.5

        尚未生效或已经过期：× 0.1
        """

        semantic = min(max((float(similarity) + 1.0) / 2.0, 0.0), 1.0)
        score = 0.7 * semantic + 0.3 * _keyword_score(query, chunk)
        category = str(entities.get("category") or "")
        if category and chunk.category:
            score *= 1.5 if category == chunk.category else 0.3
        brand = str(entities.get("brand") or "")
        if brand and chunk.brand and brand == chunk.brand:
            score *= 1.3
        score *= PRIORITY_WEIGHTS[chunk.doc_type]
        current_time = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        if (chunk.expire_date and chunk.expire_date < current_time) or (
            chunk.effective_date and chunk.effective_date > current_time
        ):
            score *= 0.1
        return score

    def search(
        self,
        query: str,
        entities: Mapping[str, str] | None = None,
        now: datetime | None = None,
    ) -> list[KnowledgeResult]:
        """输入：非空用户问题 ``query``、可选实体和可替换的当前时间。

        输出：按混合分数降序排列的至多 Top3 知识片段。
        功能：再结合先以 Embedding 在 Milvus 取候选，关键词、品类、品牌、来源优先级和生效日期重排，并优先保留不同来源。
        """

        normalized_query = str(query or "").strip()
        if not normalized_query:
            return []
        resolved_entities = dict(entities or extract_entities(normalized_query))
        vector = self.embedder.embed(normalized_query)
        candidates = self.vector_store.search(vector, max(10, self.top_k * 3))
        current_time = now or datetime.now(timezone.utc)
        results = [
            KnowledgeResult(
                chunk=chunk,
                score=self._score(
                    normalized_query,
                    resolved_entities,
                    chunk,
                    similarity,
                    current_time,
                ),
            )
            for chunk, similarity in candidates
        ]
        ranked = sorted(results, key=lambda item: item.score, reverse=True)
        selected: list[KnowledgeResult] = []
        selected_titles: set[str] = set()
        for result in ranked:
            if result.chunk.source_title not in selected_titles:
                selected.append(result)
                selected_titles.add(result.chunk.source_title)
            if len(selected) == self.top_k:
                return selected
        for result in ranked:
            if result not in selected:
                selected.append(result)
            if len(selected) == self.top_k:
                break
        return selected


def format_knowledge_context(results: Sequence[KnowledgeResult]) -> str:
    """输入：已经排序的知识检索结果序列 ``results``。

    输出：可由 ``pre_llm_call`` 注入的中文引用上下文；无结果时返回空字符串。
    功能：按固定编号提供原文与来源，并明确要求模型只基于片段作答且逐条标注引用编号。
    """

    if not results:
        return ""
    parts = [
        "以下是售后知识库检索片段，只能依据这些片段回答政策、保修或维修问题。",
        "结论后必须标注引用编号，如 [知识来源1]；片段无法支持时应明确说明。",
    ]
    for index, result in enumerate(results, start=1):
        chunk = result.chunk
        source = " ".join(item for item in (chunk.source_title, chunk.article_number) if item)
        scope = "；".join(
            item
            for item in (
                f"品类：{chunk.category}" if chunk.category else "",
                f"品牌：{chunk.brand}" if chunk.brand else "",
            )
            if item
        )
        parts.extend(
            [
                "",
                f"[知识来源{index}] {source or '未命名来源'}",
                scope,
                chunk.content,
            ]
        )
    return "\n".join(item for item in parts if item)


__all__ = [
    "BRAND_KEYWORDS",
    "CATEGORY_KEYWORDS",
    "COMPONENT_KEYWORDS",
    "DocType",
    "KnowledgeChunk",
    "KnowledgeMilvusStore",
    "KnowledgeRAG",
    "KnowledgeResult",
    "PRIORITY_WEIGHTS",
    "extract_entities",
    "format_knowledge_context",
    "should_search_knowledge",
]
