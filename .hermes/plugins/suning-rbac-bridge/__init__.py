"""苏宁业务 Hermes 插件入口。"""

from __future__ import annotations

import logging

from .bridge import make_handler
from .charting import CHART_TOOL_SCHEMA, handle_chart
from .mcp_resilience import MCPCallManager
from . import observability
from .orchestration import COMPLEX_ANALYSIS_SCHEMA, make_complex_analysis_handler
from .schemas import TOOL_SPECS
from .tool_governor import ToolGovernor


logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """输入：Hermes 插件上下文 ``ctx``。

    输出：无；注册共享 Manager 的工具，并在配置完整时注册各层 Hooks。
    功能：复用单个 Redis Client 装配 MCP 熔断；可选上下文依赖故障时仍保留业务工具。
    """

    redis_client = None
    build_conversation_runtime = None
    try:
        from .context_hooks import build_conversation_runtime, build_redis_client

        redis_client = build_redis_client()
    except (ImportError, RuntimeError, ValueError):
        logger.exception("Redis 配置不完整，MCP 熔断已 fail-open 并跳过上下文 Hooks")

    call_manager = MCPCallManager(redis_client)
    tool_governor = ToolGovernor(redis_client)
    for tool_name, spec in TOOL_SPECS.items():
        ctx.register_tool(
            name=tool_name,
            toolset="suning_business",
            schema=spec.schema,
            handler=make_handler(tool_name, call_manager, tool_governor),
            is_async=True,
            description=str(spec.schema["description"]),
            emoji="🛡️",
        )
    ctx.register_tool(
        name="send_aftersale_chart",
        toolset="suning_business",
        schema=CHART_TOOL_SCHEMA,
        handler=handle_chart,
        is_async=True,
        description=str(CHART_TOOL_SCHEMA["description"]),
        emoji="📊",
    )
    ctx.register_tool(
        name="orchestrate_aftersale_analysis",
        toolset="suning_business",
        schema=COMPLEX_ANALYSIS_SCHEMA,
        handler=make_complex_analysis_handler(ctx.llm, ctx.subagent_lifecycle),
        is_async=True,
        description=str(COMPLEX_ANALYSIS_SCHEMA["description"]),
        emoji="🧭",
    )
    if build_conversation_runtime is None or redis_client is None:
        return
    try:
        conversation_hooks, redis_client, database_engine = build_conversation_runtime(
            redis_client
        )
    except (ImportError, RuntimeError, ValueError):
        logger.exception("结构化上下文 Hooks 配置不完整，已保留 RBAC 工具并跳过 Hooks")
        return

    memory_hooks = None
    try:
        from .memory_hooks import build_memory_hooks

        memory_hooks = build_memory_hooks(conversation_hooks.manager)
    except Exception:
        logger.exception("长期记忆 Hooks 初始化失败，已保留 RBAC 工具和短期上下文 Hooks")

    knowledge_hooks = None
    try:
        from .knowledge_hooks import build_knowledge_hooks

        knowledge_hooks = build_knowledge_hooks(conversation_hooks.manager, database_engine)
    except Exception:
        logger.exception("售后知识库 Hook 初始化失败，已保留已有业务能力")

    skill_hooks = None
    try:
        from .skill_evolution import build_skill_evolution_hooks

        skill_hooks = build_skill_evolution_hooks(
            ctx.llm,
            set(TOOL_SPECS) | {"send_aftersale_chart", "orchestrate_aftersale_analysis"},
        )
    except Exception:
        logger.exception("Skill 自进化 Hook 初始化失败，已保留已有业务能力")

    from .identity_session import IdentitySessionRouter, UnifiedIdentityHooks

    identity_hooks = UnifiedIdentityHooks(
        IdentitySessionRouter(database_engine, redis_client),
        conversation_hooks,
        memory_hooks,
        knowledge_hooks,
        skill_hooks,
        observability,
        tool_governor,
    )

    ctx.register_hook("pre_llm_call", identity_hooks.pre_llm_call)
    ctx.register_hook("pre_tool_call", identity_hooks.pre_tool_call)
    ctx.register_hook("post_llm_call", identity_hooks.post_llm_call)
    ctx.register_hook("pre_api_request", identity_hooks.pre_api_request)
    ctx.register_hook("post_api_request", identity_hooks.post_api_request)
    ctx.register_hook("api_request_error", identity_hooks.api_request_error)
    ctx.register_hook("transform_llm_output", identity_hooks.transform_llm_output)
    ctx.register_hook("on_session_end", identity_hooks.on_session_end)


__all__ = ["register"]
