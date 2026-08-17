"""验证长期记忆模型、SQLite/FTS、提取门禁与双通道召回。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from suning_context_runtime.long_memory_extractor import MemoryExtractor
from suning_context_runtime.long_memory_models import (
    MemoryExtraction,
    MemoryRecord,
    build_memory_key,
)
from suning_context_runtime.long_memory_retriever import LongMemoryRetriever
from suning_context_runtime.long_memory_store import (
    DashScopeEmbeddingClient,
    MilvusVectorStore,
    SQLiteMemoryStore,
)


def _extraction(
    *,
    should_record: bool = True,
    topic: str | None = "return_analysis",
    entities: dict[str, object] | None = None,
    filters: dict[str, object] | None = None,
    conclusion: str | None = "空调退单中安装问题长期占比最高。",
    evidence: dict[str, object] | None = None,
    confidence: float = 0.9,
) -> MemoryExtraction:
    """输入：可覆盖的长期记忆提取字段。

    输出：字段完整且适合测试门禁与持久化的 ``MemoryExtraction``。
    功能：集中构造默认合法提取结果，减少各测试中的重复数据。
    """

    return MemoryExtraction(
        should_record=should_record,
        topic=topic,
        entities=entities or {"category": "空调"},
        filters=filters or {"region": "华东", "date_range_days": 30},
        conclusion=conclusion,
        evidence=evidence or {"sample_count": 120},
        confidence=confidence,
    )


def _record(
    record_id: str,
    *,
    user_id: str = "user-a",
    topic: str = "return_analysis",
    entities: dict[str, object] | None = None,
    filters: dict[str, object] | None = None,
    conclusion: str = "空调退单中安装问题长期占比最高。",
    evidence: dict[str, object] | None = None,
    confidence: float = 0.9,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    version: int = 1,
) -> MemoryRecord:
    """输入：记录标识以及可覆盖的用户、业务内容、时间和版本字段。

    输出：带稳定 ``memory_key`` 的 ``MemoryRecord`` 测试实例。
    功能：按正式键生成规则创建记录，供存储和召回测试复用。
    """

    resolved_entities = entities or {"category": "空调"}
    resolved_filters = filters or {"region": "华东", "date_range_days": 30}
    resolved_created_at = created_at or datetime(2026, 8, 1, tzinfo=timezone.utc)
    resolved_updated_at = updated_at or resolved_created_at
    return MemoryRecord(
        id=record_id,
        memory_key=build_memory_key(
            user_id=user_id,
            topic=topic,
            entities=resolved_entities,
            filters=resolved_filters,
        ),
        user_id=user_id,
        topic=topic,
        entities=resolved_entities,
        filters=resolved_filters,
        conclusion=conclusion,
        evidence=evidence or {"sample_count": 120},
        confidence=confidence,
        created_at=resolved_created_at,
        updated_at=resolved_updated_at,
        version=version,
    )


def _records_from_fts_hits(
    hits: list[tuple[MemoryRecord, float]],
) -> list[MemoryRecord]:
    """输入：SQLite FTS 返回的记录与 BM25 分数二元组。

    输出：保持召回顺序的 ``MemoryRecord`` 列表。
    功能：让存储测试聚焦记录内容，而不耦合 BM25 的具体数值范围。
    """

    return [record for record, _score in hits]


def test_build_memory_key_is_canonical_and_user_scoped() -> None:
    """输入：键顺序不同但语义相同的实体、筛选条件以及两个用户。

    输出：无；断言同用户同语义得到稳定键，不同用户得到不同键。
    功能：验证字典序列化顺序不制造重复记忆，并保证用户隔离进入唯一键。
    """

    first = build_memory_key(
        user_id="user-a",
        topic="return_analysis",
        entities={"category": "空调", "reason": "安装问题"},
        filters={"region": "华东", "date_range_days": 30},
    )
    reordered = build_memory_key(
        user_id="user-a",
        topic="return_analysis",
        entities={"reason": "安装问题", "category": "空调"},
        filters={"date_range_days": 30, "region": "华东"},
    )
    other_user = build_memory_key(
        user_id="user-b",
        topic="return_analysis",
        entities={"category": "空调", "reason": "安装问题"},
        filters={"region": "华东", "date_range_days": 30},
    )

    assert first == reordered
    assert first != other_user
    assert first == build_memory_key(
        user_id="user-a",
        topic="return_case",
        entities={"category": " 空调 ", "reason": "安装问题 "},
        filters={"region": " 华东", "date_range_days": 30},
    )


def test_memory_extraction_dict_defaults_are_not_shared() -> None:
    """输入：两个未显式提供实体、筛选和证据的提取结果。

    输出：无；断言修改一个实例的字典不会污染另一个实例。
    功能：验证 Pydantic 模型为可变字段提供独立默认值。
    """

    first = MemoryExtraction(should_record=False)
    second = MemoryExtraction(should_record=False)

    first.entities["category"] = "空调"
    first.filters["region"] = "华东"
    first.evidence["count"] = 1

    assert second.entities == {}
    assert second.filters == {}
    assert second.evidence == {}


def test_sqlite_upsert_updates_version_and_fts_without_replacing_identity(
    tmp_path: Path,
) -> None:
    """输入：同一 ``memory_key`` 的旧结论和携带不同 ID、创建时间的新结论。

    输出：无；断言更新保留原 ID 与创建时间、版本加一且 FTS 只命中新结论。
    功能：验证结构化记忆原位演进及 SQLite Trigger 的更新同步语义。
    """

    store = SQLiteMemoryStore(tmp_path / "memory.db")
    original = _record(
        "memory-original",
        conclusion="安装问题曾是空调退单的首要原因。",
        version=9,
    )
    inserted = store.upsert(original)
    incoming = _record(
        "memory-replacement",
        conclusion="产品质量问题现为退单首要原因。",
        evidence={"sample_count": 180},
        confidence=0.95,
        created_at=original.created_at + timedelta(days=10),
        updated_at=original.updated_at + timedelta(days=10),
    )

    updated = store.upsert(incoming)
    persisted = store.get_by_key(original.memory_key)

    assert inserted.id == "memory-original"
    assert inserted.version == 1
    assert updated.id == original.id
    assert updated.created_at == original.created_at
    assert updated.updated_at == incoming.updated_at
    assert updated.version == 2
    assert updated.conclusion == incoming.conclusion
    assert updated.evidence == {"sample_count": 180}
    assert persisted == updated
    assert store.search_fts("user-a", "安装问题", 10) == []
    new_hits = _records_from_fts_hits(
        store.search_fts("user-a", "产品质量", 10)
    )
    assert [record.id for record in new_hits] == [original.id]


def test_sqlite_fts_supports_chinese_substrings_and_safe_special_characters(
    tmp_path: Path,
) -> None:
    """输入：连续中文结论以及包含 FTS 运算符、引号和括号的用户查询。

    输出：无；断言中文子串可命中，特殊字符不会抛出语法异常或返回他人数据。
    功能：验证 trigram 中文检索与原始用户文本的安全 MATCH 转义。
    """

    store = SQLiteMemoryStore(tmp_path / "memory.db")
    own_record = _record(
        "memory-own",
        conclusion="空调退单质量问题呈持续上升趋势。",
    )
    foreign_record = _record(
        "memory-foreign",
        user_id="user-b",
        conclusion="空调退单质量问题呈持续上升趋势。",
    )
    store.upsert(own_record)
    store.upsert(foreign_record)

    substring_hits = _records_from_fts_hits(
        store.search_fts("user-a", "空调退单", 10)
    )
    short_entity_hits = _records_from_fts_hits(
        store.search_fts("user-a", "空调", 10)
    )
    special_hits = _records_from_fts_hits(
        store.search_fts("user-a", '空调 " OR * (退单) -', 10)
    )

    assert [record.id for record in substring_hits] == [own_record.id]
    assert [record.id for record in short_entity_hits] == [own_record.id]
    assert all(record.user_id == "user-a" for record in special_hits)


def test_dashscope_embedding_uses_bounded_timeout_and_locks_dimension() -> None:
    """输入：返回固定向量的 DashScope 调用替身和四秒超时配置。

    输出：无；断言 SDK 收到有界超时、向量转为浮点数，并保存实际维度。
    功能：防止回答前语义召回沿用 SDK 的五分钟默认等待，并为 Milvus 建表提供真实维度。
    """

    caller = Mock(
        return_value={
            "status_code": 200,
            "output": {"embeddings": [{"embedding": [1, 2, 3]}]},
        }
    )
    embedder = DashScopeEmbeddingClient(
        "test-key",
        "text-embedding-v3",
        caller=caller,
        timeout_seconds=4,
    )

    vector = embedder.embed("空调退单长期趋势")

    assert vector == [1.0, 2.0, 3.0]
    assert embedder.dimension == 3
    caller.assert_called_once_with(
        model="text-embedding-v3",
        input=["空调退单长期趋势"],
        api_key="test-key",
        request_timeout=4.0,
    )


def test_dashscope_embedding_batches_trigger_patterns_by_ten() -> None:
    """输入：十一条触发语句与分别返回十条、一条向量的 DashScope 调用替身。

    输出：无；断言按原输入顺序返回全部向量且 SDK 调用被拆为两批。
    功能：验证路由预热遵守 DashScope 同步 Embedding 的单批上限，不会在大量 Skill 问法时被接口拒绝。
    """

    def response(count: int) -> dict[str, object]:
        """输入：本批要返回的向量数量 ``count``。

        输出：含 ``count`` 条二维向量的 DashScope 成功响应字典。
        功能：为批量边界测试构造与 SDK 返回结构相同的最小响应。
        """

        return {
            "status_code": 200,
            "output": {"embeddings": [{"embedding": [index, 1]} for index in range(count)]},
        }
    caller = Mock(side_effect=[response(10), response(1)])
    embedder = DashScopeEmbeddingClient("test-key", caller=caller)

    vectors = embedder.embed_many([f"退单问法{index}" for index in range(11)])

    assert len(vectors) == 11
    assert caller.call_count == 2
    assert len(caller.call_args_list[0].kwargs["input"]) == 10
    assert len(caller.call_args_list[1].kwargs["input"]) == 1


def test_embedding_and_milvus_reject_non_finite_vectors() -> None:
    """输入：分别含 NaN 和 Infinity 的模型响应及 Milvus 查询向量。

    输出：无；断言两层客户端均在发往向量库前拒绝非有限数值。
    功能：防止异常模型输出污染 collection 或产生不可预测的相似度结果。
    """

    embedder = DashScopeEmbeddingClient(
        "test-key",
        caller=Mock(
            return_value={
                "status_code": 200,
                "output": {"embeddings": [{"embedding": [0.1, float("nan")]}]},
            }
        ),
    )
    milvus_client = Mock()
    vector_store = MilvusVectorStore(
        "http://milvus:19530",
        client=milvus_client,
    )

    with pytest.raises(RuntimeError, match="非有限"):
        embedder.embed("异常向量")
    with pytest.raises(ValueError, match="非有限"):
        vector_store.search("user-a", [0.1, float("inf")], 3)

    milvus_client.has_collection.assert_not_called()


def test_milvus_search_checks_dimension_timeout_and_user_filter() -> None:
    """输入：三维现有 collection 和同时返回本用户、其他用户命中的 Milvus 替身。

    输出：无；断言操作使用有界超时及用户表达式，并再次丢弃显式跨用户命中。
    功能：验证语义召回的 collection 兼容检查和双层用户隔离。
    """

    client = Mock()
    client.has_collection.return_value = True
    client.describe_collection.return_value = {
        "fields": [{"name": "vector", "params": {"dim": "3"}}]
    }
    client.search.return_value = [
        [
            {
                "id": "memory-own",
                "distance": 0.91,
                "entity": {"user_id": "user-a"},
            },
            {
                "id": "memory-foreign",
                "distance": 0.99,
                "entity": {"user_id": "user-b"},
            },
        ]
    ]
    store = MilvusVectorStore(
        "http://milvus:19530",
        "memory_vectors",
        client=client,
        timeout_seconds=2,
    )

    hits = store.search("user-a", [0.1, 0.2, 0.3], 6)

    assert hits == [("memory-own", 0.91)]
    client.has_collection.assert_called_once_with(
        collection_name="memory_vectors",
        timeout=2.0,
    )
    assert client.search.call_args.kwargs["filter"] == 'user_id == "user-a"'
    assert client.search.call_args.kwargs["timeout"] == 2.0


def test_milvus_rejects_existing_collection_dimension_mismatch() -> None:
    """输入：声明 1024 维的现有 collection 和三维查询向量。

    输出：无；断言在搜索或改写任何向量前抛出明确维度异常。
    功能：防止配置指向不兼容 collection 时删除重建或静默损坏已有数据。
    """

    client = Mock()
    client.has_collection.return_value = True
    client.describe_collection.return_value = {
        "fields": [{"name": "vector", "params": {"dim": 1024}}]
    }
    store = MilvusVectorStore(
        "http://milvus:19530",
        client=client,
    )

    with pytest.raises(RuntimeError, match="维度不一致"):
        store.search("user-a", [0.1, 0.2, 0.3], 3)

    client.search.assert_not_called()


def test_milvus_first_upsert_creates_required_schema_and_stable_payload() -> None:
    """输入：不存在 collection 的 Milvus 替身、三维向量和固定长期记忆。

    输出：无；断言首次写入创建完整字段与 COSINE 索引，并以 SQLite ID upsert。
    功能：锁定由实际 Embedding 维度建表和新版本复用稳定主键的首次部署路径。
    """

    client = Mock()
    client.has_collection.return_value = False
    store = MilvusVectorStore(
        "http://milvus:19530",
        "memory_vectors",
        client=client,
        timeout_seconds=2,
    )
    record = _record("memory-first")

    store.upsert(record, [0.1, 0.2, 0.3])

    create_kwargs = client.create_collection.call_args.kwargs
    field_names = [field.name for field in create_kwargs["schema"].fields]
    assert field_names == ["id", "user_id", "topic", "vector", "updated_at"]
    vector_field = next(
        field for field in create_kwargs["schema"].fields if field.name == "vector"
    )
    assert vector_field.params["dim"] == 3
    assert create_kwargs["index_params"] is not None
    client.load_collection.assert_called_once_with(
        collection_name="memory_vectors",
        timeout=2.0,
    )
    payload = client.upsert.call_args.kwargs["data"][0]
    assert payload["id"] == record.id
    assert payload["user_id"] == "user-a"
    assert payload["vector"] == [0.1, 0.2, 0.3]


def test_sqlite_fts_and_get_many_enforce_user_isolation(tmp_path: Path) -> None:
    """输入：两个用户拥有文本相同的长期记忆以及混合 ID 查询。

    输出：无；断言 FTS 和批量加载均只返回指定用户的记录。
    功能：锁定关键词与向量回表两条路径共同遵守飞书用户隔离边界。
    """

    store = SQLiteMemoryStore(tmp_path / "memory.db")
    own_record = _record("memory-a", user_id="user-a")
    foreign_record = _record("memory-b", user_id="user-b")
    store.upsert(own_record)
    store.upsert(foreign_record)

    fts_records = _records_from_fts_hits(
        store.search_fts("user-a", "空调退单", 10)
    )
    loaded = store.get_many(
        "user-a",
        [own_record.id, foreign_record.id],
    )

    assert [record.id for record in fts_records] == [own_record.id]
    assert loaded == {own_record.id: own_record}


def test_memory_extractor_returns_structured_result_and_applies_all_gates() -> None:
    """输入：结构化模型响应以及记录意愿、置信度、主题和结论的边界组合。

    输出：无；断言 ``extract`` 固定返回模型，且仅完整可信白名单结果可沉淀。
    功能：验证 LLM 结构化调用与第一版长期记忆的四项写入门禁。
    """

    accepted = _extraction()
    structured_model = Mock()
    structured_model.invoke.return_value = accepted
    model = Mock()
    model.with_structured_output.return_value = structured_model
    extractor = MemoryExtractor(model, min_confidence=0.7)

    result = extractor.extract(
        user_message="上次空调退单的主要原因是什么？",
        assistant_response="多次分析显示安装问题长期占比最高。",
        slot_context={
            "topic": "return_analysis",
            "filters": {"category": "空调"},
        },
        conversation_history=[
            {
                "role": "user",
                "content": "原始历史问题",
                "api_content": "插件注入的旧长期记忆，不得再次沉淀",
            },
            {"role": "tool", "content": "不应发送给提取模型"},
        ],
    )

    rejected = (
        _extraction(should_record=False),
        _extraction(confidence=0.699),
        _extraction(topic="product_query"),
        _extraction(conclusion="  "),
    )
    assert result == accepted
    assert extractor.is_recordable(result)
    assert extractor.is_recordable(
        _extraction(topic="order_trace", conclusion="订单链路异常已确认。")
    )
    assert all(not extractor.is_recordable(item) for item in rejected)
    messages = structured_model.invoke.call_args.args[0]
    payload = json.loads(messages[1].content)
    assert payload["conversation_history"] == [
        {"role": "user", "content": "原始历史问题"}
    ]
    assert "插件注入的旧长期记忆" not in messages[1].content


def test_retriever_fuses_both_channels_and_limits_results_to_top_three() -> None:
    """输入：五条 SQLite 候选、五条向量候选及固定查询向量。

    输出：无；断言双命中来源标为 both、排名优于单通道且最终不超过三条。
    功能：验证关键词与语义各占一半的候选融合、去重和 Top3 截断。
    """

    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    records = {
        f"memory-{index}": _record(
            f"memory-{index}",
            updated_at=now,
            conclusion=f"长期结论{index}",
        )
        for index in range(1, 6)
    }
    store = Mock()
    store.search_fts.return_value = [
        (records["memory-1"], -10.0),
        (records["memory-2"], -8.0),
        (records["memory-3"], -6.0),
        (records["memory-4"], -4.0),
    ]
    store.get_many.return_value = records
    embedder = Mock()
    embedder.embed.return_value = [0.1, 0.2, 0.3]
    vector_store = Mock()
    vector_store.search.return_value = [
        ("memory-1", 0.95),
        ("memory-2", 0.85),
        ("memory-3", 0.75),
        ("memory-5", 0.65),
    ]
    retriever = LongMemoryRetriever(
        store,
        embedder,
        vector_store,
        top_k=3,
        half_life_days=30,
    )

    results = retriever.retrieve(
        user_id="user-a",
        user_message="以前空调退单主要是什么问题？",
        slot_context={"topic": "return_analysis", "filters": {}},
        now=now,
    )

    assert len(results) == 3
    assert results[0].record.id == "memory-1"
    assert results[0].source == "both"
    assert results[0].score > 0
    assert all(result.source == "both" for result in results)


def test_retriever_applies_thirty_day_half_life() -> None:
    """输入：同一双通道候选在更新时间当天和三十天后的两次召回。

    输出：无；断言三十天后的最终分数约为原分数一半。
    功能：验证融合完成后按更新时间应用 30 天指数半衰减。
    """

    updated_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    record = _record("memory-decay", updated_at=updated_at)
    store = Mock()
    store.search_fts.return_value = [(record, -5.0)]
    store.get_many.return_value = {record.id: record}
    embedder = Mock()
    embedder.embed.return_value = [0.2, 0.4]
    vector_store = Mock()
    vector_store.search.return_value = [(record.id, 0.8)]
    retriever = LongMemoryRetriever(
        store,
        embedder,
        vector_store,
        top_k=3,
        half_life_days=30,
    )

    fresh = retriever.retrieve(
        user_id="user-a",
        user_message="空调退单原因",
        slot_context={},
        now=updated_at,
    )
    aged = retriever.retrieve(
        user_id="user-a",
        user_message="空调退单原因",
        slot_context={},
        now=updated_at + timedelta(days=30),
    )

    assert len(fresh) == len(aged) == 1
    assert aged[0].score == pytest.approx(fresh[0].score * 0.5)


def test_retriever_falls_back_to_fts_when_milvus_fails() -> None:
    """输入：可用的 SQLite 关键词候选以及抛出异常的 Milvus 搜索替身。

    输出：无；断言召回不中断，并返回仅标记为 fts5 的关键词结果。
    功能：验证向量服务不可用时长期记忆仍通过本地 FTS 降级工作。
    """

    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    record = _record("memory-fts", updated_at=now)
    store = Mock()
    store.search_fts.return_value = [(record, -7.0)]
    store.get_many.return_value = {}
    embedder = Mock()
    embedder.embed.return_value = [0.1, 0.2]
    vector_store = Mock()
    vector_store.search.side_effect = RuntimeError("milvus unavailable")
    retriever = LongMemoryRetriever(
        store,
        embedder,
        vector_store,
        top_k=3,
        half_life_days=30,
    )

    results = retriever.retrieve(
        user_id="user-a",
        user_message="空调退单原因",
        slot_context={},
        now=now,
    )

    assert len(results) == 1
    assert results[0].record == record
    assert results[0].source == "fts5"
    assert results[0].score > 0
