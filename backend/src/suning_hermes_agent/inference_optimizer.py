"""以缓存、模型分层和上下文裁剪降低 Agent 推理开销。"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelTier:
    """可选推理模型的成本和能力边界。"""

    name: str
    cost_per_1k_tokens: float
    avg_latency_ms: float
    context_window: int
    supports_cache: bool


MODEL_TIERS = {
    "lite": ModelTier("deepseek-v4-lite", 0.0001, 300, 32768, True),
    "full": ModelTier("deepseek-v4", 0.0005, 1500, 131072, True),
}

SCENE_PROMPTS = {
    "default": "你是苏宁售后助手。基于已授权的数据准确回答用户问题。",
    "return_analysis": "你是苏宁退单分析助手。仅基于已授权的退单统计准确回答。",
    "order_trace": "你是苏宁订单售后链路助手。仅基于已授权的订单信息准确回答。",
    "simple_query": "你是苏宁售后助手。简洁、准确地回答用户问题。",
    "greeting": "你是苏宁售后助手。礼貌简洁地回应用户。",
}

HOT_QUERIES = (
    {
        "scene": "return_analysis",
        "queries": ("今天退单量多少", "今天退单量", "各品类退单分布"),
        "tool": "query_return_stats_nl2sql",
        "params": {"group_by": "category", "date_range_days": 1},
    },
    {
        "scene": "return_analysis",
        "queries": ("近7天退单趋势", "近7日退单趋势"),
        "tool": "query_return_stats_nl2sql",
        "params": {"group_by": "day", "date_range_days": 7},
    },
)


@dataclass
class InferenceStats:
    """单次推理的可观测成本数据。"""

    model_used: str
    tokens_input: int
    tokens_output: int
    latency_ms: float
    cache_hit: bool = False
    precomputed: bool = False


class InferenceOptimizer:
    """按四层独立开关顺序执行 Agent 推理优化。"""

    def __init__(self, llm_client: Any, redis_client: Any) -> None:
        """输入：异步聊天客户端 ``llm_client`` 与同步 Redis 客户端 ``redis_client``。

        输出：初始化空的进程内 Prompt 缓存。
        功能：保存推理和共享缓存边界，使每层优化可在不依赖具体 SDK 的情况下组合使用。
        """

        self.llm = llm_client
        self.redis = redis_client
        self._prompt_cache: dict[str, str] = {}

    def get_or_cache_system_prompt(
        self, scene: str, prompt_template: str, **kwargs: Any
    ) -> tuple[str, bool]:
        """输入：场景 ``scene``、可格式化 Prompt 模板和渲染参数 ``kwargs``。

        输出：渲染后的 System Prompt 与命中标记；模板参数不足时抛出 ``KeyError``。
        功能：按本地内存、Redis、模板渲染的顺序复用稳定 Prompt，减少重复传输的上下文。
        """

        cache_key = self._prompt_key(scene, prompt_template, kwargs)
        if cache_key in self._prompt_cache:
            return self._prompt_cache[cache_key], True
        cached = self.redis.get(cache_key)
        if cached is not None:
            rendered = self._decode(cached)
            self._prompt_cache[cache_key] = rendered
            return rendered, True
        rendered = prompt_template.format(**kwargs)
        self.redis.setex(cache_key, 3600, rendered)
        self._prompt_cache[cache_key] = rendered
        return rendered, False

    def select_model(self, scene: str, complexity: float) -> ModelTier:
        """输入：语义路由场景 ``scene`` 和路由器给出的置信度 ``complexity``。

        输出：适配当前请求的 Lite 或 Full ``ModelTier``。
        功能：将高置信简单退单/订单查询和明确简单场景降级为低延迟模型，其余保留完整模型。
        """

        if complexity > 0.85 and scene in {"return_analysis", "order_trace"}:
            return MODEL_TIERS["lite"]
        if scene in {"simple_query", "greeting"}:
            return MODEL_TIERS["lite"]
        return MODEL_TIERS["full"]

    async def precompute_hot_queries(self, execute_fn: Any) -> None:
        """输入：异步工具执行函数 ``execute_fn(tool_name, **params)``。

        输出：无；成功的热门统计结果写入一小时 Redis 缓存，单项失败时继续处理后续项。
        功能：供凌晨六点 Cron 调用，在业务高峰前预热固定退单统计，命中时可跳过 LLM 与 MCP。
        """

        for query in HOT_QUERIES:
            try:
                result = await execute_fn(query["tool"], **query["params"])
                self.redis.setex(
                    self._precomputed_key(query["scene"], query["params"]),
                    3600,
                    json.dumps(result, ensure_ascii=False, default=str),
                )
            except Exception:
                continue

    def get_precomputed(
        self, scene: str, params: dict[str, Any], user_query: str = ""
    ) -> Any | None:
        """输入：查询场景 ``scene``、已标准化工具参数 ``params`` 和可选用户问题 ``user_query``。

        输出：反序列化后的预计算结果；不存在或过期时返回 ``None``，缓存 JSON 无效时抛出 ``JSONDecodeError``。
        功能：使用与预热任务一致的键读取新鲜统计结果，供请求链路提前返回。
        """

        cache_params = self._hot_query_params(scene, user_query) or params
        cached = self.redis.get(self._precomputed_key(scene, cache_params))
        return None if cached is None else json.loads(self._decode(cached))

    def should_compress(self, context: dict[str, Any], threshold_tokens: int = 15000) -> bool:
        """输入：包含 Prompt、近期轮次和历史摘要的 ``context``，以及 Token 阈值 ``threshold_tokens``。

        输出：估算 Token 严格超过阈值时返回 ``True``。
        功能：在调用完整模型前识别过长会话，避免上下文膨胀持续推高耗时和成本。
        """

        return self._estimate_tokens(context) > threshold_tokens

    def _estimate_tokens(self, context: dict[str, Any]) -> int:
        """输入：含字符串 Prompt、轮次和摘要的上下文字典 ``context``。

        输出：按约 1.5 个字符一个 Token 得到的整数估算值。
        功能：以本地字符计数替代额外模型调用，为压缩判断和成本统计提供快速近似值。
        """

        total_chars = len(str(context.get("system_prompt", "")))
        for turn in context.get("recent_turns", []):
            total_chars += len(str(turn.get("user_msg", "")))
            total_chars += len(str(turn.get("assistant_reply", "")))
        total_chars += sum(len(str(summary)) for summary in context.get("compressed_history", []))
        return int(total_chars / 1.5)

    async def optimized_inference(
        self,
        user_query: str,
        scene: str,
        complexity: float,
        context: dict[str, Any],
        **params: Any,
    ) -> tuple[str, InferenceStats]:
        """输入：用户问题、路由场景与置信度、会话上下文及已标准化的查询参数。

        输出：自然语言回复和本次 ``InferenceStats``；聊天客户端调用失败时透传异常。
        功能：依次尝试预计算结果、Prompt 缓存、模型分层和超过 15K Token 的上下文压缩，再执行一次聊天推理。
        """

        precomputed = self.get_precomputed(scene, params, user_query)
        if precomputed is not None:
            return self._format_result(precomputed), InferenceStats(
                model_used="cache",
                tokens_input=0,
                tokens_output=0,
                latency_ms=1,
                precomputed=True,
            )
        system_prompt, cache_hit = self.get_or_cache_system_prompt(
            scene, SCENE_PROMPTS.get(scene, SCENE_PROMPTS["default"]), **params
        )
        if self.should_compress(context):
            self._compress_context(context)
        full_prompt = self._build_prompt(system_prompt, user_query, context)
        model = self.select_model(scene, complexity)
        started_at = time.perf_counter()
        response = await self.llm.chat(
            full_prompt,
            model=model.name,
            max_tokens=500,
        )
        latency_ms = (time.perf_counter() - started_at) * 1000
        return response, InferenceStats(
            model_used=model.name,
            tokens_input=self._estimate_tokens({"system_prompt": full_prompt}),
            tokens_output=self._estimate_tokens(
                {"recent_turns": [{"assistant_reply": response}]}
            ),
            latency_ms=latency_ms,
            cache_hit=cache_hit,
        )

    def _compress_context(self, context: dict[str, Any]) -> None:
        """输入：Token 已超阈值的可变会话上下文 ``context``。

        输出：无；早期轮次原地归纳为一条摘要，仅保留最后三轮。
        功能：复用短期会话的最近三轮保留语义，以低成本占位摘要截断过长历史。
        """

        turns = context.get("recent_turns", [])
        if len(turns) <= 4:
            return
        compressed_count = len(turns) - 3
        topic = context.get("slots", {}).get("topic", "")
        summary = f"已完成的{compressed_count}轮对话中，用户主要咨询了{topic}相关问题。"
        context["compressed_history"] = context.get("compressed_history", []) + [summary]
        context["recent_turns"] = turns[-3:]

    @staticmethod
    def _format_result(data: Any) -> str:
        """输入：预计算缓存中的可 JSON 序列化结果 ``data``。

        输出：保持中文字符的 JSON 文本；不可序列化对象使用其字符串表示。
        功能：在跳过模型时提供稳定的直接回复，避免为了格式化而额外调用 LLM。
        """

        return json.dumps(data, ensure_ascii=False, default=str)

    @staticmethod
    def _decode(value: Any) -> str:
        """输入：Redis 返回的 ``str``、``bytes`` 或兼容值 ``value``。

        输出：UTF-8 解码后的字符串。
        功能：兼容 Redis 客户端的两种常见响应类型，统一后续 JSON 和 Prompt 处理。
        """

        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    @staticmethod
    def _build_prompt(system_prompt: str, user_query: str, context: dict[str, Any]) -> str:
        """输入：System Prompt ``system_prompt``、用户问题 ``user_query`` 与会话上下文 ``context``。

        输出：供聊天模型调用的完整 Prompt 文本。
        功能：只注入最近三条历史摘要和当前问题，防止压缩前的完整历史再次进入上下文窗口。
        """

        parts = [system_prompt]
        if context.get("compressed_history"):
            parts.extend(["历史会话摘要:", *context["compressed_history"][-3:]])
        parts.append(f"用户: {user_query}")
        return "\n\n".join(parts)

    @staticmethod
    def _prompt_key(
        scene: str, prompt_template: str, params: dict[str, Any]
    ) -> str:
        """输入：场景 ``scene``、Prompt 模板 ``prompt_template`` 和渲染参数 ``params``。

        输出：固定长度散列组成的 Prompt Redis 键。
        功能：保证参数顺序不影响缓存命中，并在 Prompt 版本变更时自动失效旧缓存。
        """

        return f"prompt:{scene}:" + hashlib.sha256(
            json.dumps([prompt_template, params], sort_keys=True, default=str).encode()
        ).hexdigest()[:16]

    @staticmethod
    def _precomputed_key(scene: str, params: dict[str, Any]) -> str:
        """输入：场景 ``scene`` 和已标准化工具参数 ``params``。

        输出：固定长度散列组成的预计算 Redis 键。
        功能：让预热写入和在线读取对同一查询生成完全一致的缓存位置。
        """

        return f"precomputed:{scene}:" + hashlib.md5(
            json.dumps(params, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]

    @staticmethod
    def _hot_query_params(scene: str, user_query: str) -> dict[str, Any] | None:
        """输入：路由场景 ``scene`` 和用户原始问题 ``user_query``。

        输出：匹配热点表达时对应的工具参数；不属于固定热点时返回 ``None``。
        功能：将等价的自然语言热点问题映射到预热写入使用的同一缓存键，避免在线参数不全导致漏命中。
        """

        normalized_query = "".join(str(user_query).split()).rstrip("？?。！!")
        for hot_query in HOT_QUERIES:
            if hot_query["scene"] != scene:
                continue
            if normalized_query in hot_query["queries"]:
                return dict(hot_query["params"])
        return None


class InferenceBenchmark:
    """运行固定用例并汇总推理延迟、Token 与缓存命中率。"""

    def __init__(self, optimizer: InferenceOptimizer) -> None:
        """输入：已配置依赖的推理优化器 ``optimizer``。

        输出：保存基准测试所用优化器。
        功能：为不同路由场景复用同一真实优化链路，避免基准与线上行为分叉。
        """

        self.optimizer = optimizer

    async def benchmark(self, test_cases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """输入：含场景、问题、可选置信度和参数的基准用例列表 ``test_cases``。

        输出：每个场景的平均延迟、Token、模型和缓存命中率；空列表返回空字典。
        功能：每个用例执行三次并汇总指标，便于验证四层优化是否持续满足性能目标。
        """

        results: dict[str, dict[str, Any]] = {}
        for case in test_cases:
            stats_list: list[InferenceStats] = []
            for _ in range(3):
                _, stats = await self.optimizer.optimized_inference(
                    user_query=case["query"],
                    scene=case["scene"],
                    complexity=case.get("complexity", 0.5),
                    context={},
                    **case.get("params", {}),
                )
                stats_list.append(stats)
            results[case["scene"]] = {
                "avg_latency_ms": sum(stat.latency_ms for stat in stats_list) / 3,
                "avg_tokens": sum(
                    stat.tokens_input + stat.tokens_output for stat in stats_list
                )
                / 3,
                "model": stats_list[0].model_used,
                "cache_hit_rate": sum(stat.cache_hit for stat in stats_list) / 3,
            }
        return results
