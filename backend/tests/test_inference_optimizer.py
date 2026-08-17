"""验证 Agent 推理效率优化的四层链路与基准统计。"""

from __future__ import annotations

from typing import Any

import pytest

from suning_hermes_agent.inference_optimizer import (
    InferenceBenchmark,
    InferenceOptimizer,
    MODEL_TIERS,
)


class _FakeRedis:
    """提供带 TTL 记录的最小 Redis 替身。"""

    def __init__(self) -> None:
        """输入：无。

        输出：初始化空键值与 TTL 记录。
        功能：让优化器测试验证缓存键和值，而不连接外部 Redis 服务。
        """

        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        """输入：缓存键 ``key``。

        输出：已保存字符串或 ``None``。
        功能：模拟 Redis 的同步读取接口。
        """

        return self.values.get(key)

    def setex(self, key: str, seconds: int, value: str) -> None:
        """输入：缓存键 ``key``、TTL 秒数 ``seconds`` 与字符串值 ``value``。

        输出：无；原地记录值和过期时间。
        功能：模拟推理优化器使用的 Redis 写入接口。
        """

        self.values[key] = value
        self.ttls[key] = seconds


class _FakeLlm:
    """记录模型请求的异步聊天客户端替身。"""

    def __init__(self) -> None:
        """输入：无。

        输出：初始化空调用记录。
        功能：让测试检查路由模型和完整 Prompt，不产生真实模型请求。
        """

        self.calls: list[dict[str, Any]] = []

    async def chat(self, prompt: str, *, model: str, max_tokens: int) -> str:
        """输入：完整 Prompt ``prompt``、模型名 ``model`` 与最大输出 Token ``max_tokens``。

        输出：固定中文回复。
        功能：记录推理调用的关键参数，作为外部 LLM SDK 的最小替身。
        """

        self.calls.append({"prompt": prompt, "model": model, "max_tokens": max_tokens})
        return "退单量为12笔。"


def test_prompt_cache_uses_memory_before_redis_and_invalidates_prompt_versions() -> None:
    """输入：同一场景与参数下的同版、升级版 Prompt 模板。

    输出：无；缓存命中、渲染文本和一小时 TTL 不正确时由 pytest 报告失败。
    功能：验证 L1 内存与 L2 Redis 共同消除重复渲染，并拒绝返回已升级模板之前的缓存。
    """

    redis = _FakeRedis()
    optimizer = InferenceOptimizer(_FakeLlm(), redis)

    first, first_hit = optimizer.get_or_cache_system_prompt("return_analysis", "品类：{category}", category="空调")
    second, second_hit = optimizer.get_or_cache_system_prompt("return_analysis", "品类：{category}", category="空调")
    updated, updated_hit = optimizer.get_or_cache_system_prompt("return_analysis", "新版品类：{category}", category="空调")

    assert (first, first_hit) == ("品类：空调", False)
    assert (second, second_hit) == ("品类：空调", True)
    assert (updated, updated_hit) == ("新版品类：空调", False)
    assert set(redis.ttls.values()) == {3600}


def test_select_model_routes_only_high_confidence_simple_scenes_to_lite() -> None:
    """输入：简单退单、低置信退单和明确问候三种路由结果。

    输出：无；模型分层与路由规则不一致时由 pytest 报告失败。
    功能：锁定简单查询使用 Lite、复杂或低置信查询保留 Full 的成本边界。
    """

    optimizer = InferenceOptimizer(_FakeLlm(), _FakeRedis())

    assert optimizer.select_model("return_analysis", 0.91) is MODEL_TIERS["lite"]
    assert optimizer.select_model("return_analysis", 0.85) is MODEL_TIERS["full"]
    assert optimizer.select_model("greeting", 0.0) is MODEL_TIERS["lite"]


@pytest.mark.asyncio
async def test_precompute_hot_queries_warms_results_and_ignores_one_failure() -> None:
    """输入：首项失败、其余成功的异步工具执行器。

    输出：无；成功结果未按一小时 TTL 写入时由 pytest 报告失败。
    功能：验证凌晨预热的单项故障不会阻断其他高频退单统计缓存。
    """

    redis = _FakeRedis()
    optimizer = InferenceOptimizer(_FakeLlm(), redis)
    calls = 0

    async def execute(tool: str, **params: Any) -> dict[str, Any]:
        """输入：工具名 ``tool`` 和查询参数 ``params``。

        输出：固定统计结果；首个调用抛出 ``RuntimeError``。
        功能：模拟可部分失败的 MCP 预计算执行器。
        """

        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("临时不可用")
        return {"tool": tool, "params": params, "return_count": 12}

    await optimizer.precompute_hot_queries(execute)

    assert calls == 2
    assert len(redis.values) == 1
    assert set(redis.ttls.values()) == {3600}
    assert optimizer.get_precomputed("return_analysis", {"group_by": "day", "date_range_days": 7})["return_count"] == 12


@pytest.mark.asyncio
async def test_precomputed_result_skips_llm_and_mcp_path() -> None:
    """输入：已写入的预计算统计和一个可记录调用的模型替身。

    输出：无；仍调用模型或返回的缓存统计不正确时由 pytest 报告失败。
    功能：验证命中热点结果时直接返回，满足跳过 LLM 与 MCP 的优化路径。
    """

    redis = _FakeRedis()
    llm = _FakeLlm()
    optimizer = InferenceOptimizer(llm, redis)
    redis.setex(
        optimizer._precomputed_key(
            "return_analysis", {"group_by": "category", "date_range_days": 1}
        ),
        3600,
        '{"return_count": 12}',
    )

    response, stats = await optimizer.optimized_inference("今天退单量多少", "return_analysis", 0.99, {}, date_range_days=1)

    assert response == '{"return_count": 12}'
    assert stats.precomputed is True
    assert stats.model_used == "cache"
    assert llm.calls == []


@pytest.mark.asyncio
async def test_inference_uses_lite_prompt_cache_and_compresses_long_context() -> None:
    """输入：高置信退单问题以及超过 15K Token 的五轮上下文。

    输出：无；Lite 路由、历史裁剪或缓存标记错误时由 pytest 报告失败。
    功能：一次覆盖未命中预计算后的 Prompt 缓存、模型分层和上下文压缩三个层次。
    """

    redis = _FakeRedis()
    llm = _FakeLlm()
    optimizer = InferenceOptimizer(llm, redis)
    context = {
        "slots": {"topic": "退单"},
        "recent_turns": [{"user_msg": "问" * 5000, "assistant_reply": "答"}] * 5,
    }

    response, stats = await optimizer.optimized_inference("今天退单量多少", "return_analysis", 0.91, context, date_range_days=1)

    assert response == "退单量为12笔。"
    assert stats.model_used == "deepseek-v4-lite"
    assert stats.cache_hit is False
    assert len(context["recent_turns"]) == 3
    assert context["compressed_history"] == ["已完成的2轮对话中，用户主要咨询了退单相关问题。"]
    assert "历史会话摘要:" in llm.calls[0]["prompt"]
    assert llm.calls[0]["max_tokens"] == 500


@pytest.mark.asyncio
async def test_benchmark_averages_real_optimizer_stats() -> None:
    """输入：一个简单查询基准用例和无预计算缓存的优化器。

    输出：无；平均 Token、模型或缓存命中率不符合三次执行时由 pytest 报告失败。
    功能：验证基准器复用真实优化链路并报告可比较的场景指标。
    """

    benchmark = InferenceBenchmark(InferenceOptimizer(_FakeLlm(), _FakeRedis()))

    result = await benchmark.benchmark([{"scene": "simple_query", "query": "你好"}])

    assert result["simple_query"]["model"] == "deepseek-v4-lite"
    assert result["simple_query"]["avg_tokens"] > 0
    assert result["simple_query"]["cache_hit_rate"] == pytest.approx(2 / 3)
