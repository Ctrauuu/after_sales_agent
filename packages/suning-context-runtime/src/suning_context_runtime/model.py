"""使用 LangChain 统一创建和调用 DeepSeek 聊天模型。"""

from __future__ import annotations

import os
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import SecretStr


DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_LLM_TIMEOUT_SECONDS = 30.0


def create_model(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    timeout_seconds: float | None = None,
    max_tokens: int = 1200,
) -> BaseChatModel:
    """输入：可覆盖环境变量的 DeepSeek 密钥、地址、模型、超时和输出上限。

    输出：由 LangChain ``init_chat_model`` 创建的 DeepSeek 聊天模型；缺少密钥时抛出 ``RuntimeError``。
    功能：集中解析项目配置并构造可复用于 NL2SQL、槽位提取和会话摘要的模型实例。
    """

    resolved_api_key = (
        api_key
        if api_key is not None
        else os.getenv("DEEPSEEK_API", "")
        or os.getenv("DEEPSEEK_API_KEY", "")
    ).strip()
    if not resolved_api_key:
        raise RuntimeError("未配置 DEEPSEEK_API 或 DEEPSEEK_API_KEY，无法调用模型")

    resolved_base_url = (
        base_url
        if base_url is not None
        else os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL)
    ).rstrip("/")
    resolved_model = (
        model
        if model is not None
        else os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL)
    ).strip() or DEFAULT_DEEPSEEK_MODEL
    configured_timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else os.getenv(
            "NL2SQL_TIMEOUT_SECONDS",
            str(DEFAULT_LLM_TIMEOUT_SECONDS),
        )
    )
    return init_chat_model(
        resolved_model,
        model_provider="deepseek",
        api_key=SecretStr(resolved_api_key),
        base_url=resolved_base_url,
        temperature=0.0,
        timeout=max(1.0, float(configured_timeout)),
        max_tokens=max(1, int(max_tokens)),
        max_retries=2,
        extra_body={"thinking": {"type": "disabled"}},
    )


def message_content_to_text(content: Any) -> str:
    """输入：LangChain 消息的字符串或多内容块 ``content``。

    输出：按原顺序拼接并去除首尾空白的文本；无可用文本时返回空字符串。
    功能：兼容不同模型提供方的纯文本和内容块响应，统一供业务调用方校验。
    """

    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts).strip()


def invoke_model_text(
    model: BaseChatModel,
    system_prompt: str,
    user_prompt: str,
) -> str:
    """输入：LangChain 聊天模型、系统指令和用户内容。

    输出：模型返回的非空文本；空响应时抛出 ``RuntimeError``。
    功能：使用标准 System/Human 消息调用模型并规范化不同响应内容格式。
    """

    response = model.invoke(
        [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
    )
    text = message_content_to_text(response.content)
    if not text:
        raise RuntimeError("模型没有返回文本内容")
    return text


__all__ = [
    "DEFAULT_DEEPSEEK_BASE_URL",
    "DEFAULT_DEEPSEEK_MODEL",
    "DEFAULT_LLM_TIMEOUT_SECONDS",
    "create_model",
    "invoke_model_text",
    "message_content_to_text",
]
