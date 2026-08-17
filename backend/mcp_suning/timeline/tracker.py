"""聚合订单、售后、物流和支付 MCP 结果，生成标准化售后时间线。"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
SLA_WARNING_RATIO = 0.8
RETURN_STATUS_LABELS = {
    "0": "待审核",
    "1": "已通过",
    "2": "已驳回",
    "3": "已完成",
}
TERMINAL_RETURN_STATUSES = {"2", "3", "rejected", "completed", "closed"}


@dataclass(frozen=True)
class SLAConfig:
    """描述一个售后服务类型对应的阶段时效阈值。"""

    apply_to_audit: float = 24.0
    audit_to_pickup: float = 48.0
    pickup_to_warehouse: float = 72.0
    warehouse_to_quality: float = 48.0
    quality_to_refund: float = 24.0
    total_turnaround: float = 168.0


DEFAULT_SLA_CONFIG = SLAConfig()
SLA_OVERRIDES: dict[tuple[str, str], SLAConfig] = {
    ("repair", "C1-AC"): replace(DEFAULT_SLA_CONFIG, audit_to_pickup=2.0),
}


@dataclass
class TimelineNode:
    """表示已标准化的单个订单售后时间线节点。"""

    timestamp: datetime | None
    source: str
    status: str
    description: str
    sla_status: str = "normal"
    duration_hours: float | None = None

    # 以下字段只用于内部关联、SLA 与待办判断，不会出现在 MCP 返回中。
    return_id: int | None = None
    phase: str = "other"
    step_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        """输入：当前标准化时间线节点的字段状态。

        输出：可作为 MCP 结构化结果返回的节点字典，时间为空时保留 ``None``。
        功能：把内部 ``datetime``、SLA 和间隔转换为参考方案使用的精简 JSON 结构。
        """

        return {
            "time": self.timestamp.isoformat() if self.timestamp else None,
            "source": self.source,
            "return_id": self.return_id,
            "status": self.status,
            "description": self.description,
            "sla_status": self.sla_status,
            "duration_hours": self.duration_hours,
        }


@dataclass(frozen=True)
class SourceFailure:
    """记录一个下游 MCP 数据源未能提供结果的原因。"""

    source: str
    message: str

    def to_dict(self) -> dict[str, str]:
        """输入：当前失败来源的名称和错误信息。

        输出：包含 ``source`` 与 ``message`` 的可序列化字典。
        功能：为 Agent 和调用方明确标记部分失败，而不伪装成没有业务数据。
        """

        return {"source": self.source, "message": self.message}


@dataclass(frozen=True)
class OrderTrace:
    """封装一次跨系统订单追踪的聚合结果。"""

    order_id: int
    timeline: tuple[TimelineNode, ...]
    failures: tuple[SourceFailure, ...] = ()
    current_bottleneck: TimelineNode | None = None
    total_duration_hours: float | None = None
    total_sla_status: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        """输入：当前订单追踪的时间线、失败来源、卡点、SLA、退单关联和来源状态。

        输出：供 FastMCP 返回的完整结构化订单全链路字典。
        功能：以参考方案的精简结构暴露时间线、卡点、总 SLA 和部分失败，不返回内部计算中间值。
        """

        return {
            "success": bool(self.timeline),
            "order_id": self.order_id,
            "partial": bool(self.failures),
            "source_failures": [failure.to_dict() for failure in self.failures],
            "timeline": [node.to_dict() for node in self.timeline],
            "current_bottleneck": (
                self.current_bottleneck.to_dict()
                if self.current_bottleneck is not None
                else None
            ),
            "total_duration_hours": self.total_duration_hours,
            "total_sla_status": self.total_sla_status,
        }


SourceFetcher = Callable[[int], Awaitable[Any]]


def utc_now() -> datetime:
    """输入：无；隐式读取当前系统时钟。

    输出：带 UTC 时区的当前时间。
    功能：为卡点滞留和总 SLA 计算提供可在测试中替换的统一时钟入口。
    """

    return datetime.now(timezone.utc)


def _text(value: Any) -> str:
    """输入：来自下游 MCP 的任意字段值 ``value``。

    输出：去除首尾空白后的字符串；空值返回空字符串。
    功能：统一状态、描述和时间字段的空值处理，避免 ``None`` 被渲染为业务文本。
    """

    return "" if value is None else str(value).strip()


def _first_value(values: Mapping[str, Any], *keys: str) -> Any:
    """输入：一条下游数据映射和按优先级排列的候选字段名。

    输出：第一个非空字段值；全部为空时返回 ``None``。
    功能：兼容各 MCP 对同一业务概念使用不同字段名称的异构返回格式。
    """

    for key in keys:
        value = values.get(key)
        if value not in (None, ""):
            return value
    return None


def _return_id(value: Any) -> int | None:
    """输入：下游记录中的候选退单标识 ``value``。

    输出：大于零的整数退单 ID；空值、非整数或非正数返回 ``None``。
    功能：统一关联订单、工单、物流和退款节点，防止多个退单共用订单号时被错误合并。
    """

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _return_status_label(value: Any) -> str:
    """输入：退单来源返回的状态码或状态文本 ``value``。

    输出：已登记状态码的中文标签；未知值返回带原值的未知状态标签。
    功能：把状态码字典转换为可读证据，而不把数字状态码误表达为未经来源确认的工单节点。
    """

    normalized = _text(value).lower()
    return RETURN_STATUS_LABELS.get(normalized, f"未知状态({normalized or '空'})")


def parse_timestamp(value: Any) -> datetime | None:
    """输入：Unix 秒或毫秒、ISO 8601 文本、MySQL 时间文本或空时间 ``value``。

    输出：统一转换为上海时区 ``datetime``；无法解析或空值时返回 ``None``。
    功能：消除订单、工单、物流和支付系统的时间格式差异，避免伪造当前时间污染排序。
    """

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if not math.isfinite(numeric):
            return None
        if abs(numeric) >= 100_000_000_000:
            numeric /= 1000
        try:
            return datetime.fromtimestamp(numeric, timezone.utc).astimezone(SHANGHAI_TZ)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None

    normalized = value.strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        for format_text in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(normalized, format_text)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(SHANGHAI_TZ)


def select_sla_config(return_type: str, category_code: str) -> SLAConfig:
    """输入：退单服务类型 ``return_type`` 和 SKU 品类编码 ``category_code``。

    输出：最匹配的阶段 SLA 配置；未配置专项规则时返回默认配置。
    功能：按服务类型和品类优先选择时效阈值，例如空调维修采用两小时响应规则。
    """

    normalized_type = _text(return_type).lower()
    normalized_category = _text(category_code).upper()
    for (service_type, category_prefix), config in SLA_OVERRIDES.items():
        if normalized_type == service_type and normalized_category.startswith(category_prefix):
            return config
    return DEFAULT_SLA_CONFIG


def _service_description(context: Mapping[str, Any]) -> str:
    """输入：订单 MCP 返回的一条退单上下文 ``context``。

    输出：服务类型、品类和审核后响应 SLA 组成的简短说明；字段缺失时省略对应片段。
    功能：将回答维修类问题需要的少量规则依据放入申请节点说明，避免增加独立的退单摘要 Schema。
    """

    service_type = _text(context.get("return_type"))
    category = _text(context.get("category_name")) or _text(context.get("category_code"))
    config = select_sla_config(service_type, _text(context.get("category_code")))
    details = [
        f"服务类型：{service_type}" if service_type else "",
        f"品类：{category}" if category else "",
        f"审核后响应 SLA：{config.audit_to_pickup:g}小时" if service_type else "",
    ]
    return "；".join(item for item in details if item)


def _phase_for_status(status: str, source: str) -> str:
    """输入：节点状态文本 ``status`` 和标准化数据源 ``source``。

    输出：申请、审核、取件、入库、质检、退款或其他阶段标识。
    功能：把不同系统的自然语言节点名映射到同一套 SLA 阶段语义。
    """

    normalized = _text(status)
    if "申请" in normalized:
        return "apply"
    if "审核" in normalized:
        return "audit"
    if "取件" in normalized or "pickup" in normalized.lower():
        return "pickup"
    if "质检" in normalized or "检测" in normalized:
        return "quality"
    if (
        "签收" in normalized
        or "入库" in normalized
        or "arrive" in normalized.lower()
        or (source == "logistics" and "到达" in normalized)
    ):
        return "warehouse"
    if "退款" in normalized or source == "payment":
        return "refund"
    return "other"


def _source_for_workflow(step_name: str) -> str:
    """输入：售后工单返回的步骤名称 ``step_name``。

    输出：``warehouse`` 或 ``aftersale`` 标准来源名称。
    功能：在没有独立仓储 MCP 的当前部署中，将工单中的仓库和质检步骤显式标记为仓储节点。
    """

    return "warehouse" if any(word in step_name for word in ("仓库", "质检", "检测")) else "aftersale"


def _source_priority(source: str) -> int:
    """输入：标准化时间线数据源 ``source``。

    输出：同一时刻、同一阶段去重时使用的可信度优先级整数。
    功能：优先采用支付、物流和仓储等事实系统记录，避免工单镜像节点重复展示。
    """

    return {"order": 0, "aftersale": 1, "warehouse": 2, "logistics": 3, "payment": 4}.get(source, 0)


def _node_sort_key(node: TimelineNode) -> tuple[bool, datetime, int, int, str]:
    """输入：一个含可选时间戳的标准化时间线节点。

    输出：可用于稳定排序的“未知时间、时间、退单 ID、来源优先级、状态”元组。
    功能：让有时间的业务事实按发生顺序排列，并在同一时刻稳定区分不同退单；时间缺失的待处理节点固定放在末尾。
    """

    latest = datetime.max.replace(tzinfo=SHANGHAI_TZ)
    return (
        node.timestamp is None,
        node.timestamp or latest,
        node.return_id or 0,
        _source_priority(node.source),
        node.status,
    )


def _deduplicate(nodes: list[TimelineNode]) -> list[TimelineNode]:
    """输入：未排序且可能包含跨系统镜像记录的时间线节点列表。

    输出：按阶段和时间去除重复后、仍保留不同业务事实的节点列表。
    功能：在同一退单内消除工单与物流或支付系统的镜像记录，保留不同退单即使同一时刻处于相同阶段的独立事实。
    """

    selected: dict[tuple[int | None, str, datetime | None], TimelineNode] = {}
    for node in nodes:
        key = (node.return_id, node.phase, node.timestamp)
        previous = selected.get(key)
        if previous is None or _source_priority(node.source) >= _source_priority(previous.source):
            selected[key] = node
    return sorted(selected.values(), key=_node_sort_key)


def _status_rank(status: str) -> int:
    """输入：节点或总流程的 SLA 状态文本 ``status``。

    输出：正常、预警、超时和未知状态对应的严重等级整数。
    功能：为整条订单链路从所有节点中归并出最高严重度的 SLA 状态。
    """

    return {"normal": 0, "warning": 1, "overrun": 2, "unknown": -1}.get(status, -1)


def _sla_status(elapsed_hours: float | None, limit_hours: float | None) -> str:
    """输入：实际耗时 ``elapsed_hours`` 和该阶段允许耗时 ``limit_hours``。

    输出：``normal``、``warning``、``overrun`` 或 ``unknown``；缺少任一值时返回 ``unknown``。
    功能：以 80% 阈值生成阶段预警，并在缺少前置时间等计算依据时明确标记为无法计算。
    """

    if elapsed_hours is None or limit_hours is None:
        return "unknown"
    if elapsed_hours > limit_hours:
        return "overrun"
    if elapsed_hours >= limit_hours * SLA_WARNING_RATIO:
        return "warning"
    return "normal"


def _limit_for_phase(phase: str, config: SLAConfig) -> tuple[str, float] | None:
    """输入：当前节点阶段 ``phase`` 和已选定的 SLA 配置。

    输出：``(前置阶段, 允许小时数)``，申请或未知阶段没有前置 SLA 时返回 ``None``。
    功能：定义每个实际完成节点应从哪个前置节点开始计算时效。
    """

    limits = {
        "audit": ("apply", config.apply_to_audit),
        "pickup": ("audit", config.audit_to_pickup),
        "warehouse": ("pickup", config.pickup_to_warehouse),
        "quality": ("warehouse", config.warehouse_to_quality),
        "refund": ("quality", config.quality_to_refund),
    }
    return limits.get(phase)


def _next_limit_for_current(phase: str, config: SLAConfig) -> tuple[str, float] | None:
    """输入：当前卡点已达到的阶段 ``phase`` 和适用 SLA 配置。

    输出：``(下一阶段, 允许小时数)``；退款已完成或未知阶段返回 ``None``。
    功能：在没有明确待处理节点时，从最后一个完成节点计算下一阶段的等待时长和卡点 SLA。
    """

    limits = {
        "apply": ("audit", config.apply_to_audit),
        "audit": ("pickup", config.audit_to_pickup),
        "pickup": ("warehouse", config.pickup_to_warehouse),
        "warehouse": ("quality", config.warehouse_to_quality),
        "quality": ("refund", config.quality_to_refund),
    }
    return limits.get(phase)


def _next_stage_label(phase: str) -> str:
    """输入：最后一条可确认节点的标准流程阶段 ``phase``。

    输出：面向用户的下一待推进阶段名称；未知阶段返回“后续处理”。
    功能：为没有显式 pending 记录的链路生成保守的待推进提示，不把推导阶段伪装成来源系统事实。
    """

    return {
        "apply": "审核响应",
        "audit": "审核后响应",
        "pickup": "仓库签收",
        "warehouse": "质检完成",
        "quality": "退款处理",
    }.get(phase, "后续处理")


def _derived_current_stage(
    node: TimelineNode,
    *,
    elapsed_hours: float | None,
    limit_hours: float | None,
) -> TimelineNode:
    """输入：最后一条已确认节点 ``node``，以及可选的等待时长和 SLA 阈值。

    输出：``source="derived"`` 的当前待推进节点。
    功能：把没有显式 pending 记录时的阶段推导集中构造，明确其证据边界，避免覆盖或伪装最后一条真实业务记录。
    """

    if limit_hours is None:
        status = "等待后续处理"
        description = (
            f"依据最后确认节点“{node.status}”推导；"
            "缺少可匹配的阶段 SLA，无法确认具体待推进环节。"
        )
    else:
        status = f"等待{_next_stage_label(node.phase)}"
        description = (
            f"依据最后确认节点“{node.status}”推导；"
            "本次授权查询未返回后续记录，推导结果不代表业务未发生。"
        )
    if elapsed_hours is not None:
        description += f" [当前卡点，已滞留{elapsed_hours}小时]"
    return TimelineNode(
        timestamp=node.timestamp,
        source="derived",
        return_id=node.return_id,
        status=status,
        description=description,
        phase=node.phase,
        step_status="derived",
        sla_status=_sla_status(elapsed_hours, limit_hours),
    )


def _is_pending(node: TimelineNode) -> bool:
    """输入：已标准化的时间线节点及其步骤状态、时间字段。

    输出：节点缺少发生时间或状态表示待处理时返回 ``True``。
    功能：从工单和退款系统识别显式的当前待办节点，优先用于定位卡点。
    """

    return node.timestamp is None or _text(node.step_status).lower() in {"pending", "doing", "processing", "created"}


def _is_successful_refund(node: TimelineNode) -> bool:
    """输入：一个标准化时间线节点。

    输出：该节点代表已成功退款时返回 ``True``。
    功能：区分已结束的退款链路和仍需标记当前卡点的退款处理中状态。
    """

    return node.phase == "refund" and _text(node.step_status).lower() in {"success", "done", "completed"}


class OrderTimelineTracker:
    """通过并行数据源调用和内存聚合构建订单售后全链路。"""

    def __init__(
        self,
        fetchers: Mapping[str, SourceFetcher],
        *,
        timeout_seconds: float = 10.0,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """输入：按来源注册的异步获取函数、单次整体超时秒数和可替换时钟。

        输出：可执行订单全链路追踪的引擎实例。
        功能：保存四个 MCP 调用入口并规范超时下限，供每轮追踪独立并发和 SLA 计算使用。
        """

        self.fetchers = dict(fetchers)
        self.timeout_seconds = max(float(timeout_seconds), 0.1)
        self.clock = clock

    async def _safe_call(self, awaitable: Awaitable[Any]) -> tuple[Any, str]:
        """输入：已创建的异步 MCP 调用 ``awaitable``。

        输出：``(负载, 错误文本)`` 元组；取消信号继续向上抛出。
        功能：把单个 MCP 异常转换为可直接归档的部分失败，而不影响其他来源完成。
        """

        try:
            return await awaitable, ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return None, str(exc) or type(exc).__name__

    async def trace(self, order_id: int) -> OrderTrace:
        """输入：已校验的正整数订单 ID ``order_id``。

        输出：包含标准化节点、部分失败、卡点和 SLA 的 ``OrderTrace``。
        功能：并发调用订单、工单、物流和支付来源，在统一超时后发布事件并聚合已有结果。
        """

        tasks = {
            source: asyncio.create_task(self._safe_call(fetcher(order_id)))
            for source, fetcher in self.fetchers.items()
        }
        done, pending = await asyncio.wait(tasks.values(), timeout=self.timeout_seconds)
        payloads: dict[str, Any] = {}
        failures: list[SourceFailure] = []
        for source, task in tasks.items():
            if task in done:
                payload, error = task.result()
                if error:
                    failures.append(SourceFailure(source, error))
                else:
                    payloads[source] = payload
            elif task in pending:
                task.cancel()
                failures.append(SourceFailure(source, "调用超时"))
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return self._merge_timeline(order_id, payloads, failures)

    def _merge_timeline(
        self,
        order_id: int,
        payloads: Mapping[str, Any],
        failures: list[SourceFailure],
    ) -> OrderTrace:
        """输入：订单 ID ``order_id``、按来源归档的成功负载和下游失败列表。

        输出：按标准时间、SLA 和当前卡点补全的 ``OrderTrace``。
        功能：把异构 MCP 负载按 ``return_id`` 关联为统一节点，保留来源计数和解释边界，并在失败时保留部分可用结果。
        """

        order_payload = self._payload_mapping(payloads.get("order"), "order", failures)
        aftersale_payload = self._payload_list(payloads.get("aftersale"), "aftersale", failures)
        logistics_payload = self._payload_mapping(payloads.get("logistics"), "logistics", failures)
        payment_payload = self._payload_mapping(payloads.get("payment"), "payment", failures)

        nodes: list[TimelineNode] = []
        order = order_payload.get("order", {}) if order_payload else {}
        return_contexts = self._return_contexts(order_payload, order)
        contexts_by_id = {
            _return_id(context.get("return_id")): context
            for context in return_contexts
            if _return_id(context.get("return_id")) is not None
        }
        if isinstance(order, Mapping):
            order_timestamp = parse_timestamp(
                _first_value(order, "create_time", "create_time_text")
            )
            order_status = _text(order.get("order_status")) or "订单创建"
            if order_timestamp is not None:
                nodes.append(
                    TimelineNode(
                        timestamp=order_timestamp,
                        source="order",
                        status="订单创建",
                        description=f"订单状态：{order_status}",
                    )
                )

        logistics_traces = self._payload_list(
            logistics_payload.get("traces") if logistics_payload else None,
            "logistics",
            failures,
        )
        logistics_return_ids = {
            _return_id(trace.get("return_id"))
            for trace in logistics_traces
        }
        logistics_return_ids.discard(None)
        workflow_phases: dict[int | None, set[str]] = {}
        for step in aftersale_payload:
            workflow_return_id = _return_id(step.get("return_id"))
            workflow_phases.setdefault(workflow_return_id, set()).add(
                _phase_for_status(
                    _text(_first_value(step, "step_name", "status")),
                    "aftersale",
                )
            )
        for return_context in return_contexts:
            return_id = _return_id(return_context.get("return_id"))
            phases = workflow_phases.get(return_id, set())
            apply_time = parse_timestamp(
                _first_value(return_context, "return_create_time", "return_create_time_text")
            )
            return_status = _text(return_context.get("return_status"))
            return_status_label = _text(return_context.get("return_status_label")) or _return_status_label(return_status)
            if apply_time is not None and "apply" not in phases:
                nodes.append(
                    TimelineNode(
                        timestamp=apply_time,
                        source="order",
                        return_id=return_id,
                        status="用户提交售后申请",
                        description="；".join(
                            item
                            for item in (
                                f"原因：{_text(return_context.get('return_reason_desc')) or '未提供'}",
                                _service_description(return_context),
                            )
                            if item
                        ),
                        phase="apply",
                        step_status=return_status,
                    )
                )
            update_time = parse_timestamp(
                _first_value(return_context, "return_update_time", "return_update_time_text")
            )
            if update_time is not None and "audit" not in phases and return_status in {"1", "approved", "done"}:
                nodes.append(
                    TimelineNode(
                        timestamp=update_time,
                        source="order",
                        return_id=return_id,
                        status="退单状态：已通过",
                        description="由退单状态码映射；未返回独立审核工单节点",
                        phase="audit",
                        step_status=return_status,
                    )
                )
            elif update_time is not None and return_status in TERMINAL_RETURN_STATUSES:
                nodes.append(
                    TimelineNode(
                        timestamp=update_time,
                        source="order",
                        return_id=return_id,
                        status=f"退单状态：{return_status_label}",
                        description="该状态来自退单表，不能单独推断退款或具体工单处理环节",
                        step_status=return_status,
                    )
                )
        for step in aftersale_payload:
            raw_step_name = _text(_first_value(step, "step_name", "status"))
            raw_description = _text(_first_value(step, "remark", "description"))
            step_name = raw_step_name or "未命名售后工单节点"
            phase = _phase_for_status(step_name, "aftersale")
            step_return_id = _return_id(step.get("return_id"))
            if step_return_id in logistics_return_ids and phase in {"pickup", "warehouse"}:
                continue
            description = raw_description
            if not raw_step_name:
                description = (
                    "源工单未提供步骤名称，无法识别具体业务环节"
                    + (f"；备注：{raw_description}" if raw_description else "")
                )
            elif phase == "apply":
                description = "；".join(
                    item
                    for item in (
                        raw_description,
                        _service_description(contexts_by_id.get(step_return_id, {})),
                    )
                    if item
                )
            nodes.append(
                TimelineNode(
                    timestamp=parse_timestamp(_first_value(step, "step_time", "step_time_text", "time")),
                    source=_source_for_workflow(step_name),
                    return_id=step_return_id,
                    status=step_name,
                    description=description,
                    phase=phase,
                    step_status=_text(_first_value(step, "step_status", "status")),
                )
            )

        for trace in logistics_traces:
            status = _text(_first_value(trace, "node_desc", "status_desc", "node_type")) or "物流节点"
            nodes.append(
                TimelineNode(
                    timestamp=parse_timestamp(_first_value(trace, "node_time", "node_time_text", "time")),
                    source="logistics",
                    return_id=_return_id(trace.get("return_id")),
                    status=status,
                    description=_text(_first_value(trace, "operator_name", "operator")),
                    phase=_phase_for_status(status, "logistics"),
                    step_status=_text(trace.get("node_type")),
                )
            )

        refunds = self._payload_list(
            payment_payload.get("refunds") if payment_payload else None,
            "payment",
            failures,
        )
        if not refunds and payment_payload:
            refund = payment_payload.get("refund")
            if isinstance(refund, Mapping):
                refunds = [refund]
        for refund in refunds:
            amount = _first_value(refund, "refund_amount_yuan", "refund_amount")
            amount_text = "" if amount is None else f" ¥{amount}"
            refund_state = _text(refund.get("refund_status")) or "pending"
            nodes.append(
                TimelineNode(
                    timestamp=parse_timestamp(_first_value(refund, "refund_time", "refund_time_text")),
                    source="payment",
                    return_id=_return_id(refund.get("return_id")),
                    status=f"退款{amount_text}".strip(),
                    description=f"退款方式：{_text(refund.get('refund_method')) or '未提供'}",
                    phase="refund",
                    step_status=refund_state,
                )
            )

        nodes = _deduplicate(nodes)
        primary_return = return_contexts[0] if return_contexts else order
        config = select_sla_config(
            _text(primary_return.get("return_type")) if isinstance(primary_return, Mapping) else "",
            _text(primary_return.get("category_code")) if isinstance(primary_return, Mapping) else "",
        )
        terminal_return_ids = {
            _return_id(return_context.get("return_id"))
            for return_context in return_contexts
            if _text(return_context.get("return_status")).lower() in TERMINAL_RETURN_STATUSES
        }
        terminal_return_ids.discard(None)
        terminal_return_ids.update(
            node.return_id for node in nodes if _is_successful_refund(node) and node.return_id is not None
        )
        annotated, current, total_hours, total_status = self._annotate_sla(
            nodes,
            config,
            known_return_ids={
                return_id
                for return_id in (_return_id(item.get("return_id")) for item in return_contexts)
                if return_id is not None
            },
            terminal_return_ids=terminal_return_ids, # type: ignore
        )
        return OrderTrace(
            order_id=order_id,
            timeline=tuple(annotated),
            failures=tuple(failures),
            current_bottleneck=current,
            total_duration_hours=total_hours,
            total_sla_status=total_status,
        )

    @staticmethod
    def _return_contexts(
        order_payload: Mapping[str, Any],
        order: Any,
    ) -> list[Mapping[str, Any]]:
        """输入：订单 MCP 负载 ``order_payload`` 和其中的订单对象 ``order``。

        输出：按退单创建时间稳定排列的授权退单上下文列表。
        功能：兼容新版 ``returns`` 数组和旧版单退单订单对象，让时间线可把同一订单下的多个退单分开关联。
        """

        raw_returns = order_payload.get("returns") if order_payload else None
        contexts = [
            item
            for item in raw_returns
            if isinstance(item, Mapping)
        ] if isinstance(raw_returns, list) else []
        if not contexts and isinstance(order, Mapping) and _return_id(order.get("return_id")):
            contexts = [order]
        return sorted(
            contexts,
            key=lambda item: (
                parse_timestamp(
                    _first_value(item, "return_create_time", "return_create_time_text")
                ) or datetime.max.replace(tzinfo=SHANGHAI_TZ),
                _return_id(item.get("return_id")) or 0,
            ),
        )

    @staticmethod
    def _payload_mapping(
        payload: Any,
        source: str,
        failures: list[SourceFailure],
    ) -> Mapping[str, Any]:
        """输入：单个来源负载、来源名称和待追加的失败列表。

        输出：可读取的映射负载；来源失败、空值或格式错误时返回空映射。
        功能：统一识别下游 MCP 的 ``success=false`` 与无效响应，避免聚合器把错误对象当业务节点。
        """

        if payload is None:
            return {}
        if not isinstance(payload, Mapping):
            failures.append(SourceFailure(source, "返回格式不是对象"))
            return {}
        if payload.get("success") is False:
            failures.append(SourceFailure(source, _text(payload.get("error")) or "未返回可访问数据"))
            return {}
        return payload

    @staticmethod
    def _payload_list(
        payload: Any,
        source: str,
        failures: list[SourceFailure],
    ) -> list[Mapping[str, Any]]:
        """输入：列表负载、来源名称和待追加的失败列表。

        输出：只包含映射项的下游列表；空列表合法，格式错误时返回空列表。
        功能：兼容售后和物流节点列表，忽略异常元素并以来源失败显式暴露不兼容响应。
        """

        if payload is None:
            return []
        if not isinstance(payload, list):
            failures.append(SourceFailure(source, "返回格式不是列表"))
            return []
        invalid_count = sum(1 for item in payload if not isinstance(item, Mapping))
        if invalid_count:
            failures.append(SourceFailure(source, f"忽略 {invalid_count} 条格式错误节点"))
        return [item for item in payload if isinstance(item, Mapping)]

    def _annotate_sla(
        self,
        nodes: list[TimelineNode],
        config: SLAConfig,
        *,
        known_return_ids: set[int],
        terminal_return_ids: set[int],
    ) -> tuple[list[TimelineNode], TimelineNode | None, float | None, str]:
        """输入：已排序时间线节点、当前订单适用的 ``SLAConfig``、已知退单 ID 和终态退单 ID。

        输出：带阶段耗时与 SLA 标记的节点、当前卡点、总时长及总 SLA 状态。
        功能：按退单隔离阶段锚点和卡点判断，避免多个退单共享订单号时互相覆盖 SLA 或把已终态退单误标为当前卡点。
        """

        if not nodes:
            return [], None, None, "unknown"
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(SHANGHAI_TZ)
        annotated: list[TimelineNode] = []
        phase_times: dict[int | None, dict[str, datetime]] = {}
        previous_timestamp: datetime | None = None
        for node in nodes:
            duration_hours = None
            if node.timestamp is not None and previous_timestamp is not None:
                duration_hours = round(
                    max(0.0, (node.timestamp - previous_timestamp).total_seconds() / 3600),
                    1,
                )
            if node.timestamp is not None:
                previous_timestamp = node.timestamp
            phase_limit = _limit_for_phase(node.phase, config)
            elapsed_hours = None
            limit_hours = None
            sla_status = "normal"
            if phase_limit is not None:
                anchor_phase, limit_hours = phase_limit
                anchor_time = phase_times.get(node.return_id, {}).get(anchor_phase)
                effective_time = node.timestamp or now
                if anchor_time is not None:
                    elapsed_hours = round(
                        max(0.0, (effective_time - anchor_time).total_seconds() / 3600),
                        1,
                    )
                    sla_status = _sla_status(elapsed_hours, limit_hours)
                else:
                    sla_status = "unknown"
            annotated_node = replace(
                node,
                duration_hours=duration_hours,
                sla_status=sla_status,
            )
            annotated.append(annotated_node)
            if node.timestamp is not None and node.phase in {"apply", "audit", "pickup", "warehouse", "quality", "refund"}:
                phase_times.setdefault(node.return_id, {})[node.phase] = node.timestamp

        unresolved_return_ids = known_return_ids - terminal_return_ids
        if known_return_ids:
            candidate_nodes = [
                node for node in annotated if node.return_id in unresolved_return_ids
            ]
        else:
            candidate_nodes = annotated
        pending_nodes = [node for node in candidate_nodes if _is_pending(node)]
        current = pending_nodes[-1] if pending_nodes else (candidate_nodes[-1] if candidate_nodes else None)
        if current is not None:
            is_pending = _is_pending(current)
            current_index = annotated.index(current)
            pending_phase_limit = (
                _limit_for_phase(current.phase, config)
                if is_pending
                else _next_limit_for_current(current.phase, config)
            )
            limit_hours = None
            wait_hours = None
            if pending_phase_limit is not None:
                anchor_phase, limit_hours = pending_phase_limit
                anchor_time = phase_times.get(current.return_id, {}).get(anchor_phase)
                if is_pending and current.phase == anchor_phase:
                    anchor_time = current.timestamp
                if anchor_time is None and not is_pending:
                    anchor_time = current.timestamp
                if anchor_time is not None:
                    wait_hours = round(
                        max(0.0, (now - anchor_time).total_seconds() / 3600),
                        1,
                    )
            if is_pending:
                current = replace(
                    current,
                    sla_status=_sla_status(wait_hours, limit_hours)
                    if pending_phase_limit is not None
                    else current.sla_status,
                    description=(
                        f"{current.description} [当前卡点，已滞留{wait_hours}小时]"
                        if wait_hours is not None
                        else current.description
                    ).strip(),
                )
                annotated[current_index] = current
            else:
                current = _derived_current_stage(
                    current,
                    elapsed_hours=wait_hours,
                    limit_hours=limit_hours,
                )

        total_hours = None
        total_status = "unknown"
        if len(known_return_ids) == 1:
            return_id = next(iter(known_return_ids))
            apply_time = phase_times.get(return_id, {}).get("apply")
            refund_time = phase_times.get(return_id, {}).get("refund")
            if apply_time is not None and (
                return_id not in terminal_return_ids or refund_time is not None
            ):
                terminal_time = refund_time or now
                total_hours = round(
                    max(0.0, (terminal_time - apply_time).total_seconds() / 3600),
                    1,
                )
                total_status = _sla_status(total_hours, config.total_turnaround)
        elif not known_return_ids:
            apply_time = phase_times.get(None, {}).get("apply")
            refund_time = phase_times.get(None, {}).get("refund")
            if apply_time is not None:
                terminal_time = refund_time or now
                total_hours = round(
                    max(0.0, (terminal_time - apply_time).total_seconds() / 3600),
                    1,
                )
                total_status = _sla_status(total_hours, config.total_turnaround)
        if total_hours is None:
            return annotated, current, None, "unknown"
        node_statuses = [node.sla_status for node in annotated]
        if current is not None:
            node_statuses.append(current.sla_status)
        node_statuses.append(total_status)
        highest = max(node_statuses, key=_status_rank, default="unknown")
        return annotated, current, total_hours, highest


__all__ = [
    "DEFAULT_SLA_CONFIG",
    "OrderTimelineTracker",
    "OrderTrace",
    "SLAConfig",
    "SourceFailure",
    "TimelineNode",
    "parse_timestamp",
    "select_sla_config",
]
