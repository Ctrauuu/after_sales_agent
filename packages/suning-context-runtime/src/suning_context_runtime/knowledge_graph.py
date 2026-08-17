"""售后知识图谱：用 MySQL 邻接表回答可精确定位的售后事实。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

import sqlalchemy as sa


@dataclass(frozen=True)
class GraphQueryResult:
    """表示图查询或 RAG 降级后的统一回答。"""

    answer: str
    source: str
    confidence: float
    query_type: Literal["graph", "rag", "graph_then_rag"]


def _format_duration(months: int) -> str:
    """输入：保修月数 ``months``。

    输出：可直接展示的“年”或“个月”期限文本。
    功能：保留非整年保修期，避免将 18 个月等期限错误截断为 1 年。
    """

    return f"{months // 12}年" if months > 0 and months % 12 == 0 else f"{months}个月"


class AftersaleKnowledgeGraph:
    """基于 MySQL 邻接表和递归 CTE 的售后领域知识图谱。"""

    def __init__(self, engine: Any) -> None:
        """输入：可创建 SQLAlchemy 连接的 MySQL ``engine``。

        输出：绑定业务库的图谱查询对象。
        功能：复用现有售后业务表，不创建额外图库或维护第二份实体数据。
        """

        self.engine = engine

    def query_warranty(
        self, brand: str, product: str, component: str | None = None
    ) -> GraphQueryResult | None:
        """输入：品牌、品类和可选部件名称。

        输出：命中时返回含条款引用的保修结果；无匹配记录时返回 ``None``。
        功能：沿品牌、SKU、品类和保修条款关系精确定位整机或部件保修期，并兼容三级 SKU 品类对二级品类的提问。
        """

        normalized_brand, normalized_product = brand.strip(), product.strip()
        normalized_component = (component or "").strip()
        if not normalized_brand or not normalized_product:
            return None
        if normalized_component:
            statement = """
                SELECT w.warranty_months AS duration, w.coverage,
                       ws.doc_name AS source, ws.article_number AS article
                FROM t_product_brand AS b
                JOIN t_product_sku AS s ON s.brand_id = b.brand_id
                JOIN t_product_category AS c ON c.category_code = s.category_l3_code
                LEFT JOIN t_product_category AS parent ON parent.category_code = c.parent_code
                JOIN t_component_warranty AS w
                  ON w.category_code = c.category_code AND w.component_name = :component
                JOIN t_warranty_source AS ws ON ws.source_id = w.source_id
                WHERE b.brand_name = :brand
                  AND (c.category_name = :product OR parent.category_name = :product)
                ORDER BY s.sku_code, w.warranty_id
                LIMIT 1
            """
            params = {
                "brand": normalized_brand,
                "product": normalized_product,
                "component": normalized_component,
            }
            subject = normalized_component
        else:
            statement = """
                SELECT w.warranty_months AS duration, '' AS coverage,
                       ws.doc_name AS source, ws.article_number AS article
                FROM t_product_brand AS b
                JOIN t_product_sku AS s ON s.brand_id = b.brand_id
                JOIN t_product_category AS c ON c.category_code = s.category_l3_code
                LEFT JOIN t_product_category AS parent ON parent.category_code = c.parent_code
                JOIN t_product_warranty AS w ON w.sku_code = s.sku_code
                JOIN t_warranty_source AS ws ON ws.source_id = w.source_id
                WHERE b.brand_name = :brand
                  AND (c.category_name = :product OR parent.category_name = :product)
                ORDER BY s.sku_code, w.id
                LIMIT 1
            """
            params = {"brand": normalized_brand, "product": normalized_product}
            subject = "整机"
        with self.engine.connect() as connection:
            record = connection.execute(sa.text(statement), params).mappings().first()
        if record is None:
            return None
        coverage = str(record["coverage"] or "").strip()
        answer = f"{normalized_brand}{normalized_product}{subject}保修{_format_duration(int(record['duration']))}。"
        if coverage:
            answer += coverage
        article = str(record["article"] or "").strip()
        return GraphQueryResult(
            answer=answer,
            source=f"《{record['source']}》{article}",
            confidence=0.95,
            query_type="graph",
        )

    def query_fault_association(self, fault_type: str) -> list[dict[str, Any]]:
        """输入：故障类型名称或含该名称的用户问题 ``fault_type``。

        输出：按退单数量降序的品牌、品类、SKU 和批次关联记录。
        功能：通过递归品类 CTE 展开故障适用品类，再关联 SKU、生产批次和退单；从原始问题中匹配故障名，避免同一退单因图遍历重复计数。
        """

        normalized_fault = fault_type.strip()
        if not normalized_fault:
            return []
        statement = """
            WITH RECURSIVE fault_categories AS (
                SELECT f.category_code, 0 AS depth
                FROM t_fault_type AS f
                WHERE f.fault_name = :fault OR INSTR(:fault, f.fault_name) > 0

                UNION ALL

                SELECT child.category_code, fc.depth + 1
                FROM fault_categories AS fc
                JOIN t_product_category AS child ON child.parent_code = fc.category_code
                WHERE fc.depth < 3
            )
            SELECT b.brand_name AS brand,
                   COALESCE(parent.category_name, c.category_name) AS product,
                   s.sku_code AS sku, bn.batch_id AS batch,
                   COUNT(DISTINCT r.return_id) AS return_count
            FROM t_sku_fault_map AS sf
            JOIN t_fault_type AS f ON f.fault_id = sf.fault_id
            JOIN t_product_sku AS s ON s.sku_code = sf.sku_code
            JOIN t_product_category AS c ON c.category_code = s.category_l3_code
            LEFT JOIN t_product_category AS parent ON parent.category_code = c.parent_code
            JOIN t_product_brand AS b ON b.brand_id = s.brand_id
            LEFT JOIN t_batch_number AS bn ON bn.sku_code = s.sku_code
            LEFT JOIN t_aftersale_return AS r ON r.batch_id = bn.batch_id
            WHERE f.fault_name = :fault OR INSTR(:fault, f.fault_name) > 0
              AND s.category_l3_code IN (SELECT category_code FROM fault_categories)
            GROUP BY b.brand_name, parent.category_name, c.category_name, s.sku_code, bn.batch_id
            ORDER BY return_count DESC, s.sku_code, bn.batch_id
            LIMIT 10
        """
        with self.engine.connect() as connection:
            records = connection.execute(
                sa.text(statement), {"fault": normalized_fault}
            ).mappings().all()
        return [dict(record) for record in records]

    def query_product_chain(self, sku_code: str) -> dict[str, Any]:
        """输入：SKU 编码 ``sku_code``。

        输出：SKU 对应的品牌、品类路径、可保修部件和常见故障；不存在时返回空字典。
        功能：使用递归 CTE 向上追溯 SKU 品类链，并聚合其直接关联的部件保修和故障节点。
        """

        normalized_sku = sku_code.strip().upper()
        if not normalized_sku:
            return {}
        statement = """
            WITH RECURSIVE category_chain AS (
                SELECT c.category_code, c.category_name, c.parent_code, 0 AS depth
                FROM t_product_sku AS s
                JOIN t_product_category AS c ON c.category_code = s.category_l3_code
                WHERE s.sku_code = :sku

                UNION ALL

                SELECT parent.category_code, parent.category_name, parent.parent_code, cc.depth + 1
                FROM category_chain AS cc
                JOIN t_product_category AS parent ON parent.category_code = cc.parent_code
                WHERE cc.depth < 3
            )
            SELECT b.brand_name AS brand,
                   MAX(CASE WHEN cc.depth = 0 THEN cc.category_name END) AS product,
                   GROUP_CONCAT(DISTINCT cc.category_name) AS category_chain,
                   GROUP_CONCAT(DISTINCT cw.component_name) AS components,
                   GROUP_CONCAT(DISTINCT ft.fault_name) AS common_faults
            FROM t_product_sku AS s
            JOIN t_product_brand AS b ON b.brand_id = s.brand_id
            JOIN category_chain AS cc ON 1 = 1
            LEFT JOIN t_component_warranty AS cw ON cw.category_code = s.category_l3_code
            LEFT JOIN t_sku_fault_map AS sf ON sf.sku_code = s.sku_code
            LEFT JOIN t_fault_type AS ft ON ft.fault_id = sf.fault_id
            WHERE s.sku_code = :sku
            GROUP BY b.brand_name
        """
        with self.engine.connect() as connection:
            record = connection.execute(sa.text(statement), {"sku": normalized_sku}).mappings().first()
        return dict(record) if record is not None else {}

    def ingest_from_policy_doc(self, doc_text: str, doc_name: str, llm_client: Any) -> int:
        """输入：政策原文、来源文档名和提供 ``chat`` 方法的 LLM 客户端。

        输出：成功写入的部件保修关系数量；抽取格式不合法时抛出 ``ValueError``。
        功能：让 LLM 将政策原文提取为受限 JSON，再在单个事务中登记来源并写入尚不存在的品类—部件—保修关系。
        """

        normalized_doc = doc_name.strip()
        if not normalized_doc or not doc_text.strip():
            raise ValueError("政策原文和文档名称不能为空")
        prompt = (
            "从以下售后政策抽取部件保修关系，只返回 JSON："
            '{"warranties":[{"product":"品类","component":"部件",'
            '"duration_months":整数,"coverage":"保修范围","article_number":"条款号"}]}。\n'
            f"文档名称：{normalized_doc}\n政策原文：\n{doc_text}"
        )
        try:
            payload = json.loads(str(llm_client.chat(prompt)))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("政策抽取结果不是有效 JSON") from exc
        warranties = payload.get("warranties") if isinstance(payload, dict) else None
        if not isinstance(warranties, list):
            raise ValueError("政策抽取结果缺少 warranties 列表")
        count = 0
        with self.engine.begin() as connection:
            for warranty in warranties:
                if not isinstance(warranty, Mapping):
                    raise ValueError("每条保修关系必须是对象")
                try:
                    product = str(warranty["product"]).strip()
                    component = str(warranty["component"]).strip()
                    duration = int(warranty["duration_months"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("保修关系缺少有效的品类、部件或期限") from exc
                if not product or not component or duration <= 0:
                    raise ValueError("保修关系的品类、部件和期限必须有效")
                article = str(warranty.get("article_number") or "").strip()
                coverage = str(warranty.get("coverage") or "").strip()
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO t_warranty_source (doc_name, article_number)
                        SELECT :doc_name, :article
                        WHERE NOT EXISTS (
                            SELECT 1 FROM t_warranty_source
                            WHERE doc_name = :doc_name AND article_number = :article
                        )
                        """
                    ),
                    {"doc_name": normalized_doc, "article": article},
                )
                written = connection.execute(
                    sa.text(
                        """
                        INSERT INTO t_component_warranty
                            (category_code, component_name, warranty_months, coverage, source_id)
                        SELECT c.category_code, :component, :duration, :coverage, ws.source_id
                        FROM t_product_category AS c
                        LEFT JOIN t_product_category AS parent ON parent.category_code = c.parent_code
                        JOIN t_warranty_source AS ws
                          ON ws.doc_name = :doc_name AND ws.article_number = :article
                        WHERE (c.category_name = :product OR parent.category_name = :product)
                          AND EXISTS (
                              SELECT 1 FROM t_product_sku AS sku
                              WHERE sku.category_l3_code = c.category_code
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM t_component_warranty AS existing
                              WHERE existing.category_code = c.category_code
                                AND existing.component_name = :component
                                AND existing.source_id = ws.source_id
                          )
                        """
                    ),
                    {
                        "component": component,
                        "duration": duration,
                        "coverage": coverage,
                        "doc_name": normalized_doc,
                        "article": article,
                        "product": product,
                    },
                )
                count += max(0, int(written.rowcount or 0))
        return count


class QueryRouter:
    """在精确图查询与开放式 RAG 检索之间做轻量路由。"""

    GRAPH_PATTERNS = (
        (re.compile(r"保修|质保|三包.*几年|保修.*多久"), "warranty"),
        (re.compile(r"什么部件|配件|压缩机|电机|主板.*保修"), "component_warranty"),
        (re.compile(r"哪个批次|批次号|同批次"), "batch_trace"),
        (re.compile(r"什么型号.*故障|通病|常见问题"), "fault_association"),
    )

    def __init__(self, kg: AftersaleKnowledgeGraph, rag: Any) -> None:
        """输入：售后图谱 ``kg`` 和提供 ``search`` 方法的 RAG 检索器 ``rag``。

        输出：可处理用户售后提问的路由对象。
        功能：保存已存在的图和 RAG 能力，避免为规则路由新增分类模型或网络调用。
        """

        self.kg = kg
        self.rag = rag

    def _rag_result(
        self, query: str, entities: Mapping[str, str], query_type: Literal["rag", "graph_then_rag"]
    ) -> GraphQueryResult:
        """输入：原始问题、已识别实体和本次 RAG 的路由类型。

        输出：首条 RAG 片段转换后的统一结果；无命中时返回标准未命中结果。
        功能：让图查询未命中和开放问题共享已有 RAG 检索及来源引用字段。
        """

        chunks = self.rag.search(query, entities)
        if not chunks:
            return GraphQueryResult("未找到相关信息", "", 0.0, "rag")
        chunk = chunks[0].chunk
        source = " ".join(
            value for value in (chunk.source_title, chunk.article_number) if value
        )
        return GraphQueryResult(chunk.content[:300], source, 0.7, query_type)

    def route_and_query(self, query: str, entities: Mapping[str, str] | None = None) -> GraphQueryResult:
        """输入：用户问题 ``query`` 和可选的品牌、品类、部件、故障实体。

        输出：图谱精确答案、图谱失败后的 RAG 答案或直接 RAG 答案。
        功能：识别保修、批次和故障关联事实优先查图，图中无记录时自动降级到现有 RAG，解释性问题直接检索原文。
        """

        normalized_query = query.strip()
        resolved = {key: str(value).strip() for key, value in (entities or {}).items() if str(value).strip()}
        product = resolved.get("product") or resolved.get("category") or ""
        for pattern, query_type in self.GRAPH_PATTERNS:
            if not pattern.search(normalized_query):
                continue
            if query_type in {"warranty", "component_warranty"}:
                result = self.kg.query_warranty(
                    resolved.get("brand", ""), product, resolved.get("component")
                )
                if result is not None:
                    return result
                return self._rag_result(normalized_query, resolved, "graph_then_rag")
            if query_type in {"batch_trace", "fault_association"}:
                rows = self.kg.query_fault_association(
                    resolved.get("fault_type") or normalized_query
                )
                if rows:
                    answer = "\n".join(
                        f"{row['brand']} {row['product']} 批次{row.get('batch') or '未知'} 退单{row['return_count']}件"
                        for row in rows[:5]
                    )
                    return GraphQueryResult(answer, "知识图谱-退单关联分析", 0.9, "graph")
                return self._rag_result(normalized_query, resolved, "graph_then_rag")
        return self._rag_result(normalized_query, resolved, "rag")


def format_graph_context(result: GraphQueryResult) -> str:
    """输入：图谱或路由器返回的统一查询结果 ``result``。

    输出：可注入 LLM 的、带来源的售后事实文本；无答案时返回空字符串。
    功能：要求模型优先使用精确图关系回答，并在图谱降级到 RAG 时仍保留可追溯来源。
    """

    if not result.answer or result.answer == "未找到相关信息":
        return ""
    if result.query_type != "graph":
        source = result.source or "未命名来源"
        return (
            "以下是售后知识检索片段，只能依据它回答政策、保修或维修问题。"
            "结论后必须标注 [知识来源1]。\n\n"
            f"[知识来源1] {source}\n{result.answer}"
        )
    source = f"\n来源：{result.source}" if result.source else ""
    return f"以下是知识图谱精确查询结果，请依据它回答，不要编造未提供的期限或条款。\n{result.answer}{source}"


__all__ = [
    "AftersaleKnowledgeGraph",
    "GraphQueryResult",
    "QueryRouter",
    "format_graph_context",
]
