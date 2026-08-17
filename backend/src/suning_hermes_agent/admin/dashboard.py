"""管理看板的 Trace、MCP 调用分布和动态告警聚合路由。"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import runtime
from .common import _query_date, _query_int
from .conversations import _open_state_database, _state_database, _trace_events
from .runtime import LOCAL_TIMEZONE


router = APIRouter(prefix="/api/admin/dashboard")
SERVER_NAMES = {
    "mcp-order": "订单服务",
    "mcp-aftersale": "售后服务",
    "mcp-product": "商品服务",
    "mcp-logistics": "物流服务",
    "mcp-payment": "支付服务",
    "mcp-order-timeline": "全链路服务",
}


def _today() -> datetime:
    """输入：无。

    输出：Asia/Shanghai 当前带时区时间。
    功能：统一仪表盘按自然日统计的时区边界。
    """

    return datetime.now(LOCAL_TIMEZONE)


def _events_on(events: list[dict[str, Any]], date_text: str) -> list[dict[str, Any]]:
    """输入：Trace 事件数组和 ``YYYY-MM-DD`` 日期 ``date_text``。

    输出：发生在该日期的事件数组。
    功能：为概览环比和小时趋势复用日期过滤。
    """

    return [event for event in events if str(event.get("timestamp") or "").startswith(date_text)]


def _change(today_value: float, yesterday_value: float) -> float:
    """输入：今日值 ``today_value`` 与昨日值 ``yesterday_value``。

    输出：保留一位小数的环比百分比；昨日为零时返回零。
    功能：为四张指标卡生成稳定环比。
    """

    if not yesterday_value:
        return 0.0
    return round((today_value - yesterday_value) / yesterday_value * 100, 1)


@router.get("/overview")
def dashboard_overview(request: Request) -> JSONResponse:
    """输入：仪表盘概览请求 ``request``。

    输出：今日对话、活跃用户、MCP 调用和平均响应时间及环比。
    功能：从真实 Trace 日志聚合四项核心指标。
    """

    del request
    now = _today()
    events = _trace_events()
    today_events = _events_on(events, now.strftime("%Y-%m-%d"))
    yesterday_events = _events_on(events, (now - timedelta(days=1)).strftime("%Y-%m-%d"))
    today_users = {str(event.get("user_id")) for event in today_events if event.get("user_id")}
    yesterday_users = {str(event.get("user_id")) for event in yesterday_events if event.get("user_id")}
    today_mcp = sum(int(event.get("total_mcp_calls") or 0) for event in today_events)
    yesterday_mcp = sum(int(event.get("total_mcp_calls") or 0) for event in yesterday_events)
    today_average = sum(float(event.get("duration_ms") or 0) for event in today_events) / len(today_events) / 1000 if today_events else 0
    yesterday_average = sum(float(event.get("duration_ms") or 0) for event in yesterday_events) / len(yesterday_events) / 1000 if yesterday_events else 0
    return JSONResponse(
        {
            "conversations": {"value": len(today_events), "change": _change(len(today_events), len(yesterday_events))},
            "active_users": {"value": len(today_users), "change": _change(len(today_users), len(yesterday_users))},
            "mcp_calls": {"value": today_mcp, "change": _change(today_mcp, yesterday_mcp)},
            "avg_response_time": {"value": round(today_average, 1), "change": _change(today_average, yesterday_average)},
        }
    )


@router.get("/trend")
def dashboard_trend(request: Request) -> JSONResponse:
    """输入：可选 ``date`` 查询参数。

    输出：0 至 23 点的真实会话数量数组。
    功能：按 Trace 完成时间聚合当日 Agent 对话趋势。
    """

    date_text = _query_date(request, "date", _today().strftime("%Y-%m-%d"))
    counts = [0] * 24
    for event in _events_on(_trace_events(), date_text):
        try:
            hour = int(str(event["timestamp"])[11:13])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= hour < 24:
            counts[hour] += 1
    return JSONResponse({"items": [{"hour": f"{hour:02d}", "count": count} for hour, count in enumerate(counts)]})


@router.get("/mcp-distribution")
def dashboard_mcp_distribution(request: Request) -> JSONResponse:
    """输入：可选 ``date`` 查询参数。

    输出：按 MCP 服务聚合的真实工具调用量数组。
    功能：读取 Hermes 工具消息并用现有 ``TOOL_SPECS`` 归属业务服务。
    """

    date_text = _query_date(request, "date", _today().strftime("%Y-%m-%d"))
    start = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=LOCAL_TIMEZONE).timestamp()
    end = start + 86400
    counts: dict[str, int] = {}
    tool_servers: dict[str, str] = {}
    with runtime.engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT tool_name, server_id FROM mcp_tool_registry")
        ).mappings()
        for row in rows:
            tool_servers[str(row["tool_name"])] = str(row["server_id"])
    if _state_database().is_file():
        with closing(_open_state_database()) as state:
            rows = state.execute(
                "SELECT tool_name, COUNT(*) AS count FROM messages WHERE role = 'tool' AND active = 1 AND timestamp >= ? AND timestamp < ? GROUP BY tool_name",
                (start, end),
            ).fetchall()
            for row in rows:
                tool_name = str(row["tool_name"] or "")
                server_id = tool_servers.get(tool_name, "other")
                counts[server_id] = counts.get(server_id, 0) + int(row["count"])
    items = [{"name": SERVER_NAMES.get(server_id, server_id), "value": value} for server_id, value in counts.items()]
    return JSONResponse({"items": items})


@router.get("/alerts")
def dashboard_alerts(request: Request) -> JSONResponse:
    """输入：可选 ``limit`` 查询参数。

    输出：最近服务异常、MCP 失败和慢请求告警数组。
    功能：从注册表与 Trace 指标动态生成运维告警，不复制告警存储。
    """

    limit = _query_int(request, "limit", 20, 1, 100)
    alerts: list[dict[str, str]] = []
    with runtime.engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT server_name, status, last_heartbeat FROM mcp_server_registry WHERE status <> 'online'")
        ).mappings()
        for row in rows:
            alerts.append(
                {
                    "time": str(row.get("last_heartbeat") or "尚未成功心跳"),
                    "level": "error" if str(row.get("status")) == "offline" else "warning",
                    "description": f"{row['server_name']} 当前状态：{row['status']}",
                    "status": "待处理",
                }
            )
    for event in reversed(_trace_events()):
        duration = float(event.get("duration_ms") or 0)
        failures = int(event.get("total_mcp_failures") or 0)
        if duration <= 30000 and failures <= 0:
            continue
        alerts.append(
            {
                "time": str(event.get("timestamp") or ""),
                "level": "error" if failures else "warning",
                "description": f"会话 {event.get('conversation_id')}：{'MCP 调用失败' if failures else f'响应耗时 {duration / 1000:.1f} 秒'}",
                "status": "待处理",
            }
        )
        if len(alerts) >= limit:
            break
    return JSONResponse({"items": alerts[:limit]})


