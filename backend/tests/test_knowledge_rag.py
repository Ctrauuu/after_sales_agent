"""验证售后知识库的混合重排、引用上下文与 Hermes 回答前注入。"""

from __future__ import annotations

import runpy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from suning_context_runtime import (
    DocType,
    KnowledgeChunk,
    KnowledgeMilvusStore,
    KnowledgeRAG,
    KnowledgeResult,
    extract_entities,
    format_knowledge_context,
    should_search_knowledge,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_HOOKS = (
    PROJECT_ROOT / ".hermes" / "plugins" / "suning-rbac-bridge" / "knowledge_hooks.py"
)
INGEST_SCRIPT = PROJECT_ROOT / "backend" / "scripts" / "ingest_knowledge.py"
NOW = datetime(2026, 8, 13, tzinfo=timezone.utc)


def _chunk(
    chunk_id: str,
    doc_type: DocType,
    content: str,
    *,
    category: str = "",
    brand: str = "",
    expire_date: datetime | None = None,
    source_title: str = "",
) -> KnowledgeChunk:
    """输入：片段 ID、来源类型、原文、可选适用范围、失效时间和来源标题。

    输出：带固定来源标题和条款号的测试 ``KnowledgeChunk``。
    功能：以紧凑方式构造不同来源和时效性候选，供混合排序测试复用。
    """

    return KnowledgeChunk(
        chunk_id=chunk_id,
        content=content,
        doc_type=doc_type,
        category=category,
        brand=brand,
        effective_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        expire_date=expire_date,
        source_title=source_title or f"{chunk_id} 来源",
        article_number="第一条",
    )


def test_extract_entities_and_policy_gate_use_question_and_context() -> None:
    """输入：含格力空调压缩机保修词的提问和品牌槽位上下文。

    输出：无；断言实体提取与政策类问题门禁结果。
    功能：验证检索只在知识问题触发，并优先使用问题中明确的品类、品牌和部件。
    """

    assert should_search_knowledge("格力空调压缩机保修多久？")
    assert not should_search_knowledge("查询订单 202406010003")
    assert extract_entities(
        "格力空调压缩机保修多久？", {"brand": "海尔"}
    ) == {"category": "空调", "brand": "格力", "component": "压缩机"}


def test_rag_prefers_matching_brand_policy_and_demotes_expired_chunk() -> None:
    """输入：品牌政策、国家法规和过期品牌政策三个 Milvus 候选。

    输出：无；断言命中品牌和品类的现行条款排首位，过期条款大幅降权。
    功能：验证向量、关键词、元数据优先级和生效日期共同参与 RAG 的二次排序。
    """

    current_brand = _chunk(
        "格力政策",
        DocType.BRAND_POLICY,
        "格力家用空调整机免费保修6年，压缩机免费保修10年。",
        category="空调",
        brand="格力",
        source_title="格力电器售后服务政策",
    )
    national_law = _chunk(
        "三包规定",
        DocType.NATIONAL_LAW,
        "家用空调器三包有效期：整机1年，主要部件3年。",
        category="空调",
    )
    expired_brand = _chunk(
        "旧格力政策",
        DocType.BRAND_POLICY,
        "格力空调压缩机保修12年。",
        category="空调",
        brand="格力",
        expire_date=NOW - timedelta(days=1),
        source_title="格力电器售后服务政策",
    )
    store = Mock()
    store.search.return_value = [
        (national_law, 0.95),
        (current_brand, 0.82),
        (expired_brand, 0.99),
    ]
    embedder = Mock()
    embedder.embed.return_value = [0.1, 0.2, 0.3]

    results = KnowledgeRAG(store, embedder).search(
        "格力空调压缩机保修多久？",
        {"category": "空调", "brand": "格力", "component": "压缩机"},
        now=NOW,
    )

    assert [item.chunk.chunk_id for item in results] == ["格力政策", "三包规定", "旧格力政策"]
    assert results[-1].score < results[0].score * 0.2
    store.search.assert_called_once_with([0.1, 0.2, 0.3], 10)


def test_milvus_store_decodes_chunk_metadata_from_search_hit() -> None:
    """输入：包含完整 Milvus entity 字段的一条模拟余弦命中。

    输出：无；断言命中会恢复为带生效时间和条款号的知识片段。
    功能：验证检索层正确读取入库元数据，保证后续排序和引用无需访问原始文件。
    """

    client = Mock()
    client.search.return_value = [
        [
            {
                "distance": 0.9,
                "entity": {
                    "chunk_id": "chunk-1",
                    "content": "空调整机保修6年",
                    "doc_type": "brand_policy",
                    "category": "空调",
                    "brand": "格力",
                    "effective_at": 1704067200,
                    "expire_at": 0,
                    "source_title": "格力政策",
                    "article_number": "一、保修期限",
                },
            }
        ]
    ]
    store = KnowledgeMilvusStore("http://unused", client=client)
    store.dimension = 3

    results = store.search([0.1, 0.2, 0.3], 3)

    assert results[0][0].doc_type is DocType.BRAND_POLICY
    assert results[0][0].effective_date == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert results[0][0].article_number == "一、保修期限"


def test_existing_mock_documents_are_chunked_with_citation_metadata() -> None:
    """输入：项目已有的三包法 mock 文本及真实入库脚本。

    输出：无；断言 LangChain Document 继承国家法规元数据且转换后保留目录级引用定位。
    功能：验证 Loader、Splitter 与领域模型转换的链路，使 RAG 回答可追溯到具体来源而非泛化结论。
    """

    ingest = runpy.run_path(str(INGEST_SCRIPT))
    path = PROJECT_ROOT / "backend" / "mock-files" / "三包法-节选.txt"
    document = ingest["load_document"](path)
    documents = ingest["split_document"](document)
    chunks = ingest["documents_to_chunks"](documents)

    assert document.metadata["source_filename"] == "三包法-节选.txt"
    assert document.metadata["doc_type"] is DocType.NATIONAL_LAW
    assert len(documents) == 8
    assert ingest["chunk_document"](path) == chunks
    warranty_chunk = next(chunk for chunk in chunks if "家用空调器三包有效期" in chunk.content)
    assert warranty_chunk.source_title == "部分商品修理更换退货责任规定（三包规定）"
    assert warranty_chunk.article_number == "三包有效期目录（节选）"


def test_format_context_and_hook_require_numbered_citations() -> None:
    """输入：一条已排序知识结果、政策类问题和带品类槽位的会话上下文。

    输出：无；断言格式化文本含原文来源编号，Hook 将其作为回答前上下文注入。
    功能：锁定“检索片段而非模型记忆作答”及“必须引用来源”的 RAG 输出约束。
    """

    result = KnowledgeResult(
        _chunk(
            "格力政策",
            DocType.BRAND_POLICY,
            "格力家用空调整机免费保修6年。",
            category="空调",
            brand="格力",
        ),
        1.0,
    )
    context = format_knowledge_context([result])
    assert "[知识来源1] 格力政策 来源 第一条" in context
    assert "必须标注引用编号" in context

    namespace = runpy.run_path(str(KNOWLEDGE_HOOKS))
    manager = Mock()
    manager.load_context.return_value = SimpleNamespace(
        slots=SimpleNamespace(filters={"category": "空调"})
    )
    rag = Mock()
    rag.search.return_value = [result]
    hooks = namespace["KnowledgeHooks"](manager, rag)

    injected = hooks.pre_llm_call(
        session_id="session-1",
        user_message="格力空调保修几年？",
    )

    assert injected == {"context": context}
    rag.search.assert_called_once_with(
        "格力空调保修几年？",
        {"category": "空调", "brand": "格力"},
    )
