"""苏宁业务插件向 Hermes 暴露的工具定义。

这些 schema 只包含模型可以填写的业务查询参数。用户身份、工号、角色和权限
均不属于工具参数，而是由 :mod:`bridge` 从 Hermes 请求级会话上下文中读取。

第一层：工具暴露白名单
TOOL_SPECS 决定模型能看到哪些工具

第二层：参数白名单
每个 schema 的 properties + additionalProperties: false
决定模型能填写哪些参数

第三层：服务端授权
MCP 验证身份凭证、角色和数据范围
决定本次调用是否真的允许执行
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .mcp_resilience import DegradeLevel


@dataclass(frozen=True)
class ToolSpec:
    """单个业务工具对应的私有 MCP 地址与 Hermes 工具 schema。"""

    server_id: str
    degrade_level: DegradeLevel
    retry_on_timeout: bool
    empty_result_is_success: bool
    endpoint_env: str
    default_endpoint: str
    schema: dict[str, Any]


TOOL_SPECS: dict[str, ToolSpec] = {
    "search_orders": ToolSpec(
        server_id="mcp-order",
        degrade_level=DegradeLevel.L2_CORE,
        retry_on_timeout=True,
        empty_result_is_success=True,
        endpoint_env="SUNING_MCP_ORDER_URL",
        default_endpoint="http://127.0.0.1:8101/mcp",
        schema={
            "name": "search_orders",
            "description": "按状态、品类、时间范围和区域查询订单。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "category": {"type": "string"},
                    "date_range_days": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 7,
                    },
                    "region": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 50,
                    },
                },
                "additionalProperties": False,
            },
        },
    ),
    "get_order_detail": ToolSpec(
        server_id="mcp-order",
        degrade_level=DegradeLevel.L2_CORE,
        retry_on_timeout=True,
        empty_result_is_success=False,
        endpoint_env="SUNING_MCP_ORDER_URL",
        default_endpoint="http://127.0.0.1:8101/mcp",
        schema={
            "name": "get_order_detail",
            "description": "根据订单 ID 查询订单详情。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "integer", "minimum": 1},
                },
                "required": ["order_id"],
                "additionalProperties": False,
            },
        },
    ),
    "query_return_stats_nl2sql": ToolSpec(
        server_id="mcp-aftersale",
        degrade_level=DegradeLevel.L2_CORE,
        retry_on_timeout=True,
        empty_result_is_success=True,
        endpoint_env="SUNING_MCP_AFTERSALE_URL",
        default_endpoint="http://127.0.0.1:8102/mcp",
        schema={
            "name": "query_return_stats_nl2sql",
            "description": (
                "通过共用 NL2SQL Pipeline，按日期、品类、退单原因、区域或品牌"
                "动态聚合最近若干天的退单。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "group_by": {
                        "type": "string",
                        "enum": ["day", "category", "reason", "region", "brand"],
                        "default": "category",
                    },
                    "date_range_days": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 7,
                    },
                    "category": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    ),
    "query_aftersale_nl2sql": ToolSpec(
        server_id="mcp-aftersale",
        degrade_level=DegradeLevel.L2_CORE,
        retry_on_timeout=True,
        empty_result_is_success=True,
        endpoint_env="SUNING_MCP_AFTERSALE_URL",
        default_endpoint="http://127.0.0.1:8102/mcp",
        schema={
            "name": "query_aftersale_nl2sql",
            "description": (
                "使用自然语言执行只读售后聚合分析。"
                "仅用于退单数量、金额及品类、原因、区域、品牌等统计问题。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                        "description": "需要分析的自然语言售后统计问题",
                    }
                },
                "required": ["question"],
                "additionalProperties": False,
            },
        },
    ),
    "query_sku_return_rate": ToolSpec(
        server_id="mcp-aftersale",
        degrade_level=DegradeLevel.L2_CORE,
        retry_on_timeout=True,
        empty_result_is_success=True,
        endpoint_env="SUNING_MCP_AFTERSALE_URL",
        default_endpoint="http://127.0.0.1:8102/mcp",
        schema={
            "name": "query_sku_return_rate",
            "description": (
                "按订单创建时间计算 SKU 退单订单率，"
                "返回退单订单数、订单数和退单率最高的 SKU；不使用 NL2SQL。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date_range_days": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 30,
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 5,
                    },
                    "category": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    ),
    "get_aftersale_workflow": ToolSpec(
        server_id="mcp-aftersale",
        degrade_level=DegradeLevel.L1_NON_CRITICAL,
        retry_on_timeout=True,
        empty_result_is_success=True,
        endpoint_env="SUNING_MCP_AFTERSALE_URL",
        default_endpoint="http://127.0.0.1:8102/mcp",
        schema={
            "name": "get_aftersale_workflow",
            "description": "根据退单 ID 或订单 ID 查询售后工单流转记录；两个标识必须且只能提供一个。",
            "parameters": {
                "type": "object",
                "properties": {
                    "return_id": {"type": "integer", "minimum": 1},
                    "order_id": {"type": "integer", "minimum": 1},
                },
                "oneOf": [
                    {"required": ["return_id"]},
                    {"required": ["order_id"]},
                ],
                "additionalProperties": False,
            },
        },
    ),
    "get_product_info": ToolSpec(
        server_id="mcp-product",
        degrade_level=DegradeLevel.L1_NON_CRITICAL,
        retry_on_timeout=True,
        empty_result_is_success=False,
        endpoint_env="SUNING_MCP_PRODUCT_URL",
        default_endpoint="http://127.0.0.1:8103/mcp",
        schema={
            "name": "get_product_info",
            "description": "根据 SKU 编码查询商品、品牌和品类信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sku_code": {"type": "string", "minLength": 1},
                },
                "required": ["sku_code"],
                "additionalProperties": False,
            },
        },
    ),
    "query_logistics": ToolSpec(
        server_id="mcp-logistics",
        degrade_level=DegradeLevel.L1_NON_CRITICAL,
        retry_on_timeout=True,
        empty_result_is_success=True,
        endpoint_env="SUNING_MCP_LOGISTICS_URL",
        default_endpoint="http://127.0.0.1:8104/mcp",
        schema={
            "name": "query_logistics",
            "description": "根据退单 ID 或订单 ID 查询物流轨迹；两个标识必须且只能提供一个。",
            "parameters": {
                "type": "object",
                "properties": {
                    "return_id": {"type": "integer", "minimum": 1},
                    "order_id": {"type": "integer", "minimum": 1},
                },
                "oneOf": [
                    {"required": ["return_id"]},
                    {"required": ["order_id"]},
                ],
                "additionalProperties": False,
            },
        },
    ),
    "get_refund_status": ToolSpec(
        server_id="mcp-payment",
        degrade_level=DegradeLevel.L2_CORE,
        retry_on_timeout=True,
        empty_result_is_success=False,
        endpoint_env="SUNING_MCP_PAYMENT_URL",
        default_endpoint="http://127.0.0.1:8105/mcp",
        schema={
            "name": "get_refund_status",
            "description": "根据退单 ID 或订单 ID 查询退款状态；两个标识必须且只能提供一个。",
            "parameters": {
                "type": "object",
                "properties": {
                    "return_id": {"type": "integer", "minimum": 1},
                    "order_id": {"type": "integer", "minimum": 1},
                },
                "oneOf": [
                    {"required": ["return_id"]},
                    {"required": ["order_id"]},
                ],
                "additionalProperties": False,
            },
        },
    ),
    "trace_order_timeline": ToolSpec(
        server_id="mcp-order-timeline",
        degrade_level=DegradeLevel.L2_CORE,
        retry_on_timeout=True,
        empty_result_is_success=False,
        endpoint_env="SUNING_MCP_TIMELINE_URL",
        default_endpoint="http://127.0.0.1:8106/mcp",
        schema={
            "name": "trace_order_timeline",
            "description": (
                "根据订单 ID 并行聚合订单、工单、物流和退款，"
                "返回按 return_id 隔离的售后时间线、SLA 状态、当前卡点和失败来源。"
                "没有对应节点且 source_failures 未报错时，只能说“本次授权查询未返回记录”；"
                "current_bottleneck.source=derived 时必须说明它是推导阶段而非实际业务记录。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "integer", "minimum": 1},
                },
                "required": ["order_id"],
                "additionalProperties": False,
            },
        },
    ),
}


__all__ = ["TOOL_SPECS", "ToolSpec"]
