"""验证三平台复杂售后分析的 DAG 规划、并发执行和平台适配。"""

from __future__ import annotations

import importlib.util
import json
import sys
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


PLUGIN_ROOT = (
    Path(__file__).resolve().parents[2] / ".hermes" / "plugins" / "suning-rbac-bridge"
)
ORCHESTRATION_PATH = PLUGIN_ROOT / "orchestration.py"


class _SubagentState(str, Enum):
    """测试中替代 Hermes 子 Agent 状态枚举的最小集合。"""

    SUCCEEDED = "SUCCEEDED"


class _SubagentLaunchRequest:
    """记录编排器交给 Hermes 生命周期的子任务启动参数。"""

    def __init__(self, **kwargs: Any) -> None:
        """输入：生命周期启动请求的关键字参数 ``kwargs``。

        输出：保存每个参数为实例属性。
        功能：模拟 Hermes 的不可变启动请求，供测试检查子任务目标和权限范围。
        """

        self.__dict__.update(kwargs)


def _load_orchestration(
    monkeypatch: pytest.MonkeyPatch, platform: str = "feishu"
) -> ModuleType:
    """输入：pytest 补丁器 ``monkeypatch`` 和模拟会话平台 ``platform``。

    输出：装载了真实编排代码的模块对象。
    功能：替换 Hermes 运行时边界，使 DAG 单元测试无需启动网关、LLM 或子 Agent 进程。
    """

    agent = ModuleType("agent")
    agent.__path__ = []  # type: ignore[attr-defined]
    lifecycle = ModuleType("agent.subagent_lifecycle")
    lifecycle.SubagentLaunchRequest = _SubagentLaunchRequest
    lifecycle.SubagentState = _SubagentState
    tools = ModuleType("tools")
    tools.__path__ = []  # type: ignore[attr-defined]
    registry = ModuleType("tools.registry")
    registry.tool_error = lambda message: json.dumps({"error": message}, ensure_ascii=False)
    registry.tool_result = lambda payload: json.dumps(payload, ensure_ascii=False)
    package_name = "_suning_orchestration_test"
    package = ModuleType(package_name)
    package.__path__ = [str(PLUGIN_ROOT)]  # type: ignore[attr-defined]
    bridge = ModuleType(f"{package_name}.bridge")
    bridge.current_identity = lambda: {"platform": platform}

    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.subagent_lifecycle", lifecycle)
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.registry", registry)
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, f"{package_name}.bridge", bridge)

    spec = importlib.util.spec_from_file_location(
        f"{package_name}.orchestration", ORCHESTRATION_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载复杂分析编排模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeLlm:
    """返回确定性任务图和聚合报告的宿主 LLM 替身。"""

    def __init__(self) -> None:
        """输入：无。

        输出：初始化普通完成调用用途记录。
        功能：区分规划与聚合请求，验证规划器不再调用不兼容的结构化响应格式。
        """

        self.purposes: list[str] = []

    async def acomplete(self, _messages: list[dict[str, Any]], **kwargs: Any) -> SimpleNamespace:
        """输入：规划或聚合消息 ``_messages`` 及调用参数 ``kwargs``。

        输出：规划时返回 JSON 文本，聚合时返回固定中文报告。
        功能：模拟不使用 ``response_format`` 的宿主 LLM，覆盖普通 JSON 规划与报告聚合两条路径。
        """

        purpose = str(kwargs.get("purpose", ""))
        self.purposes.append(purpose)
        if purpose == "suning_aftersale_dag_planning":
            return SimpleNamespace(
                text=json.dumps(
                    {
                "sub_tasks": [
                    {
                        "task_id": "trend",
                        "task_type": "trend_analysis",
                        "description": "统计退单趋势",
                        "dependencies": [],
                    },
                    {
                        "task_id": "category",
                        "task_type": "category_breakdown",
                        "description": "统计品类分布",
                        "dependencies": [],
                    },
                    {
                        "task_id": "reason",
                        "task_type": "reason_drilldown",
                        "description": "下钻主要品类退单原因",
                        "dependencies": ["trend", "category"],
                    },
                ]
                    },
                    ensure_ascii=False,
                )
            )

        return SimpleNamespace(text="聚合报告：数据均来自子任务查询。")


class _FakeLifecycle:
    """同步成功完成的子 Agent 生命周期替身。"""

    def __init__(self) -> None:
        """输入：无。

        输出：初始化子任务请求记录。
        功能：记录启动顺序，供测试验证 DAG 依赖和只授予业务工具集的约束。
        """

        self.requests: list[_SubagentLaunchRequest] = []

    def launch(self, request: _SubagentLaunchRequest) -> int:
        """输入：待启动的子任务请求 ``request``。

        输出：可回传给等待和结果读取接口的任务序号。
        功能：记录每个子 Agent 的目标和权限范围，并模拟立即接受启动。
        """

        self.requests.append(request)
        return len(self.requests) - 1

    def wait(self, _handle: int, *, timeout_seconds: float) -> SimpleNamespace:
        """输入：子任务句柄 ``_handle`` 与等待时限 ``timeout_seconds``。

        输出：表示未超时的终态对象。
        功能：模拟 Hermes 生命周期等待成功，避免测试产生真实并发工作。
        """

        assert timeout_seconds > 0
        return SimpleNamespace(timed_out=False)

    def cancel(self, _handle: int, *, reason: str) -> None:
        """输入：子任务句柄 ``_handle`` 与取消原因 ``reason``。

        输出：无。
        功能：提供编排器超时路径所需的兼容接口，本成功案例不会调用。
        """

        raise AssertionError("成功任务不应取消")

    def result(self, handle: int) -> SimpleNamespace:
        """输入：已完成子任务句柄 ``handle``。

        输出：带受限摘要的成功生命周期结果。
        功能：模拟子 Agent 返回压缩事实摘要，避免完整轨迹进入聚合器。
        """

        return SimpleNamespace(
            terminal_state=_SubagentState.SUCCEEDED,
            summary=f"任务 {handle} 的真实查询摘要",
        )


def test_parse_dag_rejects_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """输入：pytest 补丁器 ``monkeypatch``。

    输出：无；循环依赖未被拒绝时由 pytest 报告失败。
    功能：锁定 LLM 规划必须是可拓扑执行的有向无环图。
    """

    orchestration = _load_orchestration(monkeypatch)

    with pytest.raises(orchestration.TaskDAGValidationError):
        orchestration._parse_dag(
            {
                "sub_tasks": [
                    {"task_id": "one", "task_type": "trend_analysis", "description": "趋势", "dependencies": ["two"]},
                    {"task_id": "two", "task_type": "cost_estimation", "description": "成本", "dependencies": ["one"]},
                ]
            },
            "分析售后",
            30,
        )


@pytest.mark.asyncio
async def test_orchestrator_executes_dependency_layers_and_aggregates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：pytest 补丁器 ``monkeypatch``。

    输出：无；任务层次、权限范围或聚合结果不正确时由 pytest 报告失败。
    功能：验证第一层独立子任务可同时派发，原因下钻在依赖摘要就绪后执行并生成统一报告。
    """

    orchestration = _load_orchestration(monkeypatch)
    lifecycle = _FakeLifecycle()
    llm = _FakeLlm()
    coordinator = orchestration.TaskOrchestrator(llm, lifecycle)
    dag = await coordinator.plan("本月售后全量分析", 30)

    await coordinator.run(dag, 30)
    report = await coordinator.aggregate(dag)

    assert [request.allowed_toolsets for request in lifecycle.requests] == [
        ("suning_business",),
        ("suning_business",),
        ("suning_business",),
    ]
    assert "无前置依赖，请自行查询" in lifecycle.requests[0].goal
    assert "任务 0 的真实查询摘要" in lifecycle.requests[2].goal
    assert "最多调用一次" in lifecycle.requests[0].goal
    assert all(task.status is orchestration.TaskStatus.SUCCESS for task in dag.sub_tasks)
    assert dag.planner == "llm"
    assert llm.purposes == [
        "suning_aftersale_dag_planning",
        "suning_aftersale_dag_aggregation",
    ]
    assert report == "聚合报告：数据均来自子任务查询。"


@pytest.mark.asyncio
async def test_chart_node_reuses_completed_summaries_without_subagent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：pytest 补丁器 ``monkeypatch``。

    输出：无；图表建议仍启动子 Agent 或未成功完成时由 pytest 报告失败。
    功能：验证 ``chart_gen`` 作为 DAG 节点复用前置真实摘要，不额外消耗模型等待时间。
    """

    orchestration = _load_orchestration(monkeypatch)
    lifecycle = _FakeLifecycle()
    dag = orchestration.TaskDAG(
        "售后分析",
        [
            orchestration.SubTask("trend", "trend_analysis", "统计趋势"),
            orchestration.SubTask(
                "chart", "chart_gen", "生成图表建议", ["trend"]
            ),
        ],
    )

    await orchestration.TaskOrchestrator(_FakeLlm(), lifecycle).run(dag, 30)

    assert len(lifecycle.requests) == 1
    assert dag.sub_tasks[1].status is orchestration.TaskStatus.SUCCESS
    assert "未启动子 Agent" in dag.sub_tasks[1].summary


@pytest.mark.asyncio
async def test_aggregate_falls_back_when_outer_timeout_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：pytest 补丁器 ``monkeypatch``。

    输出：无；聚合外部超时未返回确定性摘要时由 pytest 报告失败。
    功能：验证宿主 LLM 内部重试不能绕过编排器的聚合时间上限。
    """

    orchestration = _load_orchestration(monkeypatch)

    async def timeout(awaitable: Any, **_kwargs: Any) -> None:
        """输入：待等待协程 ``awaitable`` 与其余等待参数 ``_kwargs``。

        输出：无；关闭协程并抛出 ``TimeoutError``。
        功能：模拟聚合 LLM 超时，避免测试实际等待25秒。
        """

        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(orchestration.asyncio, "wait_for", timeout)
    dag = orchestration.TaskDAG(
        "售后分析",
        [
            orchestration.SubTask(
                "trend",
                "trend_analysis",
                "统计趋势",
                status=orchestration.TaskStatus.SUCCESS,
                summary="真实趋势摘要",
            )
        ],
    )

    report = await orchestration.TaskOrchestrator(_FakeLlm(), _FakeLifecycle()).aggregate(dag)

    assert "聚合模型不可用" in report


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ("wecom", "dingtalk"))
async def test_handler_runs_multi_im_analysis(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """输入：pytest 补丁器 ``monkeypatch`` 和当前 IM 平台 ``platform``。

    输出：无；任一平台未进入受限子 Agent 流程或结果平台不正确时由 pytest 报告失败。
    功能：验证企微和钉钉身份统一后均复用相同的 DAG 编排、工具集限制和聚合能力。
    """

    orchestration = _load_orchestration(monkeypatch, platform=platform)
    lifecycle = _FakeLifecycle()
    handler = orchestration.make_complex_analysis_handler(_FakeLlm(), lifecycle)

    payload = json.loads(await handler({"query": "本月售后全量分析"}))

    assert payload["status"] == "completed"
    assert payload["platform"] == platform
    assert len(lifecycle.requests) == 3
