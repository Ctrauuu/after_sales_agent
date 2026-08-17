"""验证跨系统订单全链路的并行聚合、降级、时间标准化和 SLA 计算。"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mcp_suning.timeline.tracker import (
    OrderTimelineTracker,
    parse_timestamp,
    select_sla_config,
)
from mcp_suning.timeline.gateway import _result_payload


def _clock() -> datetime:
    """输入：无；测试使用固定业务时刻作为隐式时间来源。

    输出：上海时区的固定当前时间。
    功能：消除真实系统时间对当前卡点滞留和总 SLA 断言的影响。
    """

    return datetime.fromisoformat("2024-06-06T15:00:00+08:00")


async def _order_payload(_order_id: int) -> dict:
    """输入：测试订单 ID ``_order_id``。

    输出：包含订单创建、服务类型和品类的订单 MCP 模拟负载。
    功能：提供全链路聚合选择 SLA 配置和生成首个订单节点所需的固定订单数据。
    """

    return {
        "success": True,
        "order": {
            "create_time": "2024-06-01 09:00:00",
            "order_status": "completed",
            "return_type": "refund",
            "category_code": "C1-AC-WG",
        },
    }


async def _aftersale_payload(_order_id: int) -> list[dict]:
    """输入：测试订单 ID ``_order_id``。

    输出：申请、审核和待质检工单节点的模拟列表。
    功能：构造时间缺失的待处理质检节点，验证聚合器能够用前置仓库事件计算当前卡点滞留。
    """

    return [
        {
            "step_name": "用户提交退货申请",
            "step_status": "done",
            "step_time": "2024-06-01T10:00:00+08:00",
            "remark": "运行噪音大",
        },
        {
            "step_name": "客服审核",
            "step_status": "done",
            "step_time": "2024-06-01T12:00:00+08:00",
            "remark": "审核通过",
        },
        {
            "step_name": "仓库质检",
            "step_status": "pending",
            "step_time": None,
            "remark": "待检测",
        },
    ]


async def _logistics_payload(_order_id: int) -> dict:
    """输入：测试订单 ID ``_order_id``。

    输出：包含取件和仓库到达节点的物流 MCP 模拟负载。
    功能：提供逆向物流事实时间，验证工单中的镜像物流步骤会被去重并作为质检 SLA 的前置锚点。
    """

    return {
        "success": True,
        "traces": [
            {
                "node_type": "pickup",
                "node_desc": "上门取件",
                "operator_name": "刘建国",
                "node_time": "2024-06-02T10:00:00+08:00",
            },
            {
                "node_type": "arrive",
                "node_desc": "到达南京江宁仓",
                "operator_name": "系统",
                "node_time": "2024-06-03T14:00:00+08:00",
            },
        ],
    }


async def _payment_payload(_order_id: int) -> dict:
    """输入：测试订单 ID ``_order_id``。

    输出：没有创建退款记录的支付 MCP 模拟负载。
    功能：让质检待处理节点保持当前卡点，验证未退款并不会被误判为链路完成。
    """

    return {"success": True, "has_refund": False, "refund": None}


@pytest.mark.asyncio
async def test_trace_merges_sources_and_marks_pending_quality_bottleneck() -> None:
    """输入：无；使用四个固定 MCP 负载和固定当前时钟。

    输出：无；断言失败时由 pytest 报告时间线、卡点和 SLA 聚合错误。
    功能：验证订单、工单、物流和支付数据会被合并为统一时间线，将超时质检标记为当前卡点，并保持精简的对外结果 Schema。
    """

    tracker = OrderTimelineTracker(
        {
            "order": _order_payload,
            "aftersale": _aftersale_payload,
            "logistics": _logistics_payload,
            "payment": _payment_payload,
        },
        clock=_clock,
    )

    result = await tracker.trace(202406010001)

    assert not result.failures
    assert [node.phase for node in result.timeline] == [
        "other",
        "apply",
        "audit",
        "pickup",
        "warehouse",
        "quality",
    ]
    assert result.current_bottleneck is not None
    assert result.current_bottleneck.source == "warehouse"
    assert result.current_bottleneck.phase == "quality"
    assert result.current_bottleneck.sla_status == "overrun"
    assert result.current_bottleneck.duration_hours is None
    assert "当前卡点，已滞留73.0小时" in result.current_bottleneck.description
    assert result.total_duration_hours == 125.0
    assert result.total_sla_status == "overrun"
    payload = result.to_dict()
    assert set(payload) == {
        "success",
        "order_id",
        "partial",
        "source_failures",
        "timeline",
        "current_bottleneck",
        "total_duration_hours",
        "total_sla_status",
    }
    assert set(payload["timeline"][0]) == {
        "time",
        "source",
        "return_id",
        "status",
        "description",
        "sla_status",
        "duration_hours",
    }


@pytest.mark.asyncio
async def test_trace_starts_all_sources_and_returns_partial_data_after_timeout() -> None:
    """输入：无；使用三个立即完成来源和一个超过整体超时的异步来源。

    输出：无；断言失败时由 pytest 报告并发启动或部分失败降级行为错误。
    功能：验证四路调用在等待前同时启动，超时来源被取消且成功来源的节点仍会返回。
    """

    started: list[str] = []

    async def quick_order(_order_id: int) -> dict:
        """输入：测试订单 ID ``_order_id``。

        输出：最小订单详情负载。
        功能：记录订单来源已被并发调度，并立即返回可聚合的订单节点。
        """

        started.append("order")
        return await _order_payload(_order_id)

    async def quick_aftersale(_order_id: int) -> list[dict]:
        """输入：测试订单 ID ``_order_id``。

        输出：最小售后节点列表。
        功能：记录售后来源已被并发调度，并立即返回申请节点用于保留部分结果。
        """

        started.append("aftersale")
        return (await _aftersale_payload(_order_id))[:1]

    async def slow_logistics(_order_id: int) -> dict:
        """输入：测试订单 ID ``_order_id``。

        输出：理论上的物流负载；测试超时前不会正常返回。
        功能：记录物流来源已启动后阻塞，用于验证聚合器的整体超时、取消与降级边界。
        """

        started.append("logistics")
        await asyncio.sleep(1)
        return await _logistics_payload(_order_id)

    async def quick_payment(_order_id: int) -> dict:
        """输入：测试订单 ID ``_order_id``。

        输出：最小支付状态负载。
        功能：记录支付来源已被并发调度，并立即返回无退款结果避免终结当前链路。
        """

        started.append("payment")
        return await _payment_payload(_order_id)

    tracker = OrderTimelineTracker(
        {
            "order": quick_order,
            "aftersale": quick_aftersale,
            "logistics": slow_logistics,
            "payment": quick_payment,
        },
        timeout_seconds=0.01,
        clock=_clock,
    )

    result = await tracker.trace(202406010001)

    assert set(started) == {"order", "aftersale", "logistics", "payment"}
    assert result.timeline
    assert len(result.failures) == 1
    assert result.failures[0].source == "logistics"
    assert result.failures[0].message == "调用超时"


def test_parse_timestamp_normalizes_unix_iso_and_mysql_times() -> None:
    """输入：无；使用 Unix 秒、UTC ISO 文本和 MySQL 时间文本三种时间样本。

    输出：无；断言失败时由 pytest 报告跨系统时间标准化错误。
    功能：验证时间解析统一保留上海时区，避免不同来源以本地时区或字符串顺序错误排列。
    """

    expected = datetime.fromisoformat("2024-06-01T14:23:05+08:00")
    assert parse_timestamp(1717222985) == expected
    assert parse_timestamp("2024-06-01T06:23:05Z") == expected
    assert parse_timestamp("2024-06-01 14:23:05") == expected
    assert parse_timestamp(None) is None


def test_select_sla_config_uses_air_conditioner_repair_override() -> None:
    """输入：无；使用空调维修和普通退款两个服务类型、品类组合。

    输出：无；断言失败时由 pytest 报告品类或服务类型 SLA 规则选择错误。
    功能：验证空调维修采用两小时响应阈值，其他未配置组合继续使用默认取件 SLA。
    """

    assert select_sla_config("repair", "C1-AC-WG").audit_to_pickup == 2.0
    assert select_sla_config("refund", "C1-AC-WG").audit_to_pickup == 48.0


def test_result_payload_unwraps_fastmcp_list_result_envelope() -> None:
    """输入：由 FastMCP 为列表结果生成的 ``result`` 结构化内容信封。

    输出：无；断言失败时由 pytest 报告下游工单列表未被正确解包。
    功能：防止聚合器将 ``get_aftersale_workflow`` 的列表结果误判为对象，导致售后数据源被标记为部分失败。
    """

    steps = [{"step_name": "仓库质检", "step_status": "done"}]
    result = SimpleNamespace(structuredContent={"result": steps}, content=[])

    assert _result_payload(result) == steps


def test_result_payload_preserves_normal_business_object() -> None:
    """输入：订单 MCP 返回的正常业务对象结构化内容。

    输出：无；断言失败时由 pytest 报告正常对象被错误解包。
    功能：保证兼容逻辑只处理明确的 FastMCP 单字段信封，不影响订单、物流和支付的原有对象返回。
    """

    payload = {"success": True, "order": {"order_id": 202406010003}}
    result = SimpleNamespace(structuredContent=payload, content=[])

    assert _result_payload(result) == payload


@pytest.mark.asyncio
async def test_trace_keeps_multiple_return_ids_separate_and_avoids_order_wide_sla() -> None:
    """输入：一个订单下两条退单的订单、工单、物流和支付模拟负载。

    输出：无；断言失败时由 pytest 报告跨退单节点混合或订单级 SLA 误算。
    功能：验证聚合器保留每个节点的 ``return_id``，并在多退单场景返回退单摘要而不是把两条链路拼成单一总时长。
    """

    async def order_with_two_returns(_order_id: int) -> dict:
        """输入：测试订单 ID ``_order_id``。

        输出：带两条授权退单上下文的订单详情负载。
        功能：模拟同一订单的首次退货与二次报修，作为多退单隔离的上游事实来源。
        """

        return {
            "success": True,
            "order": {
                "create_time": "2024-06-01 09:00:00",
                "order_status": "completed",
            },
            "returns": [
                {
                    "return_id": 1,
                    "return_type": "refund",
                    "return_status": "1",
                    "return_status_label": "已通过",
                    "return_create_time": "2024-06-02T09:00:00+08:00",
                    "return_update_time": "2024-06-02T10:00:00+08:00",
                    "category_code": "C1-AC-WG",
                },
                {
                    "return_id": 5,
                    "return_type": "refund",
                    "return_status": "1",
                    "return_status_label": "已通过",
                    "return_create_time": "2024-06-03T09:00:00+08:00",
                    "return_update_time": "2024-06-03T10:00:00+08:00",
                    "category_code": "C1-AC-WG",
                },
            ],
        }

    async def workflow_for_first_return(_order_id: int) -> list[dict]:
        """输入：测试订单 ID ``_order_id``。

        输出：仅属于首次退单的待处理质检节点。
        功能：验证卡点会携带真实退单 ID，不能被归因到同订单的另一条退单。
        """

        return [
            {
                "return_id": 1,
                "step_name": "仓库质检",
                "step_status": "pending",
                "step_time": None,
                "remark": "待检测",
            }
        ]

    async def empty_logistics(_order_id: int) -> dict:
        """输入：测试订单 ID ``_order_id``。

        输出：没有逆向物流节点的成功负载。
        功能：排除物流事实对本测试多退单关联断言的干扰。
        """

        return {"success": True, "traces": []}

    async def empty_payment(_order_id: int) -> dict:
        """输入：测试订单 ID ``_order_id``。

        输出：没有退款记录的成功负载。
        功能：让首次退单保持未结束状态，并验证多退单时不生成订单级总 SLA。
        """

        return {"success": True, "refunds": []}

    result = await OrderTimelineTracker(
        {
            "order": order_with_two_returns,
            "aftersale": workflow_for_first_return,
            "logistics": empty_logistics,
            "payment": empty_payment,
        },
        clock=_clock,
    ).trace(202406010001)

    payload = result.to_dict()
    assert result.current_bottleneck is not None
    assert result.current_bottleneck.return_id == 1
    assert payload["total_duration_hours"] is None
    assert payload["total_sla_status"] == "unknown"
    assert {node["return_id"] for node in payload["timeline"] if node["return_id"]} == {1, 5}


@pytest.mark.asyncio
async def test_trace_does_not_mark_terminal_return_status_as_active_bottleneck() -> None:
    """输入：一条状态码为“已完成”、但没有下游节点的退单模拟负载。

    输出：无；断言失败时由 pytest 报告终态退单被误判为当前卡点。
    功能：验证退单状态码 3 被展示为“已完成”的来源事实，同时不被扩展为退款成功或审核停滞结论。
    """

    async def terminal_order(_order_id: int) -> dict:
        """输入：测试订单 ID ``_order_id``。

        输出：带单条已完成退单状态的订单详情负载。
        功能：构造没有工单、物流和退款记录的终态退单边界场景。
        """

        return {
            "success": True,
            "order": {
                "create_time": "2024-06-04T15:45:00+08:00",
                "order_status": "completed",
            },
            "returns": [
                {
                    "return_id": 12,
                    "return_type": "refund",
                    "return_status": "3",
                    "return_status_label": "已完成",
                    "return_create_time": "2024-06-05T13:00:00+08:00",
                    "return_update_time": "2024-06-05T13:00:00+08:00",
                    "category_code": "C1-WM-FRONT",
                }
            ],
        }

    async def no_workflow(_order_id: int) -> list[dict]:
        """输入：测试订单 ID ``_order_id``。

        输出：空工单列表。
        功能：模拟本次授权查询未返回工单记录的成功场景。
        """

        return []

    async def no_logistics(_order_id: int) -> dict:
        """输入：测试订单 ID ``_order_id``。

        输出：空物流轨迹成功负载。
        功能：模拟本次授权查询未返回逆向物流记录的成功场景。
        """

        return {"success": True, "traces": []}

    async def no_payment(_order_id: int) -> dict:
        """输入：测试订单 ID ``_order_id``。

        输出：空退款记录成功负载。
        功能：验证“退单已完成”不会被工具错误改写为“退款成功”。
        """

        return {"success": True, "refunds": []}

    result = await OrderTimelineTracker(
        {
            "order": terminal_order,
            "aftersale": no_workflow,
            "logistics": no_logistics,
            "payment": no_payment,
        },
        clock=_clock,
    ).trace(202406040003)

    payload = result.to_dict()
    assert payload["current_bottleneck"] is None
    assert payload["total_duration_hours"] is None
    assert payload["total_sla_status"] == "unknown"
    assert any(node["status"] == "退单状态：已完成" for node in payload["timeline"])
    terminal_node = next(
        node for node in payload["timeline"] if node["status"] == "退单状态：已完成"
    )
    assert "不能单独推断退款" in terminal_node["description"]


@pytest.mark.asyncio
async def test_trace_marks_unnamed_pending_workflow_as_unidentified() -> None:
    """输入：缺少步骤名称和发生时间的 pending 工单模拟负载。

    输出：无；断言失败时由 pytest 报告模型可误读的占位节点名称或说明缺失。
    功能：验证源数据不完整时工具返回“未命名”及不可确认环节提示，而不是把节点编造成质检或其他具体流程。
    """

    async def order_with_return(_order_id: int) -> dict:
        """输入：测试订单 ID ``_order_id``。

        输出：带一条进行中退单的订单详情负载。
        功能：为无名称 pending 工单提供关联退单上下文。
        """

        return {
            "success": True,
            "order": {"create_time": "2024-06-01T09:00:00+08:00"},
            "returns": [{"return_id": 1, "return_status": "1"}],
        }

    async def unnamed_workflow(_order_id: int) -> list[dict]:
        """输入：测试订单 ID ``_order_id``。

        输出：没有名称和时间的待处理工单记录。
        功能：重现模拟库字段缺失时的聚合边界。
        """

        return [{"return_id": 1, "step_name": "", "step_status": "pending", "step_time": None}]

    async def empty_source(_order_id: int) -> dict:
        """输入：测试订单 ID ``_order_id``。

        输出：空对象型下游成功负载。
        功能：为物流和支付来源提供最小响应，聚焦无名称工单节点的展示语义。
        """

        return {"success": True, "traces": [], "refunds": []}

    result = await OrderTimelineTracker(
        {
            "order": order_with_return,
            "aftersale": unnamed_workflow,
            "logistics": empty_source,
            "payment": empty_source,
        },
        clock=_clock,
    ).trace(202406010001)

    payload = result.to_dict()
    assert payload["current_bottleneck"]["status"] == "未命名售后工单节点"
    assert "无法识别具体业务环节" in payload["current_bottleneck"]["description"]


@pytest.mark.asyncio
async def test_trace_marks_refund_sla_unknown_without_quality_evidence() -> None:
    """输入：缺少质检记录但包含成功退款的一条退单模拟负载。

    输出：无；断言失败时由 pytest 报告退款阶段被错误标记为正常。
    功能：验证阶段 SLA 缺少必要前置时间时明确返回 ``unknown``，而非用默认正常状态掩盖数据缺口。
    """

    order = {
        "success": True,
        "order": {"create_time": "2024-06-01T09:00:00+08:00"},
        "returns": [
            {
                "return_id": 4,
                "return_type": "refund",
                "return_status": "2",
                "return_create_time": "2024-06-03T14:20:00+08:00",
                "return_update_time": "2024-06-04T10:00:00+08:00",
                "category_code": "C1-AC-WG",
            }
        ],
    }
    tracker = OrderTimelineTracker(
        {
            "order": AsyncMock(return_value=order),
            "aftersale": AsyncMock(return_value=[]),
            "logistics": AsyncMock(return_value={"success": True, "traces": []}),
            "payment": AsyncMock(
                return_value={
                    "success": True,
                    "refunds": [
                        {
                            "return_id": 4,
                            "refund_status": "success",
                            "refund_time": "2024-06-05T16:00:00+08:00",
                        }
                    ],
                }
            ),
        },
        clock=_clock,
    )

    result = await tracker.trace(202406020002)

    refund = next(node for node in result.timeline if node.phase == "refund")
    assert refund.sla_status == "unknown"


@pytest.mark.asyncio
async def test_trace_derives_next_stage_and_exposes_sla_rule_basis() -> None:
    """输入：审核已通过、但本次未返回任何后续系统记录的维修退单模拟负载。

    输出：无；断言失败时由 pytest 报告当前阶段被伪装为业务记录或 SLA 规则依据缺失。
    功能：验证聚合器把待推进阶段作为 ``derived`` 结果返回，并把服务类型、品类和适用响应 SLA 写入申请节点说明。
    """

    order = {
        "success": True,
        "order": {"create_time": "2024-06-05T11:20:00+08:00"},
        "returns": [
            {
                "return_id": 13,
                "return_type": "repair",
                "return_status": "1",
                "return_status_label": "已通过",
                "return_create_time": "2024-06-06T08:00:00+08:00",
                "return_update_time": "2024-06-06T08:00:00+08:00",
                "category_code": "C1-AC-WG",
                "category_name": "挂机空调",
            }
        ],
    }
    tracker = OrderTimelineTracker(
        {
            "order": AsyncMock(return_value=order),
            "aftersale": AsyncMock(return_value=[]),
            "logistics": AsyncMock(return_value={"success": True, "traces": []}),
            "payment": AsyncMock(return_value={"success": True, "refunds": []}),
        },
        clock=_clock,
    )

    result = await tracker.trace(202406050001)
    application = next(node for node in result.timeline if node.phase == "apply")

    assert result.current_bottleneck is not None
    assert result.current_bottleneck.source == "derived"
    assert result.current_bottleneck.status == "等待审核后响应"
    assert "不代表业务未发生" in result.current_bottleneck.description
    assert "品类：挂机空调" in application.description
    assert "审核后响应 SLA：2小时" in application.description
