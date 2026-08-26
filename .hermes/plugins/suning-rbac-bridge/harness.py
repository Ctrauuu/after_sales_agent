"""Hermes 托管 Leaf 子 Agent 的生命周期 Harness。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.subagent_lifecycle import SubagentLifecycleService


logger = logging.getLogger(__name__)


class AgentStatus(str, Enum):
    """Harness 记录的 Leaf 子 Agent 状态。"""

    STARTING = "starting"
    RUNNING = "running"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    STOPPED = "stopped"


@dataclass
class AgentInstance:
    """一次复杂分析中由 Harness 管理的 Leaf 子 Agent。"""

    agent_id: str
    agent_type: str
    task_id: str
    handle: Any | None = None
    status: AgentStatus = AgentStatus.STARTING


def _launch_request(**kwargs: Any) -> Any:
    """输入：Hermes 启动请求的关键字参数 ``kwargs``。

    输出：``SubagentLaunchRequest`` 实例；生命周期模块不可用时抛出导入异常。
    功能：延迟读取 Hermes 公开请求类型，使插件仅在实际派发 Leaf 子 Agent 时依赖运行时模块。
    """

    from agent.subagent_lifecycle import SubagentLaunchRequest

    return SubagentLaunchRequest(**kwargs)


class AgentHarness:
    """统一启动、等待、取消并记录一次复杂分析的所有 Leaf 子 Agent。"""

    def __init__(self, lifecycle: "SubagentLifecycleService") -> None:
        """输入：Hermes 公共生命周期服务 ``lifecycle``。

        输出：初始化本次复杂分析的空子 Agent 注册表。
        功能：保存唯一允许创建和终止 Leaf 子 Agent 的宿主接口，避免编排器直接管理句柄。
        启动一个 Leaf 子 Agent，然后一直等它结束；如果超时就取消；如果成功就拿摘要；如果失败就抛异常；最后记录状态。
        """

        self._lifecycle = lifecycle
        self._instances: dict[str, AgentInstance] = {}

    async def run(
        self,
        *,
        task_id: str,
        agent_type: str,
        goal: str,
        context: str,
        timeout_seconds: float,
    ) -> str:
        """输入：任务 ID ``task_id``、Agent 类型 ``agent_type``、目标 ``goal``、上下文 ``context`` 与超时。

        输出：成功 Leaf 子 Agent 的摘要；超时时抛出 ``TimeoutError``，失败时抛出 ``RuntimeError``。
        功能：为每个 DAG 查询节点登记并启动受限子 Agent，统一处理等待、超时取消、终态读取和状态日志。
        """

        agent_id = f"{agent_type}-{task_id}-{time.monotonic_ns()}"
        instance = AgentInstance(agent_id, agent_type, task_id)
        self._instances[agent_id] = instance
        try:
            instance.handle = self._lifecycle.launch(
                _launch_request(
                    goal=goal,
                    context=context,
                    role="leaf",
                    allowed_toolsets=("suning_business",),
                    correlation_id=f"suning-analysis-{agent_id}",
                    metadata={
                        "agent_id": agent_id,
                        "task_id": task_id,
                        "task_type": agent_type,
                    },
                )
            )
            instance.status = AgentStatus.RUNNING
            logger.info("Harness 启动子 Agent: id=%s type=%s", agent_id, agent_type)
            terminal = await asyncio.to_thread(
                self._lifecycle.wait,
                instance.handle,
                timeout_seconds=timeout_seconds,
            )
            if getattr(terminal, "timed_out", False):
                await asyncio.to_thread(
                    self._lifecycle.cancel,
                    instance.handle,
                    reason="子任务超过时间上限",
                )
                instance.status = AgentStatus.STOPPED
                raise TimeoutError("子任务超时")

            result = self._lifecycle.result(instance.handle)
            terminal_state = getattr(result, "terminal_state", None)
            if getattr(terminal_state, "value", terminal_state) == "SUCCEEDED":
                instance.status = AgentStatus.HEALTHY
                return (
                    " ".join(str(getattr(result, "summary", "") or "").split())
                    or "子任务未返回可用摘要"
                )

            instance.status = AgentStatus.UNHEALTHY
            error = getattr(result, "error_message", None) or getattr(
                result, "error_classification", None
            ) or "子任务未成功完成"
            raise RuntimeError(" ".join(str(error).split())[:300])
        except Exception:
            if instance.status in (AgentStatus.STARTING, AgentStatus.RUNNING):
                instance.status = AgentStatus.UNHEALTHY
            raise
        finally:
            logger.info(
                "Harness 子 Agent 结束: id=%s type=%s status=%s",
                agent_id,
                agent_type,
                instance.status.value,
            )

    def snapshot(self) -> dict[str, Any]:
        """输入：无；读取当前复杂分析已经登记的子 Agent 实例。

        输出：总数、运行数和各状态计数的字典。
        功能：为工具结果和日志提供可验证的 Harness 生命周期摘要，不暴露子 Agent 完整上下文。
        """

        statuses = {status.value: 0 for status in AgentStatus}
        for instance in self._instances.values():
            statuses[instance.status.value] += 1
        return {
            "total": len(self._instances),
            "running": statuses[AgentStatus.RUNNING.value],
            "statuses": statuses,
        }


__all__ = ["AgentHarness", "AgentInstance", "AgentStatus"]
