"""三平台售后复杂分析的多 Agent DAG 编排工具。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from tools.registry import tool_error, tool_result  # type: ignore

from .bridge import current_identity
from .harness import AgentHarness


logger = logging.getLogger(__name__)

TASK_TYPES = frozenset(
    {
        "trend_analysis",
        "category_breakdown",
        "reason_drilldown",
        "cost_estimation",
        "chart_gen",
    }
)
DEFAULT_GLOBAL_TIMEOUT_SECONDS = 120
DEFAULT_TASK_TIMEOUT_SECONDS = 30
AGGREGATION_TIMEOUT_SECONDS = 25
MAX_TASKS = 5
MAX_SUMMARY_CHARS = 1800
_TASK_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


COMPLEX_ANALYSIS_SCHEMA: dict[str, Any] = {
    "name": "orchestrate_aftersale_analysis",
    "description": (
        "对售后全量、趋势、品类、原因、成本等复杂分析请求，"
        "以多 Agent DAG 并发查询并聚合；子任务失败时返回可用部分。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "用户的复杂售后分析问题。",
            },
            "date_range_days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 90,
                "default": 30,
                "description": "分析时间窗口，默认近 30 天。",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

class TaskStatus(str, Enum):
    """子任务在 DAG 生命周期中可呈现的状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class SubTask:
    """一个经过校验、可由专长子 Agent 执行的 DAG 节点。"""

    task_id: str
    task_type: str
    description: str
    dependencies: list[str] = field(default_factory=list)
    timeout_seconds: int = DEFAULT_TASK_TIMEOUT_SECONDS
    status: TaskStatus = TaskStatus.PENDING
    summary: str | None = None
    error: str | None = None


@dataclass
class TaskDAG:
    """一次复杂分析的受限有向无环任务图。"""

    root_task: str
    sub_tasks: list[SubTask]
    global_timeout_seconds: int = DEFAULT_GLOBAL_TIMEOUT_SECONDS
    planner: str = "llm"


class TaskDAGValidationError(ValueError):
    """LLM 返回的任务图不符合安全边界或拓扑约束。"""


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """输入：候选值 ``value``、默认值及允许的 ``minimum``、``maximum``。

    输出：位于闭区间内的整数；类型不正确或越界时使用默认值。
    功能：将模型返回和工具参数中的超时、日期窗口收敛为固定资源上限。
    """

    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if minimum <= value <= maximum else default


def _compact_text(value: Any, limit: int = MAX_SUMMARY_CHARS) -> str:
    """输入：任意值 ``value`` 和最大字符数 ``limit``。

    输出：归一化空白且长度受限的文本。
    功能：压缩子 Agent 输出，避免把完整执行轨迹传给后续依赖任务和聚合模型。
    """

    text = " ".join(str(value or "").split())
    return text[:limit]


def _fallback_dag(query: str, date_range_days: int) -> TaskDAG:
    """输入：原始分析问题 ``query`` 与时间窗口 ``date_range_days``。

    输出：包含趋势、品类、成本、原因和图表建议的固定合法 DAG。
    功能：LLM 规划不可用或返回非法 JSON 时保留文档定义的完整分析闭环。
    """

    period = f"最近 {date_range_days} 天"
    return TaskDAG(
        root_task=query,
        planner="fallback",
        sub_tasks=[
            SubTask("trend", "trend_analysis", f"分析{period}退单趋势、峰值和变化。"),
            SubTask("category", "category_breakdown", f"统计{period}退单品类分布并识别最高品类。"),
            SubTask("cost", "cost_estimation", f"估算{period}退单相关金额或成本；没有数据时明确说明。"),
            SubTask(
                "reason",
                "reason_drilldown",
                "基于趋势和品类摘要，对主要问题品类下钻退单原因。",
                ["trend", "category"],
            ),
            SubTask(
                "chart",
                "chart_gen",
                "根据已完成分析列出适合生成的图表、标签和值来源；不发送图片。",
                ["trend", "category", "cost", "reason"],
            ),
        ],
    )


def _parse_dag(payload: Any, query: str, date_range_days: int) -> TaskDAG:
    """输入：LLM 结构化结果 ``payload``、原始问题 ``query`` 和时间窗口。

    输出：拓扑合法且资源受限的 ``TaskDAG``。
    功能：验证任务类型、唯一标识、依赖引用和无环关系，拒绝不可信的模型规划。
    """

    if not isinstance(payload, Mapping):
        raise TaskDAGValidationError("任务规划不是对象")
    raw_tasks = payload.get("sub_tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks or len(raw_tasks) > MAX_TASKS:
        raise TaskDAGValidationError("任务数量必须在 1 到 5 之间")

    tasks: list[SubTask] = []
    task_ids: set[str] = set()
    for raw_task in raw_tasks:
        if not isinstance(raw_task, Mapping):
            raise TaskDAGValidationError("子任务格式无效")
        task_id = str(raw_task.get("task_id", "")).strip().lower()
        task_type = str(raw_task.get("task_type", "")).strip()
        description = _compact_text(raw_task.get("description"), limit=800)
        dependencies = raw_task.get("dependencies", [])
        if not _TASK_ID_RE.fullmatch(task_id) or task_id in task_ids:
            raise TaskDAGValidationError("子任务标识无效或重复")
        if task_type not in TASK_TYPES or not description:
            raise TaskDAGValidationError("子任务类型或描述无效")
        if not isinstance(dependencies, list) or any(
            not isinstance(item, str) for item in dependencies
        ):
            raise TaskDAGValidationError("子任务依赖格式无效")
        cleaned_dependencies = [item.strip().lower() for item in dependencies]
        if len(cleaned_dependencies) != len(set(cleaned_dependencies)):
            raise TaskDAGValidationError("子任务依赖重复")
        task_ids.add(task_id)
        tasks.append(
            SubTask(
                task_id=task_id,
                task_type=task_type,
                description=description,
                dependencies=cleaned_dependencies,
                timeout_seconds=_bounded_int(
                    raw_task.get("timeout_seconds"),
                    DEFAULT_TASK_TIMEOUT_SECONDS,
                    5,
                    DEFAULT_TASK_TIMEOUT_SECONDS,
                ),
            )
        )

    for task in tasks:
        if task.task_id in task.dependencies or any(
            dependency not in task_ids for dependency in task.dependencies
        ):
            raise TaskDAGValidationError("子任务依赖引用无效")
    _validate_acyclic(tasks)
    return TaskDAG(root_task=query, sub_tasks=tasks)


def _parse_planner_text(text: Any) -> Any:
    """输入：规划 LLM 返回的文本 ``text``。

    输出：解析后的 JSON 值；没有完整 JSON 对象或格式无效时抛出 ``ValueError``。
    功能：兼容不支持 ``response_format`` 的模型，从普通文本完成结果提取任务 DAG。
    """

    content = str(text or "").strip()
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        raise ValueError("任务规划未返回 JSON 对象")
    return json.loads(content[start : end + 1])


def _validate_acyclic(tasks: Iterable[SubTask]) -> None:
    """输入：待校验的子任务集合 ``tasks``。

    输出：无；发现循环依赖时抛出 ``TaskDAGValidationError``。
    功能：使用 Kahn 拓扑消解法拒绝无法执行完成的任务图。
    """

    task_list = list(tasks)
    remaining = {task.task_id: set(task.dependencies) for task in task_list}
    completed: set[str] = set()
    while remaining:
        ready = [task_id for task_id, dependencies in remaining.items() if not dependencies]
        if not ready:
            raise TaskDAGValidationError("子任务存在循环依赖")
        for task_id in ready:
            remaining.pop(task_id)
            completed.add(task_id)
        for dependencies in remaining.values():
            dependencies.difference_update(completed)


def _task_context(task: SubTask, dag: TaskDAG, date_range_days: int) -> str:
    """输入：当前子任务 ``task``、完整任务图 ``dag`` 和日期窗口。

    输出：供子 Agent 使用的受限任务说明文本。
    功能：明确专长、证据要求和图表副作用边界，防止子 Agent 编造数据或直接发送消息。
    """

    dependency_summaries = [
        f"- {dependency}: {_compact_text(next(item for item in dag.sub_tasks if item.task_id == dependency).summary or '前置结果暂不可用')}"
        for dependency in task.dependencies
    ]
    dependencies = "\n".join(dependency_summaries) or "- 无前置依赖，请自行查询。"
    return (
        f"你是售后分析子 Agent，专长是 {task.task_type}。\n"
        f"用户原始问题：{dag.root_task}\n"
        f"时间窗口：最近 {date_range_days} 天。\n"
        f"本节点目标：{task.description}\n"
        f"前置任务压缩摘要：\n{dependencies}\n"
        "只使用已授权的苏宁业务工具查询事实；查询成功但为空时明确写‘查询结果为空’，绝不编造。"
        "工具返回 status=degraded 或 available=false 时，摘要必须原样保留 tool_name、degrade_level、"
        "available=false 和 notice；不得改写为‘没有数据’或‘查询结果为空’。"
        "L1 降级可继续其他分析；L2 降级必须明确写‘核心数据暂不可用、当前结果不完整’。"
        "除 chart_gen 只能使用前置摘要外，每个节点最多调用一次最匹配的查询工具，首次结果不足时直接说明局限。"
        "不要调用 send_aftersale_chart 或任何消息发送工具。"
        "最终仅返回不超过 800 字的中文摘要，包含数据依据、结论和局限。"
    )


class TaskOrchestrator:
    """使用 Hermes 公共子 Agent 生命周期执行受限售后分析 DAG。"""

    def __init__(self, llm: Any, harness: AgentHarness) -> None:
        """输入：宿主 LLM 门面 ``llm`` 和子 Agent Harness ``harness``。

        输出：初始化可复用的编排器实例。
        功能：保存规划、聚合和统一子 Agent 生命周期管理能力，使所有查询节点经过同一 Harness。
        """

        self._llm = llm
        self._harness = harness

    async def plan(self, query: str, date_range_days: int) -> TaskDAG:
        """输入：用户分析问题 ``query`` 与日期窗口 ``date_range_days``。

        输出：LLM 规划的合法 DAG；规划失败时返回固定降级 DAG。
        功能：通过普通文本 JSON 完成将复杂请求拆分为可并发任务，避免不兼容模型的 ``response_format`` 调用。
        """

        instructions = (
            "将售后复杂分析请求拆解为不超过 5 个子任务的 DAG。可用类型仅为："
            "trend_analysis、category_breakdown、reason_drilldown、cost_estimation、chart_gen。"
            "独立查询必须并行；原因下钻应依赖品类或趋势；chart_gen 只能产出图表建议，"
            "不要发送图片。每个任务都应只查询可验证事实，任务描述必须包含用户需求和时间窗口。"
            "只输出一个 JSON 对象，格式为 {\"sub_tasks\":[{\"task_id\":\"trend\",\"task_type\":\"trend_analysis\","
            "\"description\":\"...\",\"dependencies\":[]}] }，不要 Markdown 或其他文字。"
        )
        try:
            result = await self._llm.acomplete(
                [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": f"问题：{query}\n时间窗口：最近 {date_range_days} 天"},
                ],
                max_tokens=1200,
                timeout=20,
                purpose="suning_aftersale_dag_planning",
            )
            dag = _parse_dag(
                _parse_planner_text(getattr(result, "text", "")),
                query,
                date_range_days,
            )
            logger.info("售后复杂分析已由 LLM 规划 %d 个子任务", len(dag.sub_tasks))
            return dag
        except Exception:
            logger.exception("售后复杂分析任务规划失败，使用固定降级 DAG")
            return _fallback_dag(query, date_range_days)

    async def run(self, dag: TaskDAG, date_range_days: int) -> None:
        """输入：已校验任务图 ``dag`` 和日期窗口 ``date_range_days``。

        输出：无；原地写入每个子任务的状态、压缩摘要或失败原因。
        功能：按拓扑层次并发派发子 Agent，在单节点和全局超时下继续执行可用分支。
        """

        deadline = time.monotonic() + dag.global_timeout_seconds
        pending = {task.task_id: task for task in dag.sub_tasks}
        completed: set[str] = set()
        while pending and time.monotonic() < deadline:
            ready = [
                task
                for task in pending.values()
                if set(task.dependencies).issubset(completed)
            ]
            if not ready:
                for task in pending.values():
                    task.status = TaskStatus.FAILED
                    task.error = "任务依赖未能消解"
                break
            await asyncio.gather(
                *(self._run_task(task, dag, date_range_days, deadline) for task in ready)
            )
            for task in ready:
                pending.pop(task.task_id, None)
                completed.add(task.task_id)

        for task in pending.values():
            task.status = TaskStatus.TIMEOUT
            task.error = "全局编排超时，未启动"

    async def _run_task(
        self, task: SubTask, dag: TaskDAG, date_range_days: int, deadline: float
    ) -> None:
        """输入：任务节点 ``task``、任务图 ``dag``、日期窗口和全局截止时间。

        输出：无；原地更新任务状态、摘要或错误信息。
        功能：为查询类节点启动受限子 Agent；图表建议节点直接复用前置真实摘要，避免额外模型等待。
        """

        task.status = TaskStatus.RUNNING
        if task.task_type == "chart_gen":
            unavailable = [
                item.task_type
                for item in dag.sub_tasks
                if item.task_id in task.dependencies and item.status is not TaskStatus.SUCCESS
            ]
            task.status = TaskStatus.SUCCESS
            task.summary = (
                "图表建议节点已由编排器基于前置真实摘要完成，未启动子 Agent、未查询或发送图片。"
                "可按已完成节点的原始 labels 和 values 生成趋势折线、品类柱状、原因占比和退款金额条形图；"
                "缺失维度不生成图表。"
                + (f" 当前不可用维度：{', '.join(unavailable)}。" if unavailable else "")
            )
            return
        remaining = min(task.timeout_seconds, max(0.0, deadline - time.monotonic()))
        try:
            task.summary = await self._harness.run(
                task_id=task.task_id,
                agent_type=task.task_type,
                goal=(
                    f"你是售后分析子 Agent，专长是 {task.task_type}。\n"
                    f"本节点目标：{task.description}"
                ),
                context=_task_context(task, dag, date_range_days),
                timeout_seconds=remaining,
            )
        except TimeoutError:
            task.status = TaskStatus.TIMEOUT
            task.error = "子任务超时"
            return
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.error = _compact_text(exc, limit=300)
            return
        task.status = TaskStatus.SUCCESS

    async def aggregate(self, dag: TaskDAG) -> str:
        """输入：已执行的任务图 ``dag``。

        输出：中文综合分析报告；聚合 LLM 失败时返回确定性的任务摘要。
        功能：只向主聚合器传入各节点压缩结果，并在25秒内无法聚合时立即回退为确定性报告。
        """

        evidence = "\n".join(
            f"[{task.task_type}/{task.task_id}/{task.status.value}] "
            f"{task.summary if task.status is TaskStatus.SUCCESS else task.error or '暂不可用'}"
            for task in dag.sub_tasks
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "你是售后分析报告聚合器。只依据提供的子任务摘要写中文报告，"
                    "不得补充任何未出现的数据。先给关键发现，再给可执行建议；"
                    "失败或超时节点必须明确标为‘暂不可用’。如果有 chart_gen，"
                    "只列出建议图表，不能声称图片已经发送。证据含 available=false 时必须保留"
                    "tool_name、degrade_level 和 notice；L2_CORE 对应维度必须写‘暂不可用’和"
                    "‘当前报告不完整’，不得改写为‘查询结果为空’。"
                ),
            },
            {"role": "user", "content": f"用户问题：{dag.root_task}\n子任务证据：\n{evidence}"},
        ]
        try:
            result = await asyncio.wait_for(
                self._llm.acomplete(
                    messages,
                    max_tokens=1800,
                    timeout=AGGREGATION_TIMEOUT_SECONDS,
                    purpose="suning_aftersale_dag_aggregation",
                ),
                timeout=AGGREGATION_TIMEOUT_SECONDS,
            )
            report = _compact_text(getattr(result, "text", ""), limit=6000)
            if report:
                return report
        except Exception:
            logger.exception("售后复杂分析结果聚合失败，使用确定性摘要")
        return self._fallback_report(dag)

    @staticmethod
    def _fallback_report(dag: TaskDAG) -> str:
        """输入：已执行的任务图 ``dag``。

        输出：不调用 LLM 的中文任务结果汇总。
        功能：聚合模型故障时如实保留各专长 Agent 的可用摘要和不可用原因。
        """

        lines = ["复杂售后分析结果（聚合模型不可用，以下为子任务原始摘要）："]
        for task in dag.sub_tasks:
            if task.status is TaskStatus.SUCCESS:
                lines.append(f"- {task.task_type}：{task.summary}")
            else:
                lines.append(f"- {task.task_type}：暂不可用（{task.error or task.status.value}）")
        return "\n".join(lines)


def _task_payload(task: SubTask) -> dict[str, Any]:
    """输入：已执行或未执行的子任务 ``task``。

    输出：供主 Agent 消费的最小结构化任务状态字典。
    功能：暴露编排过程、任务类型和压缩输出，不泄露子 Agent 完整上下文或工具轨迹。
    """

    return {
        "task_id": task.task_id,
        "task_type": task.task_type,
        "dependencies": task.dependencies,
        "status": task.status.value,
        "summary": task.summary,
        "error": task.error,
    }


def make_complex_analysis_handler(llm: Any, lifecycle: Any):
    """输入：宿主 LLM 门面 ``llm`` 与公开生命周期服务 ``lifecycle``。

    输出：可注册到 Hermes 的异步复杂分析工具处理器。
    功能：绑定主机托管能力，并将平台范围和 DAG 编排限制封装在单个工具入口中。
    """

    async def handler(args: dict[str, Any], **_kwargs: Any) -> str:
        """输入：模型传入参数 ``args``；其余 Hermes 参数由 ``_kwargs`` 接收。

        输出：包含综合报告、节点状态和平台信息的 Hermes 工具结果。
        功能：为可信飞书、企微或钉钉会话执行规划、并发子 Agent 和结果聚合。
        """

        try:
            identity = current_identity()
        except PermissionError as exc:
            return tool_error(str(exc))
        platform = identity["platform"]

        query = _compact_text((args or {}).get("query"), limit=500)
        if not query:
            return tool_error("复杂分析问题不能为空")
        date_range_days = _bounded_int(
            (args or {}).get("date_range_days"), 30, 1, 90
        )
        harness = AgentHarness(lifecycle)
        orchestrator = TaskOrchestrator(llm, harness)
        dag = await orchestrator.plan(query, date_range_days)
        await orchestrator.run(dag, date_range_days)
        report = await orchestrator.aggregate(dag)
        unavailable = [
            task.task_type for task in dag.sub_tasks if task.status is not TaskStatus.SUCCESS
        ]
        return tool_result(
            {
                "status": "partial" if unavailable else "completed",
                "platform": platform,
                "planner": dag.planner,
                "report": report,
                "unavailable_sections": unavailable,
                "tasks": [_task_payload(task) for task in dag.sub_tasks],
                "harness": harness.snapshot(),
            }
        )

    return handler


__all__ = [
    "COMPLEX_ANALYSIS_SCHEMA",
    "DEFAULT_GLOBAL_TIMEOUT_SECONDS",
    "AgentHarness",
    "SubTask",
    "TaskDAG",
    "TaskDAGValidationError",
    "TaskOrchestrator",
    "TaskStatus",
    "make_complex_analysis_handler",
]
