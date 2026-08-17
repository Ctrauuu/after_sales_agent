"""为订单全链路聚合服务提供带身份委托的异步私有 MCP 客户端。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mcp_suning.security.attestation import (
    AuthenticatedPrincipal,
    mint_delegated_attestation,
)
from mcp_suning.config import settings
from mcp_suning.observability import (
    end_mcp_span,
    inject_trace_metadata,
    result_rows,
    start_mcp_span,
)


class DownstreamMCPError(RuntimeError):
    """表示下游 MCP 的网络、协议或业务错误。"""


def _unwrap_fastmcp_result_envelope(payload: Any) -> Any:
    """输入：下游 MCP 返回的结构化或 JSON 反序列化负载 ``payload``。

    输出：仅含 ``result`` 字段的 FastMCP 非对象结果信封会返回其内部值，其他负载原样返回。
    功能：兼容 FastMCP 对列表等非对象工具返回值添加的结构化内容包装，避免聚合器误把节点列表识别为对象。
    """

    if isinstance(payload, Mapping) and set(payload) == {"result"}:
        return payload["result"]
    return payload


def _result_text(result: Any) -> str:
    """输入：MCP ``call_tool`` 返回对象 ``result``。

    输出：所有非空文本内容块拼接后的文本。
    功能：兼容没有结构化内容的 MCP 服务，并提取下游错误信息或 JSON 回退载荷。
    """

    return "\n".join(
        text.strip()
        for block in (getattr(result, "content", None) or [])
        if isinstance((text := getattr(block, "text", None)), str) and text.strip()
    )


def _result_payload(result: Any) -> Any:
    """输入：成功的 MCP ``call_tool`` 返回对象 ``result``。

    输出：优先返回结构化内容；否则解析唯一 JSON 文本，无法解析时抛出 ``DownstreamMCPError``。
    功能：把不同 FastMCP 返回编码统一为订单时间线引擎可聚合的 Python 数据。
    """

    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return _unwrap_fastmcp_result_envelope(structured)
    text = _result_text(result)
    if not text:
        raise DownstreamMCPError("下游 MCP 未返回结构化内容")
    try:
        return _unwrap_fastmcp_result_envelope(json.loads(text))
    except json.JSONDecodeError as exc:
        raise DownstreamMCPError("下游 MCP 返回的文本不是 JSON") from exc


class PrivateMCPCaller:
    """通过私有 Streamable HTTP 调用一个下游 MCP 工具。"""

    async def call(
        self,
        *,
        endpoint: str,
        tool_name: str,
        arguments: dict[str, Any],
        principal: AuthenticatedPrincipal,
    ) -> Any:
        """输入：下游 MCP 地址、目标工具名、业务参数和已验证调用主体。

        输出：下游 MCP 的 JSON 结构化负载；网络、协议或业务拒绝时抛出 ``DownstreamMCPError``。
        功能：为每次子调用签发独立的一次性委托凭证，并通过 MCP 协议安全获取其只读业务结果。
        """

        attestation = mint_delegated_attestation(principal, tool_name=tool_name)
        span, started_at = start_mcp_span(tool_name, endpoint)
        try:
            async with streamable_http_client(endpoint) as (read, write, _session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    metadata = {"suning/authn": attestation}
                    inject_trace_metadata(metadata)
                    result = await session.call_tool(
                        tool_name,
                        arguments=arguments,
                        meta=metadata,
                    )
        except DownstreamMCPError as exc:
            end_mcp_span(span, started_at, rows_returned=0, success=False, error=str(exc))
            raise
        except Exception as exc:
            error = DownstreamMCPError(f"MCP 请求失败: {exc}")
            end_mcp_span(span, started_at, rows_returned=0, success=False, error=str(error))
            raise error from exc
        if bool(getattr(result, "isError", False)):
            error = DownstreamMCPError(_result_text(result) or "下游 MCP 拒绝调用")
            end_mcp_span(span, started_at, rows_returned=0, success=False, error=str(error))
            raise error
        try:
            payload = _result_payload(result)
        except DownstreamMCPError as exc:
            end_mcp_span(span, started_at, rows_returned=0, success=False, error=str(exc))
            raise
        end_mcp_span(span, started_at, rows_returned=result_rows(payload), success=True)
        return payload


class TimelineMCPGateway:
    """把一个订单 ID 映射为四个可并行执行的私有 MCP 获取函数。"""

    def __init__(
        self,
        principal: AuthenticatedPrincipal,
        *,
        caller: PrivateMCPCaller | None = None,
    ) -> None:
        """输入：顶层授权完成的主体 ``principal`` 和可选 MCP 调用器替身。

        输出：绑定同一可信身份的下游订单、售后、物流和支付网关。
        功能：保存委托身份并允许测试替换网络调用器，保证四路子调用的用户与权限范围一致。
        """

        self.principal = principal
        self.caller = caller or PrivateMCPCaller()

    async def fetch_order(self, order_id: int) -> Any:
        """输入：正整数订单 ID ``order_id``。

        输出：订单 MCP 的订单详情负载。
        功能：请求订单系统提供订单、关联退单类型和品类信息，作为时间线和 SLA 的基础数据。
        """

        return await self.caller.call(
            endpoint=settings.suning_mcp_order_url,
            tool_name="get_order_detail",
            arguments={"order_id": order_id},
            principal=self.principal,
        )

    async def fetch_aftersale(self, order_id: int) -> Any:
        """输入：正整数订单 ID ``order_id``。

        输出：售后 MCP 的工单流转节点列表。
        功能：按订单 ID 请求售后系统自行解析退单映射，避免聚合器为拿到 ``return_id`` 串行等待订单系统。
        """

        return await self.caller.call(
            endpoint=settings.suning_mcp_aftersale_url,
            tool_name="get_aftersale_workflow",
            arguments={"order_id": order_id},
            principal=self.principal,
        )

    async def fetch_logistics(self, order_id: int) -> Any:
        """输入：正整数订单 ID ``order_id``。

        输出：物流 MCP 的逆向物流轨迹负载。
        功能：按订单 ID 请求物流系统解析关联退单并返回可用于取件、运输和入库阶段的事实节点。
        """

        return await self.caller.call(
            endpoint=settings.suning_mcp_logistics_url,
            tool_name="query_logistics",
            arguments={"order_id": order_id},
            principal=self.principal,
        )

    async def fetch_payment(self, order_id: int) -> Any:
        """输入：正整数订单 ID ``order_id``。

        输出：支付 MCP 的退款状态负载。
        功能：按订单 ID 请求支付系统解析关联退单并返回退款金额、方式、时间和最终状态。
        """

        return await self.caller.call(
            endpoint=settings.suning_mcp_payment_url,
            tool_name="get_refund_status",
            arguments={"order_id": order_id},
            principal=self.principal,
        )


__all__ = ["DownstreamMCPError", "PrivateMCPCaller", "TimelineMCPGateway"]
