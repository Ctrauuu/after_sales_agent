"""物流 MCP：根据退单查询逆向物流轨迹。"""

from typing import Any

import sqlalchemy as sa
from fastmcp import Context, FastMCP

from mcp_suning.auth_middleware import (
    authorize_mcp_request,
    build_scope_clause,
    interceptor,
)
from mcp_suning.database import engine


mcp = FastMCP("mcp-logistics")


def _return_selector(
    return_id: int | None,
    order_id: int | None,
) -> tuple[str, dict[str, int]]:
    """输入：可选退单 ID ``return_id`` 和可选订单 ID ``order_id``。

    输出：以退单表别名 ``r`` 为准的参数化 SQL 条件及其参数；标识缺失、重复或无效时抛出 ``ValueError``。
    功能：支持原有按退单查轨迹和订单全链路按订单并发查轨迹两种安全入口，避免聚合器先串行解析退单 ID。
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
    name="query_logistics",
    description="根据退单 ID 或订单 ID 查询按时间排列的完整逆向物流轨迹。",
)
def query_logistics(
    ctx: Context,
    return_id: int | None = None,
    order_id: int | None = None,
) -> dict[str, Any]:
    """输入：FastMCP 上下文 ``ctx``，以及二选一的正整数退单 ID 或订单 ID。

    输出：一个或多个受权退单上下文、按退单关联的完整轨迹，或统一失败字典。
    功能：先通过业务关联表验证行级权限，再按指定业务标识读取并脱敏逆向物流节点；按订单查询时保留全部关联退单，避免跨退单混淆。
    """

    _, safe_filters = authorize_mcp_request(ctx, "query_logistics", {})
    selector_sql, selector_params = _return_selector(return_id, order_id)

    context_query = """
        SELECT
            r.return_id, r.order_id, r.user_id, r.sku_code,
            r.return_type, r.return_status, r.create_time,
            FROM_UNIXTIME(r.create_time) AS create_time_text
        FROM t_aftersale_return AS r
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
    context_query += scope_sql + " ORDER BY r.create_time, r.return_id"
    params.update(scope_params)

    with engine.connect() as conn:
        context_rows = conn.execute(
            sa.text(context_query),
            params,
        ).mappings().all()
        if not context_rows:
            result: dict[str, Any] = {
                "success": False,
                "error": "退单不存在或无权访问",
                "return_id": return_id,
                "order_id": order_id,
            }
            return interceptor.mask_sensitive_data(
                result,
                safe_filters["data_scope"],
            )

        return_ids = [int(row["return_id"]) for row in context_rows]
        trace_rows = conn.execute(
            sa.text(
                """
                SELECT trace_id, return_id, node_type, node_desc,
                       operator_name, node_time,
                       FROM_UNIXTIME(node_time) AS node_time_text
                FROM t_logistics_trace
                WHERE return_id IN :return_ids
                ORDER BY node_time, trace_id
                """
            ).bindparams(sa.bindparam("return_ids", expanding=True)),
            {"return_ids": return_ids},
        ).mappings().all()

    contexts = [dict(row) for row in context_rows]
    traces = [dict(row) for row in trace_rows]
    latest_trace = traces[-1] if traces else None
    result = {
        "success": True,
        "context": contexts[0],
        "contexts": contexts,
        "has_logistics": bool(traces),
        "current_logistics_status": (
            latest_trace["node_type"] if latest_trace else "暂无物流轨迹"
        ),
        "latest_trace": latest_trace,
        "trace_count": len(traces),
        "traces": traces,
    }
    return interceptor.mask_sensitive_data(result, safe_filters["data_scope"])


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8104, path="/mcp")
