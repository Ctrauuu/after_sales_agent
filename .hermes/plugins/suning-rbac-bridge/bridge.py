"""读取 Hermes 当前发送者，签发短期凭证并调用私有 MCP。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from collections.abc import Callable, Coroutine
from typing import Any

from gateway.session_context import get_session_env  # type: ignore
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from tools.registry import tool_error, tool_result  # type: ignore

from .observability import _result_rows, observability
from .schemas import TOOL_SPECS


logger = logging.getLogger(__name__)
TOKEN_VERSION = "v1"
TOKEN_AUDIENCE = "suning-mcp"
TOKEN_ISSUER = "suning-rbac-bridge"
TOKEN_TTL_SECONDS = 30
MIN_SECRET_BYTES = 32
TRUSTED_PLATFORMS = {"feishu", "wecom", "dingtalk"}
DIRECT_CHAT_TYPES = {"dm", "direct", "private", "p2p"}
GROUP_CHAT_TYPES = {"group", "channel", "forum", "thread"}


class BridgeConfigurationError(RuntimeError):
    pass


def _required_env(name: str) -> str:
    """输入：必需环境变量名称 ``name``。

    输出：去除首尾空白后的环境变量值；缺失时抛出配置异常。
    功能：集中执行插件关键配置的必填校验。
    """
    value = os.getenv(name, "").strip()
    if not value:
        raise BridgeConfigurationError(f"缺少环境变量 {name}")
    return value


def _b64url_encode(value: bytes) -> str:
    """输入：待编码的原始字节 ``value``。

    输出：不含填充符的 Base64URL 字符串。
    功能：生成身份凭证载荷和签名使用的 URL 安全文本。
    """
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _canonical_json(value: Any) -> bytes:
    """输入：可 JSON 序列化的 ``value``。

    输出：键排序、无多余空白的 UTF-8 JSON 字节。
    功能：固定 HMAC 签名载荷的序列化格式。
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def current_identity() -> dict[str, str]:
    """输入：无；隐式读取 Hermes 当前请求的 ContextVar。

    输出：包含平台、外部用户、会话类型和消息 ID 的可信身份字典。
    功能：从 Hermes 请求上下文取得真实发送者并规范化会话类型，不接受工具参数。
    """

    identity = {
        "platform": get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower(),
        "external_subject": get_session_env("HERMES_SESSION_USER_ID", "").strip(),
        "chat_type": get_session_env("HERMES_SESSION_CHAT_TYPE", "").strip().lower(),
        "message_id": get_session_env("HERMES_SESSION_MESSAGE_ID", "").strip(),
    }
    if not identity["platform"] or not identity["external_subject"]:
        raise PermissionError("当前请求没有可信的 Hermes 会话身份")
    if identity["platform"] not in TRUSTED_PLATFORMS:
        raise PermissionError(f"不支持的消息平台: {identity['platform']}")
    if len(identity["external_subject"]) > 128:
        raise PermissionError("当前请求的外部用户身份格式无效")
    if identity["chat_type"] in DIRECT_CHAT_TYPES:
        identity["chat_type"] = "dm"
    elif identity["chat_type"] not in GROUP_CHAT_TYPES:
        raise PermissionError("当前请求没有可信的会话类型")
    return identity


def mint_attestation(*, tool_name: str, identity: dict[str, str]) -> str:
    """输入：目标 ``tool_name`` 和经过校验的 ``identity``。

    输出：30 秒有效、绑定单个工具的一次性 HMAC 凭证字符串。
    功能：把可信发送者身份签名，供私有 MCP 验证调用来源和目标工具。

    防篡改：别人把 sub=用户A 改成 sub=管理员，签名就对不上。
    短期有效：比如 30 秒后 token 失效，减少被截获后重复利用的风险。
    绑定当前调用：token 里写了 tool_name，给 search_orders 的凭证不能随便拿去调用其他工具。
    """

    secret = _required_env("SUNING_MCP_BRIDGE_SECRET").encode()
    if len(secret) < MIN_SECRET_BYTES:
        raise BridgeConfigurationError("SUNING_MCP_BRIDGE_SECRET 长度不足 32 字节")

    now = int(time.time())
    claims = {
        "iss": TOKEN_ISSUER,
        "aud": TOKEN_AUDIENCE,
        "identity_issuer": _required_env("SUNING_IDENTITY_ISSUER"),
        "platform": identity["platform"],
        "sub": identity["external_subject"],
        "chat_type": identity["chat_type"],
        "message_id": identity["message_id"],
        "tool": tool_name,
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
        "jti": secrets.token_urlsafe(18),
    }
    payload = _b64url_encode(_canonical_json(claims))
    signing_input = f"{TOKEN_VERSION}.{payload}".encode("ascii")
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    return f"{TOKEN_VERSION}.{payload}.{_b64url_encode(signature)}"


def _result_text(result: Any) -> str:
    """输入：MCP Client 返回的 ``result``。

    输出：所有非空文本内容块拼接成的字符串。
    功能：从不同 MCP 返回结构中提取可读错误或普通文本。
    """
    return "\n".join(
        text.strip()
        for block in (getattr(result, "content", None) or [])
        if isinstance((text := getattr(block, "text", None)), str) and text.strip()
    )


def _successful_result(result: Any) -> str:
    """输入：成功的 MCP 调用结果 ``result``。

    输出：Hermes registry 接受的 ``tool_result`` 字符串。
    功能：优先转发结构化内容，并兼容 JSON 文本和普通内容块。
    """
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return tool_result(structured)

    text = _result_text(result)
    if text:
        try:
            return tool_result(json.loads(text))
        except json.JSONDecodeError:
            return tool_result({"text": text})
    return tool_result(
        {
            "content": [
                block.model_dump(exclude_none=True)
                if hasattr(block, "model_dump")
                else str(block)
                for block in (getattr(result, "content", None) or [])
            ]
        }
    )


async def invoke_business_tool(tool_name: str, arguments: dict[str, Any]) -> str:
    """输入：白名单工具名 ``tool_name`` 和模型生成的业务参数 ``arguments``。

    输出：Hermes 可消费的成功结果或安全错误结果。
    功能：读取真实身份、签发凭证，并通过插件内 MCP Client 调用私有服务。
    """
    spec = TOOL_SPECS.get(tool_name)
    if spec is None:
        return tool_error(f"未注册的苏宁业务工具: {tool_name}")

    try:
        identity = current_identity()
        attestation = mint_attestation(tool_name=tool_name, identity=identity)
    except PermissionError as exc:
        return tool_error(str(exc))
    except BridgeConfigurationError:
        logger.exception("苏宁业务身份桥接配置不完整")
        return tool_error("苏宁业务身份桥接配置不完整，请联系管理员")

    trace_record, owns_trace = observability.ensure_trace(
        conversation_id=identity["message_id"] or f"mcp:{tool_name}",
        user_id=identity["external_subject"],
        platform=identity["platform"],
        query=tool_name,
    )
    endpoint = os.getenv(spec.endpoint_env, spec.default_endpoint).strip()
    span = observability.start_mcp_span(tool_name, endpoint)
    success = False
    rows_returned = 0
    error = ""
    # streamable_http_client
    #      ↓
    # 负责建立 HTTP 通信连接

    # ClientSession
    #      ↓
    # 在这个连接上提供 MCP 协议操作
    try:
        async with streamable_http_client(endpoint) as (read, write, _session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                metadata = {
                    "suning/authn": attestation,
                    "suning/trace_id": trace_record.trace_id,
                }
                observability.inject_trace_metadata(metadata)
                result = await session.call_tool(
                    tool_name,
                    arguments=arguments,
                    meta=metadata,
                )
        if bool(getattr(result, "isError", False)):
            error = _result_text(result) or "苏宁业务服务拒绝了本次请求"
            return tool_error(error)
        success = True
        rows_returned = _result_rows(result)
        return _successful_result(result)
    except Exception as exc:
        error = str(exc)
        logger.exception("调用苏宁 MCP 失败: tool=%s endpoint=%s", tool_name, endpoint)
        return tool_error("苏宁业务服务暂时不可用，请稍后重试")
    finally:
        observability.end_mcp_span(
            span,
            rows_returned=rows_returned,
            success=success,
            error=error,
        )
        if owns_trace:
            observability.finish_trace(trace_record, "MCP 调用完成" if success else "MCP 调用失败")


Handler = Callable[[dict[str, Any]], Coroutine[Any, Any, str]]


def make_handler(tool_name: str) -> Handler:
    """输入：需要绑定的白名单工具名 ``tool_name``。

    输出：符合 Hermes registry 调用约定的异步 Handler。
    功能：为每个工具创建闭包，将调用统一转发给身份桥接入口。
    """

    async def handler(args: dict[str, Any], **_kwargs: Any) -> str:
        """输入：模型业务参数 ``args``；其余 Hermes 参数由 ``_kwargs`` 接收。

        输出：私有 MCP 调用转换后的 Hermes 工具结果。
        功能：执行当前 ``tool_name`` 对应的身份桥接调用。
        """
        return await invoke_business_tool(tool_name, dict(args or {}))

    return handler


__all__ = [
    "BridgeConfigurationError",
    "current_identity",
    "invoke_business_tool",
    "make_handler",
    "mint_attestation",
]
