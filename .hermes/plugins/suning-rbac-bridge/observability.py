"""Hermes Agent 的 OpenTelemetry Trace 与结构化日志。"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import context, propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


logger = logging.getLogger(__name__)
current_trace_id: ContextVar[str] = ContextVar("suning_trace_id", default="")
_otel_configured = False


@dataclass
class TraceSpan:
    """保存一个 LLM 或 MCP 调用的聚合字段和 OTel 上下文。"""

    name: str
    started_at: float
    attributes: dict[str, Any]
    otel_span: Any = field(repr=False)
    otel_token: Any = field(repr=False)
    duration_ms: float = 0.0


@dataclass
class AgentTrace:
    """保存一次 Agent 回合汇总与上下文恢复所需的最小状态。"""

    trace_id: str
    conversation_id: str
    user_id: str
    platform: str
    started_at: float
    root_span: Any = field(repr=False)
    spans: list[TraceSpan] = field(default_factory=list)
    trace_token: Token[str] | None = field(default=None, repr=False)
    agent_trace_token: Token[AgentTrace | None] | None = field(default=None, repr=False)
    otel_token: Any = field(default=None, repr=False)


current_agent_trace: ContextVar[AgentTrace | None] = ContextVar(
    "suning_agent_trace",
    default=None,
)


def configure_otel() -> None:
    """输入：隐式读取标准 OTLP Trace Endpoint 环境变量。

    输出：无；配置 Endpoint 时首次注册 Batch Span Processor，已有全局 Provider 时保持宿主配置。
    功能：把插件 Span 导出到 OTel Collector；未配置 Endpoint 时仅保留 Hermes 结构化日志。
    """

    global _otel_configured
    if _otel_configured:
        return
    provider = trace.get_tracer_provider()
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    if type(provider).__name__ == "ProxyTracerProvider" and endpoint:
        configured_provider = TracerProvider(
            resource=Resource.create({"service.name": "suning-hermes-agent"}),
        )
        configured_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)),
        )
        trace.set_tracer_provider(configured_provider)
    _otel_configured = True


configure_otel()
_tracer = trace.get_tracer("suning.hermes")


def result_rows(result: Any) -> int:
    """输入：MCP 返回的结构化负载或 Client Result ``result``。

    输出：列表项、常见结果数组的数量，或对象型单结果的 ``1``。
    功能：统一记录 MCP 返回行数，不改变已有 MCP 响应格式。
    """

    payload = getattr(result, "structuredContent", result)
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("result", "results", "items", "traces", "refunds"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
        return 1
    return 0


def start_trace(
    *,
    conversation_id: str,
    user_id: str,
    platform: str,
    query: str,
) -> AgentTrace:
    """输入：逻辑会话、可信用户、来源平台和本轮用户问题。

    输出：已绑定 ContextVar 与 OTel 根 Span 的 ``AgentTrace``。
    功能：为一次 Agent 回合创建统一 trace_id，使后续异步 LLM 和 MCP 子调用归属同一链路。
    """

    del query
    trace_id = uuid.uuid4().hex[:16]
    root_span = _tracer.start_span(
        "agent.request",
        attributes={
            "suning.trace_id": trace_id,
            "conversation.id": conversation_id,
            "user.id": user_id,
            "platform": platform,
        },
    )
    agent_trace = AgentTrace(
        trace_id=trace_id,
        conversation_id=conversation_id,
        user_id=user_id,
        platform=platform,
        started_at=time.perf_counter(),
        root_span=root_span,
    )
    agent_trace.trace_token = current_trace_id.set(trace_id)
    agent_trace.agent_trace_token = current_agent_trace.set(agent_trace)
    agent_trace.otel_token = context.attach(trace.set_span_in_context(root_span))
    return agent_trace


def ensure_trace(
    *,
    conversation_id: str,
    user_id: str,
    platform: str,
    query: str,
) -> tuple[AgentTrace, bool]:
    """输入：当前调用可得的会话、用户、平台和问题或工具名称。

    输出：活动 ``AgentTrace`` 及是否由本次调用新建的布尔值。
    功能：正常生命周期复用回合 Trace，脱离 Hook 的 MCP 直调则补建完整 Trace。
    """

    active_trace = current_agent_trace.get()
    if active_trace is not None:
        return active_trace, False
    return (
        start_trace(
            conversation_id=conversation_id,
            user_id=user_id,
            platform=platform,
            query=query,
        ),
        True,
    )


def _start_span(name: str, attributes: dict[str, Any]) -> TraceSpan:
    """输入：Span 名称 ``name``、初始属性；隐式读取当前 Agent Trace。

    输出：已加入活动 Trace 并附着为当前上下文的未结束 ``TraceSpan``。
    功能：创建 OTel 子 Span，使调用期间注入的 traceparent 指向当前 LLM 或 MCP 调用。
    """

    agent_trace = current_agent_trace.get()
    if agent_trace is None:
        raise RuntimeError("当前请求没有活动 Trace")
    otel_span = _tracer.start_span(
        name,
        attributes={"suning.trace_id": agent_trace.trace_id, **attributes},
    )
    span = TraceSpan(
        name=name,
        started_at=time.perf_counter(),
        attributes=dict(attributes),
        otel_span=otel_span,
        otel_token=context.attach(trace.set_span_in_context(otel_span)),
    )
    agent_trace.spans.append(span)
    return span


def _end_span(
    span: TraceSpan,
    attributes: dict[str, Any],
    *,
    failed: bool = False,
    duration_ms: float | None = None,
) -> None:
    """输入：待结束 Span、附加属性、失败标记和可选的已测耗时。

    输出：无；补全耗时和属性、结束 OTel Span 并恢复调用前上下文。
    功能：统一 LLM 与 MCP 的必要收尾，优先采用 Hermes API 实测耗时并标注失败状态。
    """

    measured_ms = (time.perf_counter() - span.started_at) * 1000
    span.duration_ms = (
        duration_ms
        if duration_ms is not None and math.isfinite(duration_ms) and duration_ms >= 0
        else measured_ms
    )
    span.attributes.update(attributes)
    try:
        span.otel_span.set_attributes({**span.attributes, "duration_ms": span.duration_ms})
        if failed:
            span.otel_span.set_status(trace.Status(trace.StatusCode.ERROR))
        span.otel_span.end()
    finally:
        context.detach(span.otel_token)


def start_llm_span(name: str = "generation") -> TraceSpan:
    """输入：可选 LLM 阶段名称 ``name``；隐式读取当前 Agent Trace。

    输出：尚未结束的 LLM ``TraceSpan``；没有活动 Trace 时抛出 ``RuntimeError``。
    功能：在真实模型 API 请求边界创建可记录 Token、模型和耗时的子 Span。
    """

    return _start_span(f"llm.{name}", {})


def end_llm_span(
    span: TraceSpan,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    prompt_cache_hit_tokens: int = 0,
    prompt_cache_miss_tokens: int = 0,
    model: str = "",
    duration_ms: float | None = None,
    error: str = "",
) -> None:
    """输入：LLM Span、输入/输出 Token、模型名、可选 API 耗时与错误文本。

    输出：无；结束 Span 并写入模型、普通及缓存 Token、耗时和可选失败属性。
    功能：归档 Hermes 单次模型 API 调用的真实用量，并区分上下文缓存命中与未命中 Token。
    """

    prompt_tokens = max(prompt_tokens, 0)
    completion_tokens = max(completion_tokens, 0)
    _end_span(
        span,
        {
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_cache_hit_tokens": max(prompt_cache_hit_tokens, 0),
            "prompt_cache_miss_tokens": max(prompt_cache_miss_tokens, 0),
            "error_message": error,
        },
        duration_ms=duration_ms,
        failed=bool(error),
    )


def start_mcp_span(tool_name: str, server: str) -> TraceSpan:
    """输入：MCP 工具名 ``tool_name`` 与目标服务地址 ``server``。

    输出：尚未结束且已附着为当前上下文的 MCP ``TraceSpan``。
    功能：为一次私有 MCP 逻辑调用记录工具、服务及可跨进程传播的父节点。
    """

    return _start_span(
        f"mcp.{tool_name}",
        {"tool_name": tool_name, "server": server},
    )


def inject_trace_metadata(metadata: dict[str, Any]) -> None:
    """输入：即将发送到 MCP 的私有元数据字典 ``metadata``。

    输出：无；原地写入当前 MCP Span 的 W3C ``traceparent`` 等传播字段。
    功能：让跨进程 MCP 服务恢复真实调用父节点，同时保留业务级 trace_id 日志关联。
    """

    propagate.inject(metadata)


def end_mcp_span(
    span: TraceSpan,
    *,
    rows_returned: int,
    success: bool,
    error: str = "",
    retry_count: int = 0,
    failure_type: str = "",
    circuit_state: str = "",
    degrade_level: str = "",
    degraded: bool = False,
) -> None:
    """输入：MCP Span、结果统计、resilience 状态和可选错误文本。

    输出：无；结束 Span 并写入成功、降级、重试和熔断属性。
    功能：在单个逻辑 MCP Span 上记录完整调用结果，不为每次 retry 重复创建 Span。
    """

    _end_span(
        span,
        {
            "rows_returned": max(rows_returned, 0),
            "success": success,
            "degraded": degraded,
            "retry_count": max(retry_count, 0),
            "failure_type": failure_type,
            "circuit_state": circuit_state,
            "degrade_level": degrade_level,
            "error_message": error,
        },
        failed=not success,
    )


def finish_trace(agent_trace: AgentTrace, final_response: str = "") -> dict[str, Any]:
    """输入：活动 ``AgentTrace`` 与可选最终回复文本。

    输出：供日志、管理后台与测试使用的聚合指标字典。
    功能：汇总 Token、MCP 成败、墙钟耗时和最慢 Span，结束根 Span 并恢复请求前上下文。
    """

    llm_spans = [span for span in agent_trace.spans if span.name.startswith("llm.")]
    mcp_spans = [span for span in agent_trace.spans if span.name.startswith("mcp.")]
    total_llm_tokens = sum(int(span.attributes.get("total_tokens", 0)) for span in llm_spans)
    duration_ms = (time.perf_counter() - agent_trace.started_at) * 1000
    slowest_span = max(agent_trace.spans, key=lambda span: span.duration_ms, default=None)
    summary = {
        "event": "suning_agent_trace",
        "trace_id": agent_trace.trace_id,
        "conversation_id": agent_trace.conversation_id,
        "user_id": agent_trace.user_id,
        "platform": agent_trace.platform,
        "duration_ms": round(duration_ms, 3),
        "total_llm_tokens": total_llm_tokens,
        "total_prompt_cache_hit_tokens": sum(
            int(span.attributes.get("prompt_cache_hit_tokens", 0)) for span in llm_spans
        ),
        "total_prompt_cache_miss_tokens": sum(
            int(span.attributes.get("prompt_cache_miss_tokens", 0)) for span in llm_spans
        ),
        "total_mcp_calls": len(mcp_spans),
        "total_mcp_failures": sum(
            not bool(span.attributes.get("success", True)) for span in mcp_spans
        ),
        "slowest_span": slowest_span.name if slowest_span else "",
        "slowest_span_ms": round(slowest_span.duration_ms, 3) if slowest_span else 0.0,
        "response_chars": len(final_response),
    }
    logger.info("%s", json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if duration_ms > 30_000 or total_llm_tokens > 50_000:
        logger.warning(
            "%s",
            json.dumps(
                {"event": "suning_agent_trace_alert", **summary},
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    agent_trace.root_span.set_attributes(summary)
    agent_trace.root_span.end()
    if agent_trace.otel_token is not None:
        context.detach(agent_trace.otel_token)
    if agent_trace.agent_trace_token is not None:
        current_agent_trace.reset(agent_trace.agent_trace_token)
    if agent_trace.trace_token is not None:
        current_trace_id.reset(agent_trace.trace_token)
    return summary


__all__ = [
    "AgentTrace",
    "TraceSpan",
    "current_agent_trace",
    "current_trace_id",
    "end_llm_span",
    "end_mcp_span",
    "ensure_trace",
    "finish_trace",
    "inject_trace_metadata",
    "result_rows",
    "start_llm_span",
    "start_mcp_span",
    "start_trace",
]
