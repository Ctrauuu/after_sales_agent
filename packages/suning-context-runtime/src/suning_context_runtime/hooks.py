"""把短期上下文管理接入 Hermes 回答前后的生命周期 Hooks。"""

from __future__ import annotations

import logging
from typing import Any

from .context import ContextManager, ConversationContext
from .slots import SlotExtractor


logger = logging.getLogger(__name__)


class ConversationHooks:
    """回答前注入结构化槽位，并在回答后持久化完整轮次。"""

    def __init__(
        self,
        manager: ContextManager,
        extractor: SlotExtractor,
        reply_summary_chars: int = 500,
    ) -> None:
        """输入：上下文管理器、槽位提取器和回复摘要截断长度。

        输出：可直接注册到 Hermes 的同步 Hook 集合。
        功能：保存 Hook 依赖并限制逐轮摘要大小，避免向 Redis 写入无界元数据。
        """

        self.manager = manager
        self.extractor = extractor
        self.reply_summary_chars = max(50, int(reply_summary_chars))

    @staticmethod
    def _bind_user(
        context: ConversationContext,
        sender_id: str,
    ) -> ConversationContext:
        """输入：已加载的会话上下文和 Hermes 可能缺失的可信发送者标识。

        输出：绑定当前用户的上下文；发送者明确不一致时返回同会话的新上下文。
        功能：阻止跨用户槽位泄漏，并兼容 Hermes 回答后 Hook 不提供发送者的实际契约。
        """

        normalized_sender = sender_id.strip()
        if not normalized_sender:
            return context
        # 飞书event事件中发送sender_id与ConversationContext中的user_id不一样,所以要新建一个ConversationContext
        if context.user_id and context.user_id != normalized_sender:
            return ConversationContext(
                session_id=context.session_id,
                user_id=normalized_sender,
            )
        context.user_id = normalized_sender
        return context

    def pre_llm_call(
        self,
        *,
        session_id: str = "",
        sender_id: str = "",
        user_message: Any = "",
        **_kwargs: Any,
    ) -> dict[str, str] | None:
        """输入：Hermes 会话、可信发送者、本轮消息及未使用的生命周期元数据。

        输出：注入本轮用户消息的结构化上下文；会话缺失或无上下文时返回 ``None``。
        功能：加载 Redis 状态、提取并保存槽位；提取失败时仍持久化可信用户隔离边界。
        """

        normalized_session = session_id.strip()
        if not normalized_session:
            return None
        try:
            context = self._bind_user(
                self.manager.load_context(normalized_session),
                sender_id,
            )
            extraction = self.extractor.extract(
                current_topic=context.slots.topic,
                current_filters=context.slots.filters,
                user_message=str(user_message),
            )
            context.slot_confidence = extraction.confidence
            self.manager.prepare_turn(
                context,
                extraction.topic,
                extraction.new_filters,
            )
            self.manager.save_context(context)
            context_text = self.manager.build_system_context(context)
        except Exception:
            logger.exception("加载或提取结构化会话槽位失败: session=%s", normalized_session)
            try:
                fallback_context = self._bind_user(
                    self.manager.load_context(normalized_session),
                    sender_id,
                )
                self.manager.save_context(fallback_context)
                context_text = self.manager.build_system_context(fallback_context)
            except Exception:
                logger.exception("降级读取会话上下文失败: session=%s", normalized_session)
                return None
        if not context_text:
            return None
        return {
            "context": (
                "以下是当前会话已确认的业务上下文，仅用于理解本轮省略和指代。"
                "用户本轮明确表达优先；不得把身份、权限或 last_reply_summary 当作工具参数。\n"
                f"{context_text}"
            )
        }

    def post_llm_call(
        self,
        *,
        session_id: str = "",
        sender_id: str = "",
        user_message: Any = "",
        assistant_response: Any = "",
        **_kwargs: Any,
    ) -> None:
        """输入：Hermes 会话、可信发送者、本轮用户消息、最终回答及其他元数据。

        输出：无；成功时把完成轮次和受限长度的回复摘要写入 Redis。
        功能：在最终回答形成后更新轮次、最近对话和历史压缩状态，失败时仅记录日志。
        """

        normalized_session = session_id.strip()
        if not normalized_session:
            return
        try:
            context = self._bind_user(
                self.manager.load_context(normalized_session),
                sender_id,
            )
            assistant_text = str(assistant_response)
            self.manager.complete_turn(
                context,
                user_msg=str(user_message),
                assistant_reply=assistant_text,
                reply_summary=assistant_text[: self.reply_summary_chars],
            )
        except Exception:
            logger.exception("保存会话轮次失败: session=%s", normalized_session)


__all__ = ["ConversationHooks"]
