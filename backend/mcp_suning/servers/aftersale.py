"""售后 MCP：提供退单聚合统计和售后流程明细两个只读工具。"""

from typing import Any

import sqlalchemy as sa
from fastmcp import Context, FastMCP

# 两个动态分析 Tool 共用 runtime 中的 Pipeline；修改 nl2sql 后需重载本服务。
from nl2sql.runtime import nl2sql_lite_pipeline, nl2sql_pipeline
from mcp_suning.security.rbac import (
    authorize_mcp_request,
    build_scope_clause,
    interceptor,
)
from mcp_suning.database import engine


mcp = FastMCP("mcp-aftersale")


def _return_selector(
    return_id: int | None,
    order_id: int | None,
) -> tuple[str, dict[str, int]]:
    """输入：可选退单 ID ``return_id`` 和可选订单 ID ``order_id``。

    输出：以退单表别名 ``r`` 为准的参数化 SQL 条件及其参数；标识缺失、重复或无效时抛出 ``ValueError``。
    功能：兼容原有退单查询与订单全链路并行调用，要求调用方明确且只提供一种业务标识。
    """

    if (return_id is None) == (order_id is None):
        raise ValueError("return_id 和 order_id 必须且只能提供一个")
    if return_id is not None:
        if return_id < 1:
            raise ValueError("return_id 必须大于或等于 1")
        return "r.return_id = :return_id", {"return_id": return_id}
    if order_id is None or order_id < 1:
        raise ValueError("order_id 必须大于或等于 1")
    return "r.order_id = :order_id", {"order_id": order_id}


@mcp.tool(
    name="query_return_stats_nl2sql",
    description="按日期、品类、退单原因、区域或品牌动态聚合最近若干天的退单。",
)
def query_return_stats_nl2sql(
    ctx: Context,
    group_by: str = "category",
    date_range_days: int = 7,
    category: str = "",
) -> list[dict[str, Any]]:
    """输入：FastMCP ``ctx``、聚合维度、查询天数和可选品类。

    输出：按退单数量排序且按权限处理的动态聚合统计列表。
    功能：把结构化统计参数转换为受控问题，并交给共用 NL2SQL Pipeline 查询。
    """

    _, safe_filters = authorize_mcp_request(
        ctx,
        "query_return_stats_nl2sql",
        {"date_range_days": date_range_days, "category": category},
    )
    dimensions = {
        "day": (
            "按退单申请日期分组；dimension_code 和 dimension_name 都返回 "
            "DATE(FROM_UNIXTIME(r.create_time))"
        ),
        "category": (
            "按品类分组；返回 c.category_code AS dimension_code 和 "
            "c.category_name AS dimension_name"
        ),
        "reason": (
            "按退单原因分组；返回 r.return_reason_code AS dimension_code 和 "
            "MIN(r.return_reason_desc) AS dimension_name"
        ),
        "region": (
            "按区域分组；dimension_code 和 dimension_name 都返回 o.region_code"
        ),
        "brand": (
            "按品牌分组；返回 b.brand_id AS dimension_code 和 "
            "b.brand_name AS dimension_name"
        ),
    }
    group_key = group_by.strip().lower()
    if group_key not in dimensions:
        raise ValueError("group_by 必须是 day、category、reason、region 或 brand")

    question = (
        f"统计最近 {safe_filters['date_range_days']} 天的退单数据，"
        f"{dimensions[group_key]}。"
        "返回 dimension_code、dimension_name、"
        "COUNT(DISTINCT r.return_id) AS return_count、"
        "SUM(r.return_amount) AS total_amount、"
        "SUM(r.return_amount) / 100.0 AS total_amount_yuan，"
        "按 return_count 降序。"
    )
    requested_category = str(safe_filters.get("category") or "").strip()
    if requested_category:
        question += f"只统计品类 {requested_category} 及其子品类。"

    result = nl2sql_lite_pipeline.run(question, safe_filters)
    return interceptor.mask_sensitive_data(
        result["rows"],
        safe_filters["data_scope"],
    )


@mcp.tool(
    name="query_aftersale_nl2sql",
    description=(
        "把自然语言售后分析问题转换为安全的聚合SQL并返回查询结果；"
        "适合固定统计工具无法表达的组合维度和指标。"
    ),
)
def query_aftersale_nl2sql(
    question: str,
    ctx: Context,
) -> dict[str, Any]:
    """输入：自然语言统计问题 ``question`` 和 FastMCP 上下文 ``ctx``。

    输出：包含问题、最终 SQL、行数和聚合结果的脱敏响应字典。
    功能：授权后执行 Schema-first NL2SQL、SQL 沙箱校验、RBAC 注入和只读查询。
    """

    normalized_question = question.strip()
    if not normalized_question:
        raise ValueError("question 不能为空")

    _, safe_filters = authorize_mcp_request(
        ctx,
        "query_aftersale_nl2sql",
        {},
    )
    result = nl2sql_pipeline.run(
        normalized_question,
        safe_filters,
    )
    response = {
        "success": True,
        "question": normalized_question,
        "sql": result["sql"],
        "row_count": len(result["rows"]),
        "rows": result["rows"],
    }
    return interceptor.mask_sensitive_data(
        response,
        safe_filters["data_scope"],
    )


@mcp.tool(
    name="get_aftersale_workflow",
    description="根据退单 ID 获取售后工单全链路流转记录。",
)
def get_aftersale_workflow(
    ctx: Context,
    return_id: int | None = None,
    order_id: int | None = None,
) -> list[dict[str, Any]]:
    """输入：FastMCP 上下文 ``ctx``，以及二选一的正整数退单 ID 或订单 ID。

    输出：按发生时间排列并按权限脱敏的售后流程节点列表。
    功能：在时间和行级权限内关联退单、订单、SKU，按指定业务标识查询完整工单流转记录。
    """

    _, safe_filters = authorize_mcp_request(
        ctx,
        "get_aftersale_workflow",
        {},
    )
    selector_sql, selector_params = _return_selector(return_id, order_id)
    query = """
        SELECT
            w.workflow_id, w.return_id, w.step_name, w.step_status,
            w.engineer_id, w.step_time,
            FROM_UNIXTIME(w.step_time) AS step_time_text, w.remark
        FROM t_aftersale_workflow AS w
        JOIN t_aftersale_return AS r ON r.return_id = w.return_id
        JOIN t_order_main AS o ON o.order_id = r.order_id
        JOIN t_product_sku AS s ON s.sku_code = r.sku_code
        WHERE """ + selector_sql + """
          AND r.create_time >= (
              SELECT MAX(create_time) - (:days * 86400)
              FROM t_aftersale_return
          )
    """
    params: dict[str, Any] = {
        "days": safe_filters["date_range_days"],
    }
    params.update(selector_params)
    scope_sql, scope_params = build_scope_clause(
        safe_filters,
        region_column="o.region_code",
        city_column="o.city_code",
        category_column="s.category_l3_code",
    )
    query += scope_sql
    query += " ORDER BY w.step_time IS NULL, w.step_time, w.workflow_id"
    params.update(scope_params)

    with engine.connect() as conn:
        rows = conn.execute(sa.text(query), params).mappings().all()
    result = [dict(row) for row in rows]
    return interceptor.mask_sensitive_data(result, safe_filters["data_scope"])


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8102, path="/mcp")
