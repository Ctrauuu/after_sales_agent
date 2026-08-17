"""支付 MCP：根据退单查询只读退款状态。"""

from typing import Any

import sqlalchemy as sa
from fastmcp import Context, FastMCP

from mcp_suning.security.rbac import (
    authorize_mcp_request,
    build_scope_clause,
    interceptor,
)
from mcp_suning.database import engine


mcp = FastMCP("mcp-payment")


def _return_selector(
    return_id: int | None,
    order_id: int | None,
) -> tuple[str, dict[str, int]]:
    """输入：可选退单 ID ``return_id`` 和可选订单 ID ``order_id``。

    输出：以退单表别名 ``r`` 为准的参数化 SQL 条件及其参数；标识缺失、重复或无效时抛出 ``ValueError``。
    功能：兼容原有退款按退单查询和订单全链路的按订单并行调用，同时保持服务端参数化过滤。
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
    name="get_refund_status",
    description="根据退单 ID 或订单 ID 查询退款金额、方式、时间和状态。",
)
def get_refund_status(
    ctx: Context,
    return_id: int | None = None,
    order_id: int | None = None,
) -> dict[str, Any]:
    """输入：FastMCP 上下文 ``ctx``，以及二选一的正整数退单 ID 或订单 ID。

    输出：一个或多个受权退单上下文及对应退款记录，或“不存在或无权访问”的失败字典。
    功能：先验证退单行级访问权，再按指定业务标识查询支付退款记录并按粒度脱敏；按订单查询时保留全部关联退单，避免只取任意一条退款上下文。
    """

    _, safe_filters = authorize_mcp_request(ctx, "get_refund_status", {})
    selector_sql, selector_params = _return_selector(return_id, order_id)

    context_query = """
        SELECT
            r.return_id, r.order_id, r.user_id, r.sku_code,
            r.return_type, r.return_status, r.return_amount,
            ROUND(r.return_amount / 100.0, 2) AS return_amount_yuan,
            r.create_time,
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
        refund_rows = conn.execute(
            sa.text(
                """
                SELECT
                    p.refund_id, p.return_id, p.refund_amount,
                    ROUND(p.refund_amount / 100.0, 2) AS refund_amount_yuan,
                    p.refund_method, p.refund_time,
                    FROM_UNIXTIME(p.refund_time) AS refund_time_text,
                    p.refund_status
                FROM t_payment_refund AS p
                JOIN (
                    SELECT return_id, MAX(refund_id) AS latest_refund_id
                    FROM t_payment_refund
                    WHERE return_id IN :return_ids
                    GROUP BY return_id
                ) AS latest ON latest.latest_refund_id = p.refund_id
                ORDER BY p.return_id
                """
            ).bindparams(sa.bindparam("return_ids", expanding=True)),
            {"return_ids": return_ids},
        ).mappings().all()

    contexts = [dict(row) for row in context_rows]
    refunds = [dict(row) for row in refund_rows]
    refund = refunds[0] if len(refunds) == 1 else None
    result = {
        "success": True,
        "context": contexts[0],
        "contexts": contexts,
        "has_refund": bool(refunds),
        "refund": refund,
        "refunds": refunds,
        "refund_state": (
            str(refund["refund_status"])
            if refund is not None
            else "multiple_or_not_created" if refunds else "not_created"
        ),
    }
    return interceptor.mask_sensitive_data(result, safe_filters["data_scope"])


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8105, path="/mcp")
