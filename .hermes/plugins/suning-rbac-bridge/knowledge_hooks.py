"""在回答政策、保修和维修条款问题前注入带引用的售后知识片段。"""

from __future__ import annotations

import logging
import os
from typing import Any

from suning_context_runtime import (
    ContextManager,
    DashScopeEmbeddingClient,
    AftersaleKnowledgeGraph,
    KnowledgeMilvusStore,
    KnowledgeRAG,
    QueryRouter,
    extract_entities,
    format_graph_context,
    format_knowledge_context,
    should_search_knowledge,
)


logger = logging.getLogger(__name__)


def _context_entities(context: Any) -> dict[str, str]:
    """输入：Redis 恢复的会话上下文 ``context`` 或兼容对象。

    输出：当前已确认的品类和品牌槽位字典。
    功能：为“这个品牌呢”等省略式政策追问补全检索实体，但不从普通筛选条件推断新事实。
    """

    filters = dict(getattr(getattr(context, "slots", None), "filters", {}) or {})
    return {
        key: str(filters[key]).strip()
        for key in ("category", "brand")
        if str(filters.get(key) or "").strip()
    }


class KnowledgeHooks:
    """只在知识类提问前执行检索并注入引用上下文。"""

    def __init__(self, manager: ContextManager, rag: KnowledgeRAG, graph_engine: Any | None = None) -> None:
        """输入：共享会话上下文管理器 ``manager``、已装配知识检索器 ``rag`` 和可选 MySQL Engine。

        输出：可注册到 Hermes ``pre_llm_call`` 的 Hook 对象。
        功能：保存现有会话槽位与 RAG 依赖；提供 Engine 时额外装配图谱路由，不在插件加载阶段查询外部服务。
        """

        self.manager = manager
        self.rag = rag
        self.router = QueryRouter(AftersaleKnowledgeGraph(graph_engine), rag) if graph_engine else None

    def pre_llm_call(
        self,
        *,
        session_id: str = "",
        user_message: Any = "",
        **_kwargs: Any,
    ) -> dict[str, str] | None:
        """输入：Hermes 会话 ID、本轮用户消息及其他生命周期元数据。

        输出：带来源编号的知识上下文；非知识问题、无命中或故障时返回 ``None``。
        功能：识别政策类问题，复用当前槽位补全实体，检索 Top3 片段并让后续 LLM 强制引用原文来源。
        """

        query = str(user_message or "").strip()
        if not should_search_knowledge(query):
            return None
        try:
            context = self.manager.load_context(session_id.strip()) if session_id.strip() else None
            entities = extract_entities(query, _context_entities(context))
            if self.router is not None:
                rendered = format_graph_context(self.router.route_and_query(query, entities))
            else:
                rendered = format_knowledge_context(self.rag.search(query, entities))
            return {"context": rendered} if rendered else None
        except Exception:
            logger.exception("售后知识库检索失败: session=%s", session_id)
            return None


def build_knowledge_hooks(manager: ContextManager, graph_engine: Any | None = None) -> KnowledgeHooks:
    """输入：已由短期上下文 Hook 使用的共享 ``ContextManager`` 和可选 MySQL Engine。

    输出：读取环境变量后装配完成的 ``KnowledgeHooks``；缺少 Embedding 密钥时抛出 ``RuntimeError``。
    功能：复用现有 DashScope、Milvus 和 MySQL 连接，为精确图查询与 RAG 降级提供同一个入口。
    """

    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 DASHSCOPE_API_KEY，无法启用售后知识库")
    timeout = float(os.getenv("KNOWLEDGE_TIMEOUT_SECONDS", "10"))
    embedder = DashScopeEmbeddingClient(
        api_key=api_key,
        model=os.getenv("KNOWLEDGE_EMBEDDING_MODEL", "text-embedding-v3"),
        timeout_seconds=timeout,
    )
    store = KnowledgeMilvusStore(
        uri=os.getenv("MILVUS_URI", "http://127.0.0.1:19530"),
        collection_name=os.getenv("KNOWLEDGE_MILVUS_COLLECTION", "knowledge_chunks"),
        timeout_seconds=timeout,
    )
    rag = KnowledgeRAG(store, embedder, top_k=int(os.getenv("KNOWLEDGE_TOP_K", "3")))
    return KnowledgeHooks(manager, rag, graph_engine)


def register_knowledge_hooks(
    plugin_context: Any,
    manager: ContextManager,
    hooks: KnowledgeHooks | None = None,
    graph_engine: Any | None = None,
) -> KnowledgeHooks:
    """输入：Hermes 插件上下文、共享会话管理器、可选已构建 Hook 和 MySQL Engine。

    输出：完成 ``pre_llm_call`` 注册的 ``KnowledgeHooks``。
    功能：在短期上下文和长期记忆之后追加图谱/RAG 路由，使检索可复用已更新的会话品类与品牌槽位。
    """

    resolved_hooks = hooks or build_knowledge_hooks(manager, graph_engine)
    plugin_context.register_hook("pre_llm_call", resolved_hooks.pre_llm_call)
    return resolved_hooks


__all__ = ["KnowledgeHooks", "build_knowledge_hooks", "register_knowledge_hooks"]
