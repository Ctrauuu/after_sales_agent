"""订单全链路 MCP：并行聚合订单、售后、物流和支付数据。"""

from typing import Any

from fastmcp import Context, FastMCP

from mcp_suning.auth_middleware import (
    authorize_mcp_request_with_principal,
    interceptor,
)
from mcp_suning.auth_attestation import _metadata_from_context
from mcp_suning.config import settings
from mcp_suning.order_timeline import OrderTimelineTracker
from mcp_suning.observability import finish_trace, start_trace
from mcp_suning.timeline_client import TimelineMCPGateway


mcp = FastMCP("mcp-order-timeline")


@mcp.tool(
    name="trace_order_timeline",
    description=(
        "根据订单 ID 并行查询订单、售后工单、逆向物流和退款，"
        "返回标准化售后时间线、当前卡点、SLA 状态和部分失败来源。"
    ),
)
async def trace_order_timeline(order_id: int, ctx: Context) -> dict[str, Any]:
    """输入：正整数订单 ID ``order_id`` 和 FastMCP 上下文 ``ctx``。

    输出：脱敏后的跨系统时间线、当前卡点、SLA 状态与可能的下游失败信息。
    功能：验证顶层调用身份后恢复上游 Trace、并发调用四个私有 MCP，并在不阻断部分结果的前提下聚合售后全链路。
    """

    if order_id < 1:
        raise ValueError("order_id 必须大于或等于 1")
    principal, _user, safe_filters = authorize_mcp_request_with_principal(
        ctx,
        "trace_order_timeline",
        {},
    )
    scope = start_trace(_metadata_from_context(ctx))
    try:
        gateway = TimelineMCPGateway(principal)
        tracker = OrderTimelineTracker(
            {
                "order": gateway.fetch_order,
                "aftersale": gateway.fetch_aftersale,
                "logistics": gateway.fetch_logistics,
                "payment": gateway.fetch_payment,
            },
            timeout_seconds=settings.order_timeline_timeout_seconds,
        )
        result = await tracker.trace(order_id)
        return interceptor.mask_sensitive_data(result.to_dict(), safe_filters["data_scope"])
    except Exception:
        finish_trace(scope, success=False)
        raise
    else:
        finish_trace(scope, success=True)


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8106, path="/mcp")
