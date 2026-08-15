"""验证 Agent Skill 自进化的沉淀、更新和失效降级。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


SKILL_EVOLUTION_PATH = (
    Path(__file__).resolve().parents[2]
    / ".hermes"
    / "plugins"
    / "suning-rbac-bridge"
    / "skill_evolution.py"
)


def _load_skill_evolution() -> ModuleType:
    """输入：无；读取项目内真实自进化模块路径。

    输出：新加载的 ``skill_evolution`` 模块对象。
    功能：隔离模块级状态，令每个测试不依赖 Hermes 网关或插件包相对导入环境。
    """

    module_name = "_suning_skill_evolution_test"
    spec = importlib.util.spec_from_file_location(module_name, SKILL_EVOLUTION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 Skill 自进化模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeLlm:
    """按调用序列返回自动 Skill 提取 JSON 的宿主模型替身。"""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        """输入：每次提取要返回的 JSON 对象列表 ``responses``。

        输出：初始化响应队列。
        功能：以确定性文本模拟 Hermes ``acomplete``，覆盖创建和同类 Skill 更新场景。
        """

        self._responses = responses

    async def acomplete(self, _messages: list[dict[str, Any]], **kwargs: Any) -> SimpleNamespace:
        """输入：模型消息 ``_messages`` 与调用参数 ``kwargs``。

        输出：含 JSON 文本的完成结果。
        功能：验证自进化调用宿主异步 LLM，并依次交付测试预设的抽取字段。
        """

        assert kwargs["purpose"] == "suning_skill_evolution"
        return SimpleNamespace(text=json.dumps(self._responses.pop(0), ensure_ascii=False))


class _FakeEmbedder:
    """让相同售后描述稳定命中的轻量 Embedding 替身。"""

    def embed(self, text: str) -> list[float]:
        """输入：需要比较的 Skill 描述 ``text``。

        输出：两维确定性向量。
        功能：把含“退单”的描述映射到同一方向，验证大于 0.8 时更新已有 Skill。
        """

        return [1.0, 0.0] if "退单" in text else [0.0, 1.0]


def _extraction(patterns: list[str], days: int = 7) -> dict[str, Any]:
    """输入：触发模式列表 ``patterns`` 和工作流中的日期窗口 ``days``。

    输出：符合自进化提取协议的自动 Skill 字典。
    功能：构造同一退单分析流程的两个版本，验证新问法合并与步骤替换。
    """

    return {
        "should_create_skill": True,
        "skill_name": "退单原因分析",
        "description": "按品类和原因分析退单并输出结论",
        "trigger_patterns": patterns,
        "workflow": [
            {
                "step": 1,
                "tool": "query_return_stats_nl2sql",
                "params": {"days": days, "category": "{品类}"},
                "output_key": "return_stats",
            },
            {
                "step": 2,
                "tool": "query_aftersale_nl2sql",
                "params": {"category": "{品类}"},
                "output_key": "reason_stats",
            },
        ],
        "output_template": "按 {return_stats} 和 {reason_stats} 输出结论。",
    }


def _complex_trace(module: ModuleType) -> Any:
    """输入：已加载的自进化模块 ``module``。

    输出：达到文档复杂度门槛的 ``ExecutionTrace``。
    功能：以五个 MCP 调用和四轮轨迹触发自动沉淀，不依赖真实工具或计时。
    """

    calls = [
        {"tool": "query_return_stats_nl2sql", "params": {"days": 7}}
        for _ in range(5)
    ]
    return module.ExecutionTrace(
        user_query="分析电视退单原因",
        turns=[{"mcp_calls": calls}] * 4,
        mcp_tools_used=[call["tool"] for call in calls],
        total_duration_seconds=20,
        final_output="电视退单主要集中于质量问题，建议优先排查供应商。",
    )


@pytest.mark.asyncio
async def test_evolution_creates_updates_and_reloads_markdown_skill(tmp_path: Path) -> None:
    """输入：临时 Skills Hub、两次 LLM 提取结果和确定性 Embedding。

    输出：无；创建、版本更新或 Markdown 反序列化不正确时由断言报告失败。
    功能：验证复杂轨迹先创建 Skill，再因语义重复更新版本和工作流，且文件可供下次加载。
    """

    module = _load_skill_evolution()
    engine = module.SkillEvolutionEngine(
        _FakeLlm(
            [
                _extraction(["分析{品类}最近{days}天退单原因"]),
                _extraction(["看看{品类}退单为什么", "分析{品类}退单原因"], 30),
            ]
        ),
        tmp_path,
        _FakeEmbedder(),
    )

    created = await engine.evaluate_and_evolve(_complex_trace(module))
    updated = await engine.evaluate_and_evolve(_complex_trace(module))

    assert created is not None
    assert updated is not None
    assert updated.skill_id == created.skill_id
    assert updated.version == 2
    assert updated.workflow[0]["params"]["days"] == 30
    skill_file = tmp_path / created.skill_id / "SKILL.md"
    assert skill_file.is_file()
    assert "分析电视退单原因" in engine.load_all_skills()[0].trigger_patterns


@pytest.mark.asyncio
async def test_skill_match_prechecks_tools_and_parameterized_pattern(tmp_path: Path) -> None:
    """输入：临时 Skill 文件、参数化触发问题和两组可用工具集合。

    输出：无；失效工具仍匹配或变量触发模式不生效时由断言报告失败。
    功能：验证下次提问命中 Skill 前先检查依赖 MCP，缺失时按文档降级为从头推理。
    """

    module = _load_skill_evolution()
    engine = module.SkillEvolutionEngine(
        _FakeLlm([_extraction(["分析{品类}最近{days}天退单原因"])]),
        tmp_path,
        _FakeEmbedder(),
    )
    created = await engine.evaluate_and_evolve(_complex_trace(module))

    assert created is not None
    hooks = module.SkillEvolutionHooks(
        engine,
        {"query_return_stats_nl2sql", "query_aftersale_nl2sql"},
    )
    injected = hooks.pre_llm_call(
        session_id="session-1",
        turn_id="turn-1",
        user_message="请分析最近30天电视退单原因",
    )
    unavailable = module.SkillEvolutionHooks(
        engine,
        {"query_return_stats_nl2sql"},
    ).pre_llm_call(
        session_id="session-2",
        turn_id="turn-2",
        user_message="请分析最近30天电视退单原因",
    )

    assert injected is not None
    assert "退单原因分析" in injected["context"]
    assert unavailable is None


@pytest.mark.asyncio
async def test_sync_post_hook_submits_evolution_in_background_thread(tmp_path: Path) -> None:
    """输入：同步调用的回答后 Hook、五次 MCP 轨迹和临时 Skills Hub。

    输出：无；后台线程未沉淀 Skill 时由断言报告失败。
    功能：覆盖 Hermes 同步 ``post_llm_call`` 契约，防止没有运行 asyncio loop 时复杂任务被静默跳过。
    """

    module = _load_skill_evolution()
    engine = module.SkillEvolutionEngine(
        _FakeLlm([_extraction(["分析{品类}退单原因"])]),
        tmp_path,
        _FakeEmbedder(),
    )
    hooks = module.SkillEvolutionHooks(engine, {"query_return_stats_nl2sql", "query_aftersale_nl2sql"})
    hooks.pre_llm_call(
        session_id="session-1",
        turn_id="turn-1",
        user_message="分析电视退单原因",
    )
    for _ in range(5):
        hooks.pre_tool_call(
            session_id="session-1",
            turn_id="turn-1",
            tool_name="query_return_stats_nl2sql",
            arguments={"category": "电视"},
        )
    thread = threading.Thread(
        target=hooks.post_llm_call,
        kwargs={
            "session_id": "session-1",
            "turn_id": "turn-1",
            "assistant_response": "质量问题为主要退单原因。",
        },
    )
    thread.start()
    thread.join()

    for _ in range(100):
        if list(tmp_path.glob("*/SKILL.md")):
            break
        await asyncio.sleep(0.01)
    assert list(tmp_path.glob("*/SKILL.md"))
