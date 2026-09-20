"""售后 MCP：提供退单聚合、SKU 退单率和售后流程四类只读工具。"""

import hashlib
import json

from typing import Any

import sqlalchemy as sa
from fastmcp import Context, FastMCP
from redis import Redis
from redis.exceptions import RedisError

# 两个动态分析 Tool 共用 runtime 中的 Pipeline；修改 nl2sql 后需重载本服务。
from nl2sql.runtime import nl2sql_lite_pipeline, nl2sql_pipeline
from mcp_suning.security.rbac import (
    authorize_mcp_request,
    build_scope_clause,
    interceptor,
)
from mcp_suning.config import settings
from mcp_suning.database import engine


mcp = FastMCP("mcp-aftersale")
CRON_SERVICE_USER_ID = "U-SVC-CRON"
PRECOMPUTED_RETURN_STATS_TTL_SECONDS = 60 * 60
HOT_RETURN_STATS = frozenset({("category", 1, ""), ("day", 7, "")})


def _is_hot_return_stats(
    group_by: str,
    safe_filters: dict[str, Any],
) -> bool:
    """输入：规范化聚合维度 ``group_by`` 与经过 RBAC 收窄的 ``safe_filters``。

    输出：参数属于凌晨预热的固定高频退单统计时返回 ``True``。
    功能：限制 Cron 只写入已定义的热点查询，避免任意内部查询占用预计算缓存。
    """

    return (
        group_by,
        int(safe_filters["date_range_days"]),
        str(safe_filters.get("category") or "").strip(),
    ) in HOT_RETURN_STATS


def _precomputed_return_stats_key(
    group_by: str,
    safe_filters: dict[str, Any],
) -> str:
    """输入：规范化聚合维度 ``group_by`` 与完整 ``safe_filters``。

    输出：稳定、不可反解业务条件的 Redis 缓存键。
    功能：把请求参数和最终行级权限范围共同纳入键，杜绝跨数据范围复用预计算结果。
    """

    payload = json.dumps(
        {"group_by": group_by, "safe_filters": safe_filters},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"suning:precomputed:return-stats:{digest}"


def _load_precomputed_return_stats(
    group_by: str,
    safe_filters: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """输入：规范化聚合维度 ``group_by`` 与经过授权的 ``safe_filters``。

    输出：有效预计算统计列表；不存在、损坏或缓存不可用时返回 ``None``。
    功能：在不影响在线查询可用性的前提下读取仅对同一权限范围可见的热点结果。
    """

    if not settings.redis_url or not _is_hot_return_stats(group_by, safe_filters):
        return None
    try:
        cached = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        ).get(_precomputed_return_stats_key(group_by, safe_filters))
        result = json.loads(cached) if cached else None
    except (RedisError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return result if isinstance(result, list) else None


def _store_precomputed_return_stats(
    group_by: str,
    safe_filters: dict[str, Any],
    result: list[dict[str, Any]],
) -> None:
    """输入：规范化聚合维度、授权范围和已脱敏的统计 ``result``。

    输出：无；Redis 不可用或写入失败时静默保留在线查询结果。
    功能：仅由已授权 Cron 主体写入一小时热点缓存，不让缓存故障阻断凌晨预热。
    """

    if not settings.redis_url or not _is_hot_return_stats(group_by, safe_filters):
        return
    try:
        Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        ).setex(
            _precomputed_return_stats_key(group_by, safe_filters),
            PRECOMPUTED_RETURN_STATS_TTL_SECONDS,
            json.dumps(result, ensure_ascii=False, default=str),
        )
    except RedisError:
        return


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
) -> dict[str, Any]:
    """输入：FastMCP ``ctx``、聚合维度、查询天数和可选品类。

    输出：包含 RBAC 行级范围、权限说明和按退单数量排序的动态聚合结果。
    功能：把结构化统计参数交给共用 NL2SQL Pipeline，并显式告知调用方结果是否已被区域、城市或品类权限收窄。
    """

    user, safe_filters = authorize_mcp_request(
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

    cached = _load_precomputed_return_stats(group_key, safe_filters)
    if cached is None:
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
        rows = interceptor.mask_sensitive_data(
            result["rows"],
            safe_filters["data_scope"],
        )
        if getattr(user, "user_id", "") == CRON_SERVICE_USER_ID:
            _store_precomputed_return_stats(group_key, safe_filters, rows)
    else:
        rows = cached

    rbac_scope = {
        "regions": list(safe_filters.get("allowed_regions") or []),
        "cities": list(safe_filters.get("allowed_cities") or []),
        "categories": list(safe_filters.get("allowed_categories") or []),
        "data_scope": safe_filters["data_scope"],
    }
    scope_restricted = any(
        rbac_scope[key] for key in ("regions", "cities", "categories")
    )
    return {
        "success": True,
        "scope_restricted": scope_restricted,
        "rbac_scope": rbac_scope,
        "scope_notice": (
            "RBAC 已按当前账号权限收窄统计范围；结果不是全国数据，"
            "全国口径需要更高的行级数据权限。"
            if scope_restricted
            else "未配置区域、城市或品类行级限制；结果为当前账号授权范围。"
        ),
        "row_count": len(rows),
        "rows": rows,
    }


@mcp.tool(
    name="query_sku_return_rate",
    description=(
        "按订单创建时间统计指定范围内各 SKU 的退单订单率，"
        "返回退单订单数、订单数和退单率最高的前若干 SKU。"
    ),
)
def query_sku_return_rate(
    ctx: Context,
    date_range_days: int = 30,
    limit: int = 5,
    category: str = "",
) -> dict[str, Any]:
    """输入：FastMCP ``ctx``、订单时间范围、Top N 数量和可选品类 ``category``。

    输出：包含实际查询天数、行数和按 SKU 聚合的退单订单率；参数无效时抛出 ``ValueError``。
    功能：在服务端鉴权范围内用固定参数化 SQL 计算退单订单数与订单数之比，不调用 NL2SQL。
    """

    _, safe_filters = authorize_mcp_request(
        ctx,
        "query_sku_return_rate",
        {"date_range_days": date_range_days, "category": category},
    )
    if not 1 <= limit <= 100:
        raise ValueError("limit 必须在 1 到 100 之间")

    scope_sql, params = build_scope_clause(
        safe_filters,
        region_column="o.region_code",
        city_column="o.city_code",
        category_column="s.category_l3_code",
    )
    params.update(
        date_range_days=safe_filters["date_range_days"],
        limit=limit,
    )
    sql = sa.text(
        """
        SELECT
            i.sku_code,
            MAX(s.product_name) AS product_name,
            COUNT(DISTINCT r.order_id) AS returned_order_count,
            COUNT(DISTINCT i.order_id) AS order_count,
            CAST(
                ROUND(
                    100.0 * COUNT(DISTINCT r.order_id)
                    / NULLIF(COUNT(DISTINCT i.order_id), 0),
                    2
                ) AS DOUBLE
            ) AS return_rate_pct
        FROM t_order_item AS i
        JOIN t_order_main AS o ON o.order_id = i.order_id
        JOIN t_product_sku AS s ON s.sku_code = i.sku_code
        LEFT JOIN t_aftersale_return AS r
            ON r.order_id = i.order_id
           AND r.sku_code = i.sku_code
        WHERE o.create_time >= (
            SELECT MAX(create_time) - (:date_range_days * 86400)
            FROM t_order_main
        )
        """
        + scope_sql
        + """
        GROUP BY i.sku_code
        ORDER BY return_rate_pct DESC, order_count DESC, i.sku_code ASC
        LIMIT :limit
        """
    )
    with engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(sql, params).mappings().all()]
    return interceptor.mask_sensitive_data(
        {
            "success": True,
            "date_range_days": safe_filters["date_range_days"],
            "row_count": len(rows),
            "rows": rows,
        },
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
