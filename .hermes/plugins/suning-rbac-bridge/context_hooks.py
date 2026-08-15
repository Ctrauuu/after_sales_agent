"""构建并注册苏宁结构化会话上下文 Hooks。"""

from __future__ import annotations

import os
from typing import Any

import redis
from sqlalchemy import URL, create_engine
from suning_context_runtime import (
    ContextManager,
    ConversationHooks,
    DatabaseWhitelistLoader,
    SlotExtractor,
    create_model,
)


def _required_env(name: str) -> str:
    """输入：插件运行所需的环境变量名 ``name``。

    输出：去除首尾空白后的配置值；缺失时抛出 ``RuntimeError``。
    功能：在 Hook 注册阶段阻止不完整的 Redis 或数据库配置静默失效。
    """

    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}")
    return value


def build_conversation_runtime() -> tuple[ConversationHooks, Any, Any]:
    """输入：隐式读取 Redis、MySQL、DeepSeek 和置信度环境变量。

    输出：短期上下文 Hooks、同一 Redis 客户端和同一 MySQL Engine。
    功能：一次装配会话槽位与身份路由共用的基础设施，避免插件为同一请求重复创建连接池。
    """

    redis_client = redis.Redis.from_url(
        _required_env("REDIS_URL"),
        decode_responses=False,
    )
    database_url = URL.create(
        "mysql+pymysql",
        username=_required_env("MYSQL_USER"),
        password=_required_env("MYSQL_PASSWORD"),
        host=_required_env("MYSQL_HOST"),
        port=int(_required_env("MYSQL_PORT")),
        database=_required_env("MYSQL_DATABASE"),
        query={"charset": "utf8mb4"},
    )
    database_engine = create_engine(
        database_url,
        pool_pre_ping=True,
        pool_recycle=1800,
    )
    model = create_model(
        api_key=(
            os.getenv("DEEPSEEK_API", "")
            or os.getenv("DEEPSEEK_API_KEY", "")
        ),
        base_url=os.getenv("DEEPSEEK_BASE_URL") or None,
        model=os.getenv("DEEPSEEK_MODEL") or None,
        timeout_seconds=float(
            os.getenv("CONTEXT_LLM_TIMEOUT_SECONDS", "15")
        ),
        max_tokens=600,
    )
    manager = ContextManager(redis_client, summary_model=model)
    whitelist_loader = DatabaseWhitelistLoader(
        database_engine,
        cache_ttl_seconds=float(
            os.getenv("CONTEXT_WHITELIST_TTL_SECONDS", "300")
        ),
    )
    extractor = SlotExtractor(
        model,
        whitelist_loader,
        confidence_threshold=float(
            os.getenv("CONTEXT_SLOT_CONFIDENCE_THRESHOLD", "0.7")
        ),
    )
    return ConversationHooks(manager, extractor), redis_client, database_engine


def build_conversation_hooks() -> ConversationHooks:
    """输入：隐式读取 Redis、MySQL、DeepSeek 和置信度环境变量。

    输出：绑定共享运行时、数据库白名单和 LangChain 模型的 Hermes Hooks。
    功能：保留既有公共构造入口，并复用统一运行时装配的短期会话 Hooks。
    """

    return build_conversation_runtime()[0]


def register_context_hooks(
    plugin_context: Any,
    hooks: ConversationHooks | None = None,
) -> ConversationHooks:
    """输入：Hermes 插件上下文和可选的已构建 Hook 集合。

    输出：注册完成的 ``ConversationHooks``，便于测试和运行时观测。
    功能：把回答前槽位注入与回答后轮次保存接入 Hermes 生命周期。
    """

    resolved_hooks = hooks or build_conversation_hooks()
    plugin_context.register_hook("pre_llm_call", resolved_hooks.pre_llm_call)
    plugin_context.register_hook("post_llm_call", resolved_hooks.post_llm_call)
    return resolved_hooks


__all__ = [
    "build_conversation_hooks",
    "build_conversation_runtime",
    "register_context_hooks",
]
