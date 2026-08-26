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

from .mcp_resilience import (
    DegradeLevel,
    FailureType,
    MCPCallManager,
    MCPCallResult,
)
from . import observability
from .observability import result_rows
from .schemas import TOOL_SPECS
from .tool_governor import ToolGovernor


logger = logging.getLogger(__name__)
TOKEN_VERSION = "v1"
TOKEN_AUDIENCE = "suning-mcp"
TOKEN_ISSUER = "suning-rbac-bridge"
TOKEN_TTL_SECONDS = 30
MIN_SECRET_BYTES = 32
TRUSTED_PLATFORMS = {"feishu", "wecom", "dingtalk"}
CRON_PLATFORM = "cron"
CRON_SERVICE_SUBJECT_ENV = "SUNING_CRON_SERVICE_SUBJECT"
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


def _attestation_configuration() -> tuple[bytes, str]:
    """输入：隐式读取 Bridge 密钥和身份签发方环境变量。

    输出：已校验的密钥字节与身份签发方；配置无效时抛出 BridgeConfigurationError。
    功能：在进入 Manager 前验证稳定配置，避免配置错误被误分类为 MCP 服务失败。
    """

    secret = _required_env("SUNING_MCP_BRIDGE_SECRET").encode()
    if len(secret) < MIN_SECRET_BYTES:
        raise BridgeConfigurationError("SUNING_MCP_BRIDGE_SECRET 长度不足 32 字节")
    return secret, _required_env("SUNING_IDENTITY_ISSUER")


def current_identity() -> dict[str, str]:
    """输入：无；隐式读取 Hermes 当前请求 ContextVar、Cron 标记和服务主体环境变量。

    输出：包含平台、外部用户、会话类型和消息 ID 的可信身份字典。
    功能：从 Hermes 请求上下文取得真实发送者并规范化会话类型，不接受工具参数。
    """

    if (
        os.getenv("HERMES_CRON_SESSION", "").strip() == "1"
        and get_session_env("HERMES_SESSION_ID", "").strip().startswith("cron_")
    ):
        return {
            "platform": CRON_PLATFORM,
            "external_subject": _required_env(CRON_SERVICE_SUBJECT_ENV),
            "chat_type": "dm",
            "message_id": "",
        }

    identity = {
        "platform": get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower(),
        "external_subject": get_session_env("HERMES_SESSION_USER_ID", "").strip(),
        "chat_type": get_session_env("HERMES_SESSION_CHAT_TYPE", "").strip().lower(),
        "message_id": get_session_env("HERMES_SESSION_MESSAGE_ID", "").strip(),
    }
    if not identity["platform"] or not identity["external_subject"]:
        raise PermissionError("当前请求没有可信的 Hermes 会话身份")
    if identity["platform"] == CRON_PLATFORM:
        raise PermissionError("cron 身份仅允许 Hermes 调度器使用")
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

    secret, identity_issuer = _attestation_configuration()

    now = int(time.time())
    claims = {
        "iss": TOKEN_ISSUER,
        "aud": TOKEN_AUDIENCE,
        "identity_issuer": identity_issuer,
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


def _empty_payload(result: Any) -> Any:
    """输入：Manager 判定为合法 EMPTY_RESULT 的原始 MCP Result。

    输出：结构化内容、解析后的 JSON 文本或原始空值。
    功能：为 L2 empty 信封保留已有数据结构，不把成功空查询改成服务失败。
    """

    if result is None or isinstance(result, (dict, list)):
        return result
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    text = _result_text(result)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _hermes_result(result: MCPCallResult) -> str:
    """输入：Manager 返回的标准 ``MCPCallResult``。

    输出：现有 Hermes registry 接受的 tool_result 或 tool_error 字符串。
    功能：保持普通成功负载，并转换 L1/L2/L3 降级和 L2 合法空结果。
    """

    if result.success:
        if (
            result.error_type is FailureType.EMPTY_RESULT
            and result.degrade_level is DegradeLevel.L2_CORE
        ):
            return tool_result(
                {
                    "status": "empty",
                    "available": True,
                    "degrade_level": result.degrade_level.value,
                    "tool_name": result.tool_name,
                    "failure_type": result.error_type.value,
                    "data": _empty_payload(result.data),
                    "notice": f"核心数据「{result.tool_name}」查询成功但未返回记录。",
                }
            )
        return _successful_result(result.data)

    if result.degrade_level is DegradeLevel.L3_CRITICAL:
        return tool_error("关键数据服务暂不可用，请稍后重试。")
    return tool_result(
        {
            "status": "degraded",
            "available": False,
            "degrade_level": result.degrade_level.value,
            "tool_name": result.tool_name,
            "failure_type": (
                result.error_type.value if result.error_type is not None else "UNKNOWN"
            ),
            "notice": result.degrade_note,
        }
    )


async def invoke_business_tool(
    tool_name: str,
    arguments: dict[str, Any],
    call_manager: MCPCallManager,
    tool_governor: ToolGovernor | None = None,
) -> str:
    """输入：白名单工具名、模型业务参数、注册阶段注入的 MCPCallManager 与可选治理器。

    输出：Hermes 可消费的成功结果或安全错误结果。
    功能：在 Bridge attempt 内重签身份并由 Manager 统一执行重试、熔断和结果转换。
    """
    spec = TOOL_SPECS.get(tool_name)
    if spec is None:
        return tool_error(f"未注册的苏宁业务工具: {tool_name}")

    try:
        identity = current_identity()
        _attestation_configuration()
    except PermissionError as exc:
        return tool_error(str(exc))
    except BridgeConfigurationError:
        logger.exception("苏宁业务身份桥接配置不完整")
        return tool_error("苏宁业务身份桥接配置不完整，请联系管理员")

    cache_scope = f"{identity['platform']}:{identity['external_subject']}"
    if tool_governor is not None:
        decision = tool_governor.preflight(
            tool_name=tool_name,
            params=arguments,
            cache_scope=cache_scope,
        )
        if decision.error:
            return tool_error(decision.error)
        if decision.cached_result is not None:
            return decision.cached_result

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
    call_result: MCPCallResult | None = None

    async def attempt() -> Any:
        """输入：闭包捕获的可信身份、Tool、参数、Endpoint 和 Trace ID。

        输出：一次真实 ``ClientSession.call_tool`` 的原始 MCP Result。
        功能：每次执行重新签发 attestation/JTI、建立 MCP 会话并完成协议调用。
        """

        attestation = mint_attestation(tool_name=tool_name, identity=identity)
        metadata = {
            "suning/authn": attestation,
            "suning/trace_id": trace_record.trace_id,
        }
        observability.inject_trace_metadata(metadata)
        async with streamable_http_client(endpoint) as (read, write, _session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(
                    tool_name,
                    arguments=arguments,
                    meta=metadata,
                )

    try:
        call_result = await call_manager.call(
            server_id=spec.server_id,
            tool_name=tool_name,
            degrade_level=spec.degrade_level,
            retry_on_timeout=spec.retry_on_timeout,
            empty_result_is_success=spec.empty_result_is_success,
            attempt=attempt,
        )
        success = call_result.success
        error = call_result.error_message
        if call_result.success:
            rows_returned = result_rows(call_result.data)
        elif call_result.error_message:
            logger.warning(
                "调用苏宁 MCP 最终失败: tool=%s endpoint=%s failure_type=%s error=%s",
                tool_name,
                endpoint,
                call_result.error_type.value if call_result.error_type else "UNKNOWN",
                call_result.error_message,
            )
        response = _hermes_result(call_result)
        if tool_governor is not None and call_result.success:
            tool_governor.cache_success(
                tool_name=tool_name,
                params=arguments,
                cache_scope=cache_scope,
                result=response,
            )
        return response
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
            retry_count=call_result.retry_count if call_result else 0,
            failure_type=(
                call_result.error_type.value
                if call_result and call_result.error_type is not None
                else ""
            ),
            circuit_state=call_result.circuit_state.value if call_result else "",
            degrade_level=call_result.degrade_level.value if call_result else "",
            degraded=call_result.degraded if call_result else False,
        )
        if owns_trace:
            observability.finish_trace(trace_record, "MCP 调用完成" if success else "MCP 调用失败")


Handler = Callable[[dict[str, Any]], Coroutine[Any, Any, str]]


def make_handler(
    tool_name: str,
    call_manager: MCPCallManager,
    tool_governor: ToolGovernor | None = None,
) -> Handler:
    """输入：需要绑定的白名单工具名、MCPCallManager 与可选调用治理器。

    输出：符合 Hermes registry 调用约定的异步 Handler。
    功能：为每个工具创建闭包，并复用同一个 Manager 转发身份桥接调用。
    """

    async def handler(args: dict[str, Any], **_kwargs: Any) -> str:
        """输入：模型业务参数 ``args``；其余 Hermes 参数由 ``_kwargs`` 接收。

        输出：私有 MCP 调用转换后的 Hermes 工具结果。
        功能：执行当前 ``tool_name`` 对应的身份桥接调用。
        """
        return await invoke_business_tool(
            tool_name,
            dict(args or {}),
            call_manager,
            tool_governor,
        )

    return handler


__all__ = [
    "BridgeConfigurationError",
    "current_identity",
    "invoke_business_tool",
    "make_handler",
    "mint_attestation",
]
