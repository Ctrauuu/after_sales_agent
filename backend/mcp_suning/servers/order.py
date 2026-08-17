"""订单 MCP：提供文档登记的订单搜索和订单详情两个只读工具。"""

from typing import Any

import sqlalchemy as sa
from fastmcp import Context, FastMCP

from mcp_suning.security.rbac import (
    CATEGORY_ALIASES,
    REGION_ALIASES,
    authorize_mcp_request,
    build_scope_clause,
    interceptor,
)
from mcp_suning.database import engine


mcp = FastMCP("mcp-order")

RETURN_STATUS_LABELS = {
    "0": "待审核",
    "1": "已通过",
    "2": "已驳回",
    "3": "已完成",
}


def _return_status_label(value: Any) -> str:
    """输入：退单表中的原始状态码或状态文本 ``value``。

    输出：已登记状态码对应的中文标签；未知值返回带原值的“未知状态”标签。
    功能：把退单状态码的已知业务字典显式返回给调用方，避免模型把数字状态码自行推断成审核结论。
    """

    normalized = str(value or "").strip()
    return RETURN_STATUS_LABELS.get(normalized, f"未知状态({normalized or '空'})")


@mcp.tool(
    name="search_orders",
    description="根据状态、品类、日期范围和区域搜索订单列表。",
)
def search_orders(
    ctx: Context,
    status: str = "",
    category: str = "",
    date_range_days: int = 7,
    region: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """输入：FastMCP ``ctx`` 及状态、品类、天数、区域、数量等业务筛选。

    输出：按下单时间倒序排列且按权限脱敏的订单字典列表。
    功能：先完成统一授权，再执行带时间、业务筛选和 RBAC 条件的只读订单查询。
    """

    if not 1 <= limit <= 100:
        raise ValueError("limit 必须在 1 到 100 之间")

    _, safe_filters = authorize_mcp_request(
        ctx,
        "search_orders",
        {
            "status": status,
            "category": category,
            "date_range_days": date_range_days,
            "region": region,
        },
    )
    query = """
        SELECT DISTINCT
            o.order_id, o.user_id, o.channel, o.region_code, o.city_code,
            o.order_amount,
            ROUND(o.order_amount / 100.0, 2) AS order_amount_yuan,
            o.pay_method, o.create_time,
            FROM_UNIXTIME(o.create_time) AS create_time_text,
            o.order_status, r.return_id, r.return_reason_desc, r.return_status
        FROM t_order_main AS o
        LEFT JOIN t_aftersale_return AS r ON r.order_id = o.order_id
        LEFT JOIN t_product_sku AS s ON s.sku_code = r.sku_code
        WHERE o.create_time >= (
            SELECT MAX(create_time) - (:days * 86400) FROM t_order_main
        )
    """
    params: dict[str, Any] = {
        "days": safe_filters["date_range_days"],
        "limit": limit,
    }

    normalized_status = status.strip().lower()
    if normalized_status in {"aftersale", "return", "returned"}:
        query += " AND r.return_id IS NOT NULL"
    elif normalized_status:
        query += " AND (o.order_status = :status OR r.return_status = :status)"
        params["status"] = normalized_status

    if category.strip():
        category_code = CATEGORY_ALIASES.get(
            category.strip(),
            category.strip().upper(),
        )
        query += " AND s.category_l3_code LIKE :category"
        params["category"] = f"{category_code}%"
    if region.strip():
        region_code = REGION_ALIASES.get(region.strip(), region.strip().upper())
        query += " AND o.region_code = :region"
        params["region"] = region_code

    scope_sql, scope_params = build_scope_clause(
        safe_filters,
        region_column="o.region_code",
        city_column="o.city_code",
        category_column="s.category_l3_code",
    )
    query += scope_sql
    query += " ORDER BY o.create_time DESC, o.order_id DESC LIMIT :limit"
    params.update(scope_params)

    with engine.connect() as conn:
        rows = conn.execute(sa.text(query), params).mappings().all()
    result = [dict(row) for row in rows]
    return interceptor.mask_sensitive_data(result, safe_filters["data_scope"])


@mcp.tool(
    name="get_order_detail",
    description="根据订单 ID 获取单个订单详情。",
)
def get_order_detail(order_id: int, ctx: Context) -> dict[str, Any]:
    """输入：正整数订单 ID ``order_id`` 和 FastMCP 上下文 ``ctx``。

    输出：包含订单、全部授权关联退单及状态标签的成功字典，或“不存在或无权访问”的失败字典。
    功能：在时间、区域、城市和品类权限内查询单笔订单及其所有退单，为订单详情和全链路按退单聚合提供授权后的基础数据。
    """

    if order_id < 1:
        raise ValueError("order_id 必须大于或等于 1")
    _, safe_filters = authorize_mcp_request(ctx, "get_order_detail", {})

    query = """
        SELECT DISTINCT
            o.order_id, o.user_id, o.channel, o.region_code, o.city_code,
            o.order_amount,
            ROUND(o.order_amount / 100.0, 2) AS order_amount_yuan,
            o.pay_method, o.create_time,
            FROM_UNIXTIME(o.create_time) AS create_time_text,
            o.order_status,
            r.return_id, r.sku_code, r.return_type, r.return_status,
            r.return_reason_desc, r.create_time AS return_create_time,
            FROM_UNIXTIME(r.create_time) AS return_create_time_text,
            r.update_time AS return_update_time,
            FROM_UNIXTIME(r.update_time) AS return_update_time_text,
            s.category_l3_code AS category_code,
            c.category_name
        FROM t_order_main AS o
        LEFT JOIN t_aftersale_return AS r ON r.order_id = o.order_id
        LEFT JOIN t_product_sku AS s ON s.sku_code = r.sku_code
        LEFT JOIN t_product_category AS c ON c.category_code = s.category_l3_code
        WHERE o.order_id = :order_id
          AND o.create_time >= (
              SELECT MAX(create_time) - (:days * 86400) FROM t_order_main
          )
    """
    params: dict[str, Any] = {
        "order_id": order_id,
        "days": safe_filters["date_range_days"],
    }
    scope_sql, scope_params = build_scope_clause(
        safe_filters,
        region_column="o.region_code",
        city_column="o.city_code",
        category_column="s.category_l3_code",
    )
    query += scope_sql + " ORDER BY r.create_time, r.return_id"
    params.update(scope_params)

    with engine.connect() as conn:
        rows = conn.execute(sa.text(query), params).mappings().all()
    if not rows:
        result: dict[str, Any] = {
            "success": False,
            "error": "订单不存在或无权访问",
            "order_id": order_id,
        }
    else:
        order = dict(rows[0])
        returns: list[dict[str, Any]] = []
        for row in rows:
            row_dict = dict(row)
            return_id = row_dict.get("return_id")
            if return_id is None:
                continue
            return_context = {
                key: row_dict.get(key)
                for key in (
                    "return_id",
                    "sku_code",
                    "return_type",
                    "return_status",
                    "return_reason_desc",
                    "return_create_time",
                    "return_create_time_text",
                    "return_update_time",
                    "return_update_time_text",
                    "category_code",
                    "category_name",
                )
            }
            return_context["return_status_label"] = _return_status_label(
                return_context["return_status"]
            )
            returns.append(return_context)
        order["return_status_label"] = _return_status_label(order.get("return_status"))
        result = {"success": True, "order": order, "returns": returns}
    return interceptor.mask_sensitive_data(result, safe_filters["data_scope"])


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8101, path="/mcp")
