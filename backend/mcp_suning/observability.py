"""订单聚合 MCP 的 OpenTelemetry 子 Span 与链路上下文传播。"""

from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Mapping

from opentelemetry import context, propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from mcp_suning.config import settings

logger = logging.getLogger(__name__)
current_trace_id: ContextVar[str] = ContextVar("suning_backend_trace_id", default="")
_configured = False


@dataclass
class TraceScope:
    """保存一次进入订单聚合 MCP 的 Trace 上下文恢复令牌。"""

    trace_id: str
    started_at: float
    root_span: Any
    trace_token: Any
    parent_token: Any
    span_token: Any


def configure_otel() -> None:
    """输入：隐式读取 ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``。

    输出：无；配置端点时注册 OTel 批量导出器。
    功能：使订单聚合服务将自身及下游私有 MCP Span 导出到与 Hermes 相同的 Collector；无端点时保留无侵入日志路径。
    """

    global _configured
    if _configured:
        return
    endpoint = settings.otel_exporter_otlp_traces_endpoint.strip()
    provider = trace.get_tracer_provider()
    if endpoint and type(provider).__name__ == "ProxyTracerProvider":
        configured_provider = TracerProvider(
            resource=Resource.create({"service.name": "suning-mcp-order-timeline"}),
        )
        configured_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)),
        )
        trace.set_tracer_provider(configured_provider)
    _configured = True


def start_trace(metadata: Mapping[str, Any]) -> TraceScope:
    """输入：FastMCP 请求元数据 ``metadata``，可含桥接写入的 ``traceparent`` 和业务 Trace ID。

    输出：绑定上游 OTel 父链路的 ``TraceScope``。
    功能：恢复 Hermes 传来的跨进程 Trace 上下文，为订单聚合和并行私有 MCP 调用创建同一条可视化调用链。
    """

    configure_otel()
    trace_id = str(metadata.get("suning/trace_id") or uuid.uuid4().hex[:16])
    parent_token = context.attach(propagate.extract(dict(metadata)))
    root_span = trace.get_tracer("suning.hermes").start_span(
        "mcp.trace_order_timeline",
        attributes={"suning.trace_id": trace_id},
    )
    span_token = context.attach(trace.set_span_in_context(root_span))
    return TraceScope(
        trace_id=trace_id,
        started_at=time.perf_counter(),
        root_span=root_span,
        trace_token=current_trace_id.set(trace_id),
        parent_token=parent_token,
        span_token=span_token,
    )


def finish_trace(scope: TraceScope, *, success: bool) -> None:
    """输入：已开始的 ``TraceScope`` 和订单聚合是否成功的布尔值。

    输出：无；结束服务端根 Span、写入结构化日志并恢复上游上下文。
    功能：记录整个订单全链路聚合的墙钟耗时，避免服务端 ContextVar 泄漏至后续 MCP 请求。
    """

    duration_ms = (time.perf_counter() - scope.started_at) * 1000
    scope.root_span.set_attributes({"success": success, "duration_ms": duration_ms})
    if not success:
        scope.root_span.set_status(trace.Status(trace.StatusCode.ERROR))
    scope.root_span.end()
    logger.info(
        "suning_mcp_trace trace_id=%s success=%s duration_ms=%.3f",
        scope.trace_id,
        success,
        duration_ms,
    )
    context.detach(scope.span_token)
    current_trace_id.reset(scope.trace_token)
    context.detach(scope.parent_token)


def start_mcp_span(tool_name: str, server: str) -> tuple[Any, float]:
    """输入：下游工具名 ``tool_name`` 与服务地址 ``server``。

    输出：OpenTelemetry Span 和单调时钟开始时间元组。
    功能：为订单聚合器发起的每个并行私有 MCP 调用记录同一 Trace 下的独立耗时区间。
    """

    span = trace.get_tracer("suning.hermes").start_span(
        f"mcp.{tool_name}",
        attributes={"suning.trace_id": current_trace_id.get(), "server": server},
    )
    return span, time.perf_counter()


def inject_trace_metadata(metadata: dict[str, Any]) -> None:
    """输入：即将通过私有 MCP 发送的元数据字典 ``metadata``。

    输出：无；原地写入当前 OTel 上下文的 W3C 传播字段。
    功能：让后续已接入观测的下游服务可继续同一条 Trace，而不影响已有身份凭证元数据。
    """

    propagate.inject(metadata)


def end_mcp_span(
    span: Any,
    started_at: float,
    *,
    rows_returned: int,
    success: bool,
    error: str = "",
) -> None:
    """输入：下游 Span、开始时间、返回行数、成功状态和可选错误信息。

    输出：无；写入属性并结束该私有 MCP Span。
    功能：使并行订单、售后、物流和支付请求分别暴露耗时、行数和失败原因，便于定位慢源或静默降级。
    """

    attributes = {
        "duration_ms": (time.perf_counter() - started_at) * 1000,
        "rows_returned": max(rows_returned, 0),
        "success": success,
        "error_message": error,
    }
    span.set_attributes(attributes)
    if not success:
        span.set_status(trace.Status(trace.StatusCode.ERROR))
    span.end()


def result_rows(payload: Any) -> int:
    """输入：下游 MCP 解析后的业务负载 ``payload``。

    输出：列表或常见业务数组的元素数量；对象型成功结果返回 ``1``。
    功能：统一四个异构下游返回格式的行数指标，不为观测额外创建业务 Schema。
    """

    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, Mapping):
        for key in ("result", "results", "items", "traces", "refunds"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
        return 1
    return 0
