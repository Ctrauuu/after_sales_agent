"""验证 Hermes MCP/LLM Trace 的聚合和结构化日志输出。"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from mcp_suning import observability as backend_observability

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OBSERVABILITY_PATH = (
    PROJECT_ROOT / ".hermes" / "plugins" / "suning-rbac-bridge" / "observability.py"
)


def _load_observability() -> ModuleType:
    """输入：项目中 Hermes 插件观测模块的文件路径。

    输出：独立加载的观测模块；模块无法加载时抛出 ``RuntimeError``。
    功能：让后端测试无需安装 Hermes 宿主，也能覆盖真实 OpenTelemetry 聚合逻辑。
    """

    module_name = "_suning_observability_test"
    spec = importlib.util.spec_from_file_location(module_name, OBSERVABILITY_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载观测模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_trace_aggregates_llm_mcp_failure_and_resets_context(caplog: Any) -> None:
    """输入：固定的 LLM Span、成功和失败 MCP Span，以及 pytest 日志捕获器 ``caplog``。

    输出：无；聚合字段、失败计数或 ContextVar 未恢复时通过断言报告。
    功能：验证同一 Trace 同时容纳多个 MCP 调用，保留失败可见性并输出可供 Hermes 日志平台检索的 JSON 摘要。
    """

    module = _load_observability()
    client = module.AgentObservability()
    client._tracer = module.trace.NoOpTracerProvider().get_tracer("test")
    with caplog.at_level(logging.INFO):
        trace_record = client.start_trace(
            conversation_id="conv-001",
            user_id="U-H001",
            platform="feishu",
            query="查询退单原因",
        )
        llm_span = client.start_llm_span("intent_parsing")
        client.end_llm_span(
            llm_span,
            prompt_tokens=150,
            completion_tokens=80,
            prompt_cache_hit_tokens=120,
            prompt_cache_miss_tokens=30,
            model="deepseek",
        )
        successful_mcp = client.start_mcp_span("get_order_detail", "mcp-order")
        client.end_mcp_span(successful_mcp, rows_returned=1, success=True)
        failed_mcp = client.start_mcp_span("query_logistics", "mcp-logistics")
        client.end_mcp_span(
            failed_mcp,
            rows_returned=0,
            success=False,
            error="连接超时",
            retry_count=2,
            failure_type="TIMEOUT",
            circuit_state="CLOSED",
            degrade_level="L2_CORE",
            degraded=True,
        )
        summary = client.finish_trace(trace_record, "订单仍在物流环节")

    assert summary["total_llm_tokens"] == 230
    assert summary["total_prompt_cache_hit_tokens"] == 120
    assert summary["total_prompt_cache_miss_tokens"] == 30
    assert summary["total_mcp_calls"] == 2
    assert summary["total_mcp_failures"] == 1
    assert summary["trace_id"] == trace_record.trace_id
    assert failed_mcp.attributes == {
        "tool_name": "query_logistics",
        "server": "mcp-logistics",
        "rows_returned": 0,
        "success": False,
        "degraded": True,
        "retry_count": 2,
        "failure_type": "TIMEOUT",
        "circuit_state": "CLOSED",
        "degrade_level": "L2_CORE",
        "error_message": "连接超时",
    }
    assert module.current_trace_id.get() == ""
    assert any("suning_agent_trace" in record.message for record in caplog.records)


def test_timeline_trace_reuses_bridge_id_for_parallel_mcp_spans() -> None:
    """输入：桥接 MCP 元数据中的固定 Trace ID 和两条下游 MCP Span。

    输出：无；上下文未复位或下游 Span 无法结束时通过断言报告。
    功能：验证订单聚合服务会恢复桥接 Trace ID，使并行订单、售后、物流和支付调用能按同一业务链路检索。
    """

    scope = backend_observability.start_trace({"suning/trace_id": "trace-from-hermes"})
    first_span, first_started = backend_observability.start_mcp_span("get_order_detail", "order")
    second_span, second_started = backend_observability.start_mcp_span("query_logistics", "logistics")
    backend_observability.end_mcp_span(
        first_span,
        first_started,
        rows_returned=1,
        success=True,
    )
    backend_observability.end_mcp_span(
        second_span,
        second_started,
        rows_returned=0,
        success=False,
        error="连接超时",
    )
    backend_observability.finish_trace(scope, success=True)

    assert scope.trace_id == "trace-from-hermes"
    assert backend_observability.current_trace_id.get() == ""
