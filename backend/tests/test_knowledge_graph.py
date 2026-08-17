"""验证售后知识图谱的精确关系查询、RAG 降级和政策入图。"""

from __future__ import annotations

import runpy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import sqlalchemy as sa

from suning_context_runtime import (
    AftersaleKnowledgeGraph,
    DocType,
    KnowledgeChunk,
    KnowledgeResult,
    QueryRouter,
    format_graph_context,
)


KNOWLEDGE_HOOKS = (
    Path(__file__).resolve().parents[2]
    / ".hermes"
    / "plugins"
    / "suning-rbac-bridge"
    / "knowledge_hooks.py"
)


def _engine() -> sa.Engine:
    """输入：无；使用测试内置的售后图谱模拟数据。

    输出：已创建图谱相关表并写入最小关系数据的内存 SQLite Engine。
    功能：提供覆盖品牌、品类、SKU、保修、故障、批次和退单的可重复图查询测试环境。
    """

    engine = sa.create_engine("sqlite://")
    schema = """
        CREATE TABLE t_product_brand (brand_id TEXT PRIMARY KEY, brand_name TEXT);
        CREATE TABLE t_product_category (category_code TEXT PRIMARY KEY, category_name TEXT, parent_code TEXT);
        CREATE TABLE t_product_sku (sku_code TEXT PRIMARY KEY, brand_id TEXT, category_l3_code TEXT);
        CREATE TABLE t_warranty_source (source_id INTEGER PRIMARY KEY, doc_name TEXT, article_number TEXT);
        CREATE TABLE t_component_warranty (warranty_id INTEGER PRIMARY KEY AUTOINCREMENT, category_code TEXT, component_name TEXT, warranty_months INTEGER, coverage TEXT, source_id INTEGER);
        CREATE TABLE t_product_warranty (id INTEGER PRIMARY KEY, sku_code TEXT, warranty_months INTEGER, source_id INTEGER);
        CREATE TABLE t_fault_type (fault_id INTEGER PRIMARY KEY, fault_name TEXT, severity TEXT, category_code TEXT);
        CREATE TABLE t_sku_fault_map (id INTEGER PRIMARY KEY, sku_code TEXT, fault_id INTEGER, occur_count INTEGER);
        CREATE TABLE t_batch_number (batch_id TEXT PRIMARY KEY, sku_code TEXT, produce_date TEXT, total_quantity INTEGER);
        CREATE TABLE t_aftersale_return (return_id INTEGER PRIMARY KEY, batch_id TEXT);
    """
    with engine.begin() as connection:
        for statement in schema.split(";"):
            if statement.strip():
                connection.execute(sa.text(statement))
        connection.execute(sa.text("INSERT INTO t_product_brand VALUES ('B001', '格力')"))
        connection.execute(
            sa.text(
                "INSERT INTO t_product_category VALUES "
                "('C1', '大家电', NULL), ('C1-AC', '空调', 'C1'), "
                "('C1-AC-WG', '壁挂式空调', 'C1-AC')"
            )
        )
        connection.execute(
            sa.text("INSERT INTO t_product_sku VALUES ('SKU-AC-GL-15P', 'B001', 'C1-AC-WG')")
        )
        connection.execute(
            sa.text(
                "INSERT INTO t_warranty_source VALUES "
                "(1, '格力电器售后服务政策（2024版）', '第一条'), "
                "(2, '格力电器售后服务政策（2024版）', '第一条第二款')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO t_component_warranty "
                "(category_code, component_name, warranty_months, coverage, source_id) "
                "VALUES ('C1-AC-WG', '压缩机', 120, '免费更换', 2)"
            )
        )
        connection.execute(
            sa.text("INSERT INTO t_product_warranty VALUES (1, 'SKU-AC-GL-15P', 72, 1)")
        )
        connection.execute(
            sa.text("INSERT INTO t_fault_type VALUES (1, '安装后异响', 'high', 'C1-AC')")
        )
        connection.execute(
            sa.text("INSERT INTO t_sku_fault_map VALUES (1, 'SKU-AC-GL-15P', 1, 12)")
        )
        connection.execute(
            sa.text("INSERT INTO t_batch_number VALUES ('B20240501', 'SKU-AC-GL-15P', '2024-05-01', 5000)")
        )
        connection.execute(sa.text("INSERT INTO t_aftersale_return VALUES (1, 'B20240501')"))
        connection.execute(sa.text("INSERT INTO t_aftersale_return VALUES (5, 'B20240501')"))
    return engine


class _Rag:
    """为路由器提供可观察 RAG 降级结果的轻量替身。"""

    def __init__(self) -> None:
        """输入：无。

        输出：初始化空的查询记录。
        功能：记录路由器传入的查询和实体，证明精确图未命中后会复用既有 RAG。
        """

        self.calls: list[tuple[str, dict[str, str]]] = []

    def search(self, query: str, entities: dict[str, str]) -> list[KnowledgeResult]:
        """输入：RAG 查询文本 ``query`` 和实体字典 ``entities``。

        输出：一条带政策来源的固定知识结果。
        功能：模拟现有知识库首条命中，供图谱降级和开放式问题路由断言使用。
        """

        self.calls.append((query, dict(entities)))
        return [
            KnowledgeResult(
                KnowledgeChunk(
                    chunk_id="rag-1",
                    content="三包条款原文",
                    doc_type=DocType.NATIONAL_LAW,
                    source_title="三包规定",
                    article_number="第十一条",
                ),
                0.8,
            )
        ]


class _PolicyLlm:
    """返回单条部件保修关系 JSON 的政策抽取替身。"""

    def chat(self, prompt: str) -> str:
        """输入：要求抽取政策关系的提示词 ``prompt``。

        输出：一条空调电机保修关系的 JSON 文本。
        功能：验证政策入图会向 LLM 请求受限 JSON 并按返回关系写入业务图表。
        """

        assert "只返回 JSON" in prompt
        return '{"warranties":[{"product":"空调","component":"电机","duration_months":72,"coverage":"免费维修","article_number":"第二条"}]}'


def test_warranty_query_resolves_parent_category_and_cites_source() -> None:
    """输入：格力、二级空调品类和压缩机部件。

    输出：无；断言三级 SKU 品类能匹配二级提问并返回准确期限和来源。
    功能：验证品牌→SKU→品类→部件保修关系不会因 SKU 使用叶子品类而漏查。
    """

    graph = AftersaleKnowledgeGraph(_engine())

    component = graph.query_warranty("格力", "空调", "压缩机")
    whole = graph.query_warranty("格力", "空调")

    assert component is not None
    assert component.answer == "格力空调压缩机保修10年。免费更换"
    assert component.source == "《格力电器售后服务政策（2024版）》第一条第二款"
    assert whole is not None
    assert whole.answer == "格力空调整机保修6年。"


def test_fault_and_product_chain_follow_cte_relationships_without_double_counting() -> None:
    """输入：安装后异响故障和格力空调 SKU。

    输出：无；断言批次退单计数、品类路径、部件和故障均可沿图关系获得。
    功能：验证递归 CTE 的故障品类展开不会把同一批次退单重复计入，并保留 SKU 上游关系链。
    """

    graph = AftersaleKnowledgeGraph(_engine())

    rows = graph.query_fault_association("安装后异响")
    chain = graph.query_product_chain("sku-ac-gl-15p")

    assert rows == [
        {
            "brand": "格力",
            "product": "空调",
            "sku": "SKU-AC-GL-15P",
            "batch": "B20240501",
            "return_count": 2,
        }
    ]
    assert chain["brand"] == "格力"
    assert chain["product"] == "壁挂式空调"
    assert set(chain["category_chain"].split(",")) == {"大家电", "空调", "壁挂式空调"}
    assert chain["components"] == "压缩机"
    assert chain["common_faults"] == "安装后异响"


def test_router_prefers_graph_then_falls_back_to_existing_rag() -> None:
    """输入：两条保修问题、一条含故障名的批次问题和一条解释性三包问题。

    输出：无；断言命中保修/批次图谱、图谱失败降级和直接 RAG 三种路由结果。
    功能：锁定精确事实先查图、故障名可从原问题识别、查不到才检索原文、开放性问题不访问图表的互补边界。
    """

    rag = _Rag()
    router = QueryRouter(AftersaleKnowledgeGraph(_engine()), rag)

    graph_result = router.route_and_query(
        "格力空调压缩机保修多久？",
        {"brand": "格力", "category": "空调", "component": "压缩机"},
    )
    fallback = router.route_and_query(
        "未知空调压缩机保修多久？",
        {"brand": "未知", "category": "空调", "component": "压缩机"},
    )
    batch = router.route_and_query("安装后异响哪个批次退单多？")
    direct_rag = router.route_and_query("三包法对空调有什么规定？", {"category": "空调"})

    assert graph_result.query_type == "graph"
    assert graph_result.confidence == 0.95
    assert fallback.query_type == "graph_then_rag"
    assert batch.query_type == "graph"
    assert "B20240501 退单2件" in batch.answer
    assert direct_rag.query_type == "rag"
    assert "[知识来源1] 三包规定 第十一条" in format_graph_context(direct_rag)
    assert len(rag.calls) == 2


def test_knowledge_hook_injects_graph_answer_before_rag() -> None:
    """输入：带格力空调压缩机实体的知识类提问、会话管理器和 MySQL 图谱 Engine。

    输出：无；断言 Hook 注入精确图谱答案，且不调用 RAG。
    功能：验证 M5 的回答前入口已实际接入图谱优先、RAG 降级的共享查询路由。
    """

    manager = Mock()
    manager.load_context.return_value = SimpleNamespace(
        slots=SimpleNamespace(filters={"brand": "格力", "category": "空调"})
    )
    rag = _Rag()
    hooks = runpy.run_path(str(KNOWLEDGE_HOOKS))["KnowledgeHooks"](
        manager, rag, _engine()
    )

    injected = hooks.pre_llm_call(
        session_id="session-1", user_message="格力空调压缩机保修多久？"
    )

    assert injected is not None
    assert "格力空调压缩机保修10年。免费更换" in injected["context"]
    assert "格力电器售后服务政策（2024版）" in injected["context"]
    assert rag.calls == []


def test_policy_ingestion_is_idempotent_and_uses_category_relationships() -> None:
    """输入：空调电机保修政策原文和固定 LLM 抽取结果。

    输出：无；断言首次写入一条叶子品类关系、重复导入不产生重复记录。
    功能：验证半自动政策入图在同一来源和部件关系上可安全重试，并把二级品类映射到 SKU 叶子品类。
    """

    engine = _engine()
    graph = AftersaleKnowledgeGraph(engine)

    assert graph.ingest_from_policy_doc("空调电机保修六年", "测试政策", _PolicyLlm()) == 1
    assert graph.ingest_from_policy_doc("空调电机保修六年", "测试政策", _PolicyLlm()) == 0
    with engine.connect() as connection:
        row: Any = connection.execute(
            sa.text(
                "SELECT warranty_months, coverage FROM t_component_warranty AS w "
                "JOIN t_warranty_source AS s ON s.source_id = w.source_id "
                "WHERE s.doc_name = '测试政策' AND w.component_name = '电机'"
            )
        ).mappings().one()
    assert dict(row) == {"warranty_months": 72, "coverage": "免费维修"}
