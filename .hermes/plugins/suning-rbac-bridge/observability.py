"""Hermes Agent 的 OpenTelemetry Trace 与结构化日志封装。"""

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
current_conversation_id: ContextVar[str] = ContextVar(
    "suning_conversation_id",
    default="",
)
_otel_configured = False


@dataclass
class TraceSpan:
    """保存一个 LLM 或 MCP 调用的可导出观测字段。"""

    span_id: str
    parent_span_id: str | None
    name: str
    start_time: float
    attributes: dict[str, Any] = field(default_factory=dict)
    end_time: float = 0.0
    duration_ms: float = 0.0
    otel_span: Any = field(default=None, repr=False)


@dataclass
class AgentTrace:
    """保存一次 Agent 回合的聚合 Trace 信息。"""

    trace_id: str
    conversation_id: str
    user_id: str
    platform: str
    user_query: str
    start_time: float = field(default_factory=time.perf_counter)
    spans: list[TraceSpan] = field(default_factory=list)
    total_llm_tokens: int = 0
    total_prompt_cache_hit_tokens: int = 0
    total_prompt_cache_miss_tokens: int = 0
    total_mcp_calls: int = 0
    total_mcp_failures: int = 0
    root_span: Any = field(default=None, repr=False)
    trace_token: Token[str] | None = field(default=None, repr=False)
    conversation_token: Token[str] | None = field(default=None, repr=False)
    agent_trace_token: Token[AgentTrace | None] | None = field(default=None, repr=False)
    otel_token: Any = field(default=None, repr=False)


current_agent_trace: ContextVar[AgentTrace | None] = ContextVar(
    "suning_agent_trace",
    default=None,
)


def configure_otel() -> None:
    """输入：隐式读取标准 OTLP Trace Endpoint 环境变量。

    输出：无；配置 Endpoint 时首次注册 Batch Span Processor，已有全局 Provider 时保持宿主配置。
    功能：把插件生成的 Span 导出到 OTel Collector；未配置 Endpoint 时仅保留 Hermes 结构化日志且不覆盖宿主 Provider。
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


def _span_id(otel_span: Any) -> str:
    """输入：OpenTelemetry Span ``otel_span``。

    输出：十六进制 Span ID；无 SDK 记录器时返回随机 ID。
    功能：为结构化日志提供稳定可读的 Span 标识，同时兼容无操作 OTel Provider。
    """

    span_context = otel_span.get_span_context()
    return f"{span_context.span_id:016x}" if span_context.span_id else uuid.uuid4().hex[:16]


def _result_rows(result: Any) -> int:
    """输入：MCP 返回的结构化负载或 Client Result ``result``。

    输出：列表项、常见结果数组的数量，或对象型单结果的 ``1``。
    功能：以低成本统一记录 MCP 返回行数，不改变已有 MCP 的响应格式。
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


class AgentObservability:
    """在 Hermes 生命周期内创建并汇总 LLM、MCP 调用 Span。"""

    def __init__(self) -> None:
        """输入：无；隐式读取 OTel 全局 Provider 和 OTLP Endpoint 配置。

        输出：可用于当前进程所有请求的观测对象。
        功能：按可选 Endpoint 初始化一次 OTel 导出器并获取项目专用 Tracer，不创建额外的观测平台客户端。
        """

        configure_otel()
        self._tracer = trace.get_tracer("suning.hermes")

    def start_trace(
        self,
        *,
        conversation_id: str,
        user_id: str,
        platform: str,
        query: str,
    ) -> AgentTrace:
        """输入：逻辑会话、可信用户、来源平台和本轮用户问题。

        输出：已绑定 ContextVar 与 OTel 根 Span 的 ``AgentTrace``。
        功能：为一次 Agent 回合创建统一 trace_id，使后续异步 LLM 和 MCP 子调用自动归属同一链路。
        """

        trace_id = uuid.uuid4().hex[:16]
        root_span = self._tracer.start_span(
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
            user_query=query,
            root_span=root_span,
        )
        agent_trace.trace_token = current_trace_id.set(trace_id)
        agent_trace.conversation_token = current_conversation_id.set(conversation_id)
        agent_trace.agent_trace_token = current_agent_trace.set(agent_trace)
        agent_trace.otel_token = context.attach(trace.set_span_in_context(root_span))
        return agent_trace

    def ensure_trace(
        self,
        *,
        conversation_id: str,
        user_id: str,
        platform: str,
        query: str,
    ) -> tuple[AgentTrace, bool]:
        """输入：当前调用可得的会话、用户、平台和问题或工具名称。

        输出：活动 ``AgentTrace`` 及是否由本次调用新建的布尔值。
        功能：让正常生命周期复用同一回合 Trace，并让脱离 Hook 的 MCP 直调仍可完整记录。
        """

        active_trace = current_agent_trace.get()
        if active_trace is not None:
            return active_trace, False
        return (
            self.start_trace(
                conversation_id=conversation_id,
                user_id=user_id,
                platform=platform,
                query=query,
            ),
            True,
        )

    def start_llm_span(self, name: str = "generation") -> TraceSpan:
        """输入：可选 LLM 阶段名称 ``name``；隐式读取当前 Agent Trace。

        输出：尚未结束的 LLM ``TraceSpan``；没有活动 Trace 时抛出 ``RuntimeError``。
        功能：记录模型决策或回复生成的开始时间，并作为 OTel 根 Span 的子节点。
        """

        return self._start_span(f"llm.{name}", {})

    def end_llm_span(
        self,
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
        功能：把 Hermes 单次 API 调用的真实用量归档到 Trace，区分 DeepSeek 上下文缓存命中与未命中 Token。
        """

        self._end_span(
            span,
            {
                "model": model,
                "prompt_tokens": max(prompt_tokens, 0),
                "completion_tokens": max(completion_tokens, 0),
                "total_tokens": max(prompt_tokens, 0) + max(completion_tokens, 0),
                "prompt_cache_hit_tokens": max(prompt_cache_hit_tokens, 0),
                "prompt_cache_miss_tokens": max(prompt_cache_miss_tokens, 0),
                "error_message": error,
            },
            duration_ms=duration_ms,
            failed=bool(error),
        )

    def start_mcp_span(self, tool_name: str, server: str) -> TraceSpan:
        """输入：MCP 工具名 ``tool_name`` 与目标服务地址 ``server``。

        输出：尚未结束的 MCP ``TraceSpan``。
        功能：为每次私有 MCP 调用生成可并发的子 Span，并保留工具和服务定位信息。
        """

        return self._start_span(
            f"mcp.{tool_name}",
            {"tool_name": tool_name, "server": server},
        )

    @staticmethod
    def inject_trace_metadata(metadata: dict[str, Any]) -> None:
        """输入：即将发送到 MCP 的私有元数据字典 ``metadata``。

        输出：无；原地写入 W3C ``traceparent`` 等传播字段。
        功能：让跨进程 MCP 服务能恢复当前 OTel 父链路，而非只依赖业务级 ``trace_id`` 关联日志。
        """

        propagate.inject(metadata)

    def end_mcp_span(
        self,
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
        功能：在单个逻辑 MCP Span 上记录 resilience 决策，不按 retry attempt 扩大调用数。
        """

        self._end_span(
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

    def finish_trace(self, agent_trace: AgentTrace, final_response: str = "") -> dict[str, Any]:
        """输入：活动 ``AgentTrace`` 与可选最终回复文本。

        输出：供日志与测试使用的聚合指标字典。
        功能：汇总 Token、MCP 成败、真实墙钟耗时和最慢 Span，写入 Hermes 结构化日志并在阈值超限时告警。
        """

        agent_trace.total_llm_tokens = sum(
            int(span.attributes.get("total_tokens", 0))
            for span in agent_trace.spans
            if span.name.startswith("llm.")
        )
        agent_trace.total_prompt_cache_hit_tokens = sum(
            int(span.attributes.get("prompt_cache_hit_tokens", 0))
            for span in agent_trace.spans
            if span.name.startswith("llm.")
        )
        agent_trace.total_prompt_cache_miss_tokens = sum(
            int(span.attributes.get("prompt_cache_miss_tokens", 0))
            for span in agent_trace.spans
            if span.name.startswith("llm.")
        )
        mcp_spans = [span for span in agent_trace.spans if span.name.startswith("mcp.")]
        agent_trace.total_mcp_calls = len(mcp_spans)
        agent_trace.total_mcp_failures = sum(
            not bool(span.attributes.get("success", True)) for span in mcp_spans
        )
        duration_ms = (time.perf_counter() - agent_trace.start_time) * 1000
        slowest_span = max(agent_trace.spans, key=lambda span: span.duration_ms, default=None)
        summary = {
            "event": "suning_agent_trace",
            "trace_id": agent_trace.trace_id,
            "conversation_id": agent_trace.conversation_id,
            "user_id": agent_trace.user_id,
            "platform": agent_trace.platform,
            "duration_ms": round(duration_ms, 3),
            "total_llm_tokens": agent_trace.total_llm_tokens,
            "total_prompt_cache_hit_tokens": agent_trace.total_prompt_cache_hit_tokens,
            "total_prompt_cache_miss_tokens": agent_trace.total_prompt_cache_miss_tokens,
            "total_mcp_calls": agent_trace.total_mcp_calls,
            "total_mcp_failures": agent_trace.total_mcp_failures,
            "slowest_span": slowest_span.name if slowest_span else "",
            "slowest_span_ms": round(slowest_span.duration_ms, 3) if slowest_span else 0.0,
            "response_chars": len(final_response),
        }
        logger.info("%s", json.dumps(summary, ensure_ascii=False, sort_keys=True))
        if duration_ms > 30_000 or agent_trace.total_llm_tokens > 50_000:
            logger.warning("%s", json.dumps({"event": "suning_agent_trace_alert", **summary}, ensure_ascii=False, sort_keys=True))
        agent_trace.root_span.set_attributes(summary)
        agent_trace.root_span.end()
        self._reset_trace_context(agent_trace)
        return summary

    def _start_span(self, name: str, attributes: dict[str, Any]) -> TraceSpan:
        """输入：Span 名称 ``name``、初始属性；隐式读取当前 Agent Trace。

        输出：已加入活动 Trace 的未结束 ``TraceSpan``。
        功能：集中创建 OTel 子 Span 与本地可聚合镜像，确保 LLM/MCP 具有一致 trace_id 和父节点。
        """

        agent_trace = current_agent_trace.get()
        if agent_trace is None:
            raise RuntimeError("当前请求没有活动 Trace")
        otel_span = self._tracer.start_span(
            name,
            attributes={"suning.trace_id": agent_trace.trace_id, **attributes},
        )
        span = TraceSpan(
            span_id=_span_id(otel_span),
            parent_span_id=_span_id(agent_trace.root_span),
            name=name,
            start_time=time.perf_counter(),
            attributes=dict(attributes),
            otel_span=otel_span,
        )
        agent_trace.spans.append(span)
        return span

    def _end_span(
        self,
        span: TraceSpan,
        attributes: dict[str, Any],
        *,
        failed: bool = False,
        duration_ms: float | None = None,
    ) -> None:
        """输入：待结束 Span、附加属性、失败标记和可选的已测耗时。

        输出：无；原地补全耗时和属性，结束对应 OTel Span。
        功能：统一处理 LLM 与 MCP 的结束记录，优先采用 Hermes API 实测耗时并在失败时标注 ERROR。
        """

        span.end_time = time.perf_counter()
        span.duration_ms = (
            duration_ms
            if duration_ms is not None and math.isfinite(duration_ms) and duration_ms >= 0
            else (span.end_time - span.start_time) * 1000
        )
        span.attributes.update(attributes)
        span.otel_span.set_attributes({**span.attributes, "duration_ms": span.duration_ms})
        if failed:
            span.otel_span.set_status(trace.Status(trace.StatusCode.ERROR))
        span.otel_span.end()

    @staticmethod
    def _reset_trace_context(agent_trace: AgentTrace) -> None:
        """输入：已完成的 ``AgentTrace`` 及其保存的 ContextVar、OTel token。

        输出：无；恢复调用前上下文。
        功能：防止异步工作线程复用上一位用户的 Trace、会话或 OTel 父 Span。
        """

        if agent_trace.otel_token is not None:
            context.detach(agent_trace.otel_token)
        if agent_trace.agent_trace_token is not None:
            current_agent_trace.reset(agent_trace.agent_trace_token)
        if agent_trace.conversation_token is not None:
            current_conversation_id.reset(agent_trace.conversation_token)
        if agent_trace.trace_token is not None:
            current_trace_id.reset(agent_trace.trace_token)


observability = AgentObservability()


__all__ = [
    "AgentObservability",
    "AgentTrace",
    "TraceSpan",
    "current_agent_trace",
    "current_conversation_id",
    "current_trace_id",
    "observability",
    "_result_rows",
]
