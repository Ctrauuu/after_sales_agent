"""商品 MCP：根据 SKU 查询商品、保修、故障和批次资料。"""

from typing import Any

import sqlalchemy as sa
from fastmcp import Context, FastMCP

from mcp_suning.auth_middleware import (
    authorize_mcp_request,
    build_scope_clause,
    interceptor,
)
from mcp_suning.database import engine


mcp = FastMCP("mcp-product")


@mcp.tool(
    name="get_product_info",
    description=(
        "根据 SKU 编码获取商品详情，包括品牌、品类、价格、保修、"
        "常见故障和生产批次。"
    ),
)
def get_product_info(sku_code: str, ctx: Context) -> dict[str, Any]:
    """输入：非空 SKU 编码 ``sku_code`` 和 FastMCP 上下文 ``ctx``。

    输出：商品、保修、故障和批次资料，或“不存在或无权访问”的失败字典。
    功能：先应用品类权限验证商品，再读取其关联售后资料并统一脱敏。
    """

    normalized_sku = sku_code.strip().upper()
    if not normalized_sku:
        raise ValueError("sku_code 不能为空")
    _, safe_filters = authorize_mcp_request(ctx, "get_product_info", {})

    product_query = """
        SELECT
            s.sku_code, s.product_name, s.brand_id, b.brand_name,
            b.service_policy, s.category_l3_code AS category_code,
            c.category_name, s.price,
            ROUND(s.price / 100.0, 2) AS price_yuan,
            s.warranty_months
        FROM t_product_sku AS s
        LEFT JOIN t_product_brand AS b ON b.brand_id = s.brand_id
        LEFT JOIN t_product_category AS c
            ON c.category_code = s.category_l3_code
        WHERE UPPER(s.sku_code) = :sku_code
    """
    product_params: dict[str, Any] = {"sku_code": normalized_sku}
    scope_sql, scope_params = build_scope_clause(
        safe_filters,
        category_column="s.category_l3_code",
    )
    product_query += scope_sql + " LIMIT 1"
    product_params.update(scope_params)

    with engine.connect() as conn:
        product_row = conn.execute(
            sa.text(product_query),
            product_params,
        ).mappings().first()
        if product_row is None:
            result: dict[str, Any] = {
                "success": False,
                "error": "商品 SKU 不存在或无权访问",
                "sku_code": normalized_sku,
            }
            return interceptor.mask_sensitive_data(
                result,
                safe_filters["data_scope"],
            )

        product = dict(product_row)
        warranty = conn.execute(
            sa.text(
                """
                SELECT pw.warranty_months, ws.doc_name, ws.article_number
                FROM t_product_warranty AS pw
                LEFT JOIN t_warranty_source AS ws
                    ON ws.source_id = pw.source_id
                WHERE pw.sku_code = :sku_code
                ORDER BY pw.id
                LIMIT 1
                """
            ),
            {"sku_code": normalized_sku},
        ).mappings().first()
        components = conn.execute(
            sa.text(
                """
                SELECT cw.component_name, cw.warranty_months, cw.coverage,
                       ws.doc_name, ws.article_number
                FROM t_component_warranty AS cw
                LEFT JOIN t_warranty_source AS ws
                    ON ws.source_id = cw.source_id
                WHERE cw.category_code = :category_code
                ORDER BY cw.warranty_id
                """
            ),
            {"category_code": product["category_code"]},
        ).mappings().all()
        faults = conn.execute(
            sa.text(
                """
                SELECT f.fault_id, f.fault_name, f.severity, m.occur_count
                FROM t_sku_fault_map AS m
                JOIN t_fault_type AS f ON f.fault_id = m.fault_id
                WHERE m.sku_code = :sku_code
                ORDER BY m.occur_count DESC, f.fault_id
                """
            ),
            {"sku_code": normalized_sku},
        ).mappings().all()
        batches = conn.execute(
            sa.text(
                """
                SELECT batch_id, produce_date, total_quantity
                FROM t_batch_number
                WHERE sku_code = :sku_code
                ORDER BY produce_date DESC, batch_id
                """
            ),
            {"sku_code": normalized_sku},
        ).mappings().all()

    result = {
        "success": True,
        "product": product,
        "whole_machine_warranty": dict(warranty) if warranty else None,
        "component_warranties": [dict(row) for row in components],
        "common_faults": [dict(row) for row in faults],
        "batches": [dict(row) for row in batches],
    }
    return interceptor.mask_sensitive_data(result, safe_filters["data_scope"])


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8103, path="/mcp")
