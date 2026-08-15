"""定义长期记忆的结构化提取、持久化记录和召回结果。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


MemoryTopic = Literal["return_analysis", "order_trace"]
RetrievalSource = Literal["fts5", "vector", "both"]
MEMORY_TOPIC_ALIASES = {
    "return_analysis": "return_analysis",
    "return_case": "return_analysis",
    "order_trace": "order_trace",
    "order_query": "order_trace",
}


class MemoryExtraction(BaseModel):
    """LLM 对一轮对话是否值得沉淀及其业务事实的结构化判断。"""

    model_config = ConfigDict(extra="forbid")

    should_record: bool
    topic: str | None = None
    entities: dict[str, object] = Field(default_factory=dict)
    filters: dict[str, object] = Field(default_factory=dict)
    conclusion: str | None = None
    evidence: dict[str, object] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class MemoryRecord(BaseModel):
    """SQLite 与 Milvus 共享标识的当前版本用户长期记忆。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    memory_key: str
    user_id: str
    topic: MemoryTopic
    entities: dict[str, object] = Field(default_factory=dict)
    filters: dict[str, object] = Field(default_factory=dict)
    conclusion: str
    evidence: dict[str, object] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    created_at: datetime
    updated_at: datetime
    version: int = Field(default=1, ge=1)


class RetrievalResult(BaseModel):
    """完成双通道融合与时间衰减后的单条长期记忆结果。"""

    model_config = ConfigDict(extra="forbid")

    record: MemoryRecord
    score: float = Field(ge=0.0)
    source: RetrievalSource


def utc_now() -> datetime:
    """输入：无；读取当前系统时钟。

    输出：带 UTC 时区的当前时间。
    功能：为新建和更新记忆提供统一、可序列化的时间基准。
    """

    return datetime.now(timezone.utc)


def _json_value(value: Any) -> Any:
    """输入：长期记忆实体、条件或证据中的任意嵌套值。

    输出：只含稳定 JSON 类型的规范值；不支持的对象或非有限浮点数会抛出异常。
    功能：显式规范化字典、序列、集合与时间，避免对象默认字符串表示破坏唯一键稳定性。
    """

    if isinstance(value, str):
        return value.strip()
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError("长期记忆 JSON 不允许非有限浮点数")
        return value
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=timezone.utc)
        return normalized.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized_items = [_json_value(item) for item in value]
        return sorted(
            normalized_items,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    raise TypeError(f"长期记忆值不支持 JSON 序列化: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """输入：由基础类型、集合、序列、字典或时间组成的业务值 ``value``。

    输出：键排序、无多余空白且保留中文的稳定 JSON；不支持的对象会抛出异常。
    功能：统一唯一键、检索文本和 SQLite JSON 字段的确定性序列化方式。
    """

    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def normalize_memory_topic(topic: str | None) -> MemoryTopic | None:
    """输入：LLM 或短期槽位提供的可选话题 ``topic``。

    输出：映射后的两个长期记忆白名单主题之一；未知或空话题返回 ``None``。
    功能：把 ``return_case``、``order_query`` 等会话主题收敛到稳定持久化主题。
    """

    normalized = str(topic or "").strip()
    mapped = MEMORY_TOPIC_ALIASES.get(normalized)
    if mapped == "return_analysis":
        return "return_analysis"
    if mapped == "order_trace":
        return "order_trace"
    return None


def build_memory_key(
    user_id: str,
    topic: str,
    entities: dict[str, object],
    filters: dict[str, object],
) -> str:
    """输入：可信用户、规范主题、业务实体和形成结论时的筛选条件。

    输出：由规范化内容生成的 SHA-256 十六进制稳定键。
    功能：让同用户同主题/实体/条件更新原记录，同时从键空间隔离不同用户。
    """

    payload = {
        "user_id": str(user_id).strip(),
        "topic": normalize_memory_topic(topic) or str(topic).strip(),
        "entities": dict(entities or {}),
        "filters": dict(filters or {}),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def build_embedding_text(record: MemoryRecord) -> str:
    """输入：已经持久化并带当前结论的 ``MemoryRecord``。

    输出：包含主题、实体、筛选条件和结论的稳定中文向量化文本。
    功能：使写入与后续语义查询围绕相同的业务信息生成 Embedding。
    """

    return "\n".join(
        (
            f"主题：{record.topic}",
            f"实体：{canonical_json(record.entities)}",
            f"筛选条件：{canonical_json(record.filters)}",
            f"结论：{record.conclusion}",
        )
    )


__all__ = [
    "MEMORY_TOPIC_ALIASES",
    "MemoryExtraction",
    "MemoryRecord",
    "MemoryTopic",
    "RetrievalResult",
    "RetrievalSource",
    "build_embedding_text",
    "build_memory_key",
    "canonical_json",
    "normalize_memory_topic",
    "utc_now",
]
