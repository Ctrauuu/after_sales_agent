"""验证 Hermes 桥接插件写入 MCP ``_meta`` 的短期身份凭证。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Mapping

from fastmcp import Context
from redis import Redis
from redis.exceptions import RedisError

from mcp_suning.config import settings


TOKEN_VERSION = "v1"
EXPECTED_ISSUER = "suning-rbac-bridge"
EXPECTED_AUDIENCE = "suning-mcp"
MAX_TOKEN_LIFETIME_SECONDS = 60
CLOCK_SKEW_SECONDS = 5
MIN_SECRET_BYTES = 32
TRUSTED_PLATFORMS = {"feishu", "wecom", "dingtalk"}
CRON_PLATFORM = "cron"
DIRECT_CHAT_TYPES = {"dm", "direct", "private", "p2p"}
GROUP_CHAT_TYPES = {"group", "channel", "forum", "thread"}


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """通过签名验证的外部调用主体。"""

    platform: str
    identity_issuer: str
    external_subject: str
    chat_type: str
    message_id: str
    jti: str


def _b64url_decode(value: str) -> bytes:
    """输入：省略填充符的 Base64URL 字符串 ``value``。

    输出：解码后的原始字节。
    功能：严格解码身份凭证的载荷和签名片段。
    """
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(
        (value + padding).encode("ascii"),
        altchars=b"-_",
        validate=True,
    )


def _b64url_encode(value: bytes) -> str:
    """输入：需要编码为凭证片段的原始字节 ``value``。

    输出：不带尾部填充符的 Base64URL 文本。
    功能：为服务内委托凭证构造与 Hermes 桥接插件一致的安全载荷和签名编码。
    """

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    """输入：待签名的身份声明映射 ``value``。

    输出：键有序、无多余空白的 UTF-8 JSON 字节。
    功能：固定委托凭证的签名输入，使后端服务与 Hermes 桥接插件保持同一序列化约定。
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def mint_delegated_attestation(
    principal: AuthenticatedPrincipal,
    *,
    tool_name: str,
    ttl_seconds: int = 30,
) -> str:
    """输入：已验证的调用主体 ``principal``、下游工具名和可选有效期秒数。

    输出：绑定下游工具且带新 JTI 的 HMAC 身份凭证；配置无效时抛出 ``PermissionError``。
    功能：让已验证的聚合 MCP 在不复用上游一次性凭证的前提下，把同一用户身份安全委托给各下游 MCP。
    """

    normalized_tool = tool_name.strip()
    secret = settings.suning_mcp_bridge_secret.strip()
    identity_issuer = settings.suning_identity_issuer.strip()
    if not normalized_tool:
        raise PermissionError("下游工具名不能为空")
    if not secret or not identity_issuer:
        raise PermissionError("MCP 身份验证配置不完整")
    if len(secret.encode("utf-8")) < MIN_SECRET_BYTES:
        raise PermissionError("MCP 身份验证密钥长度不足")
    bounded_ttl = min(max(int(ttl_seconds), 1), MAX_TOKEN_LIFETIME_SECONDS)
    now = int(time.time())
    claims = {
        "iss": EXPECTED_ISSUER,
        "aud": EXPECTED_AUDIENCE,
        "identity_issuer": principal.identity_issuer,
        "platform": principal.platform,
        "sub": principal.external_subject,
        "chat_type": principal.chat_type,
        "message_id": principal.message_id,
        "tool": normalized_tool,
        "iat": now,
        "exp": now + bounded_ttl,
        "jti": secrets.token_urlsafe(18),
    }
    payload = _b64url_encode(_canonical_json(claims))
    signing_input = f"{TOKEN_VERSION}.{payload}".encode("ascii")
    signature = hmac.new(
        secret.encode("utf-8"),
        signing_input,
        hashlib.sha256,
    ).digest()
    return f"{TOKEN_VERSION}.{payload}.{_b64url_encode(signature)}"


def _metadata_from_context(ctx: Context) -> dict[str, Any]:
    """输入：FastMCP 自动注入的请求上下文 ``ctx``。

    输出：普通字典形式的 MCP 请求元数据；不存在时返回空字典。
    功能：兼容 Mapping 和 Pydantic 两种 FastMCP 元数据表示。
    """
    request_context = getattr(ctx, "request_context", None)
    metadata = getattr(request_context, "meta", None)
    if isinstance(metadata, Mapping):
        return dict(metadata)
    model_dump = getattr(metadata, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(exclude_none=True)) # type: ignore
    return {}


def _consume_once(jti: str, ttl_seconds: int) -> None:
    """输入：凭证唯一编号 ``jti`` 和剩余有效秒数 ``ttl_seconds``。

    输出：无；重复使用或 Redis 不可用时抛出权限异常。
    功能：用 Redis ``SET NX`` 原子消费 JTI，阻止身份凭证重放。
    """
    if not settings.redis_url:
        if settings.suning_authn_require_redis:
            raise PermissionError("身份重放保护 Redis 未配置")
        return

    try:
        client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        accepted = client.set(
            f"suning:authn:jti:{jti}",
            "1",
            ex=max(ttl_seconds, 1),
            nx=True,
        )
    except RedisError as exc:
        raise PermissionError("身份重放保护服务不可用") from exc

    if not accepted:
        raise PermissionError("身份凭证已被使用")


def verify_attestation(
    ctx: Context,
    *,
    expected_tool: str,
) -> AuthenticatedPrincipal:
    """输入：FastMCP 上下文 ``ctx`` 和当前工具名 ``expected_tool``。

    输出：通过签名验证的 ``AuthenticatedPrincipal``。
    功能：验证 HMAC、签发方、接收方、工具绑定、有效期、主体字段和防重放状态。
    """
    token = _metadata_from_context(ctx).get("suning/authn")
    if not isinstance(token, str) or not token.strip():
        raise PermissionError("缺少苏宁业务身份凭证")

    secret = settings.suning_mcp_bridge_secret.strip()
    identity_issuer = settings.suning_identity_issuer.strip()
    if not secret or not identity_issuer:
        raise PermissionError("MCP 身份验证配置不完整")
    if len(secret.encode("utf-8")) < MIN_SECRET_BYTES:
        raise PermissionError("MCP 身份验证密钥长度不足")

    parts = token.strip().split(".")
    if len(parts) != 3 or not all(parts):
        raise PermissionError("身份凭证格式无效")
    version, payload_text, signature_text = parts
    if version != TOKEN_VERSION:
        raise PermissionError("身份凭证版本不受支持")

    expected_signature = hmac.new(
        secret.encode("utf-8"),
        f"{version}.{payload_text}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        provided_signature = _b64url_decode(signature_text)
    except Exception as exc:
        raise PermissionError("身份凭证签名格式无效") from exc
    if not hmac.compare_digest(expected_signature, provided_signature):
        raise PermissionError("身份凭证签名无效")

    try:
        claims = json.loads(_b64url_decode(payload_text).decode("utf-8"))
    except Exception as exc:
        raise PermissionError("身份凭证载荷无效") from exc
    if not isinstance(claims, dict):
        raise PermissionError("身份凭证载荷必须是对象")

    if claims.get("iss") != EXPECTED_ISSUER:
        raise PermissionError("身份凭证签发方无效")
    if claims.get("aud") != EXPECTED_AUDIENCE:
        raise PermissionError("身份凭证接收方无效")
    if claims.get("tool") != expected_tool:
        raise PermissionError("身份凭证不能用于当前工具")
    if claims.get("identity_issuer") != identity_issuer:
        raise PermissionError("外部身份命名空间无效")

    try:
        issued_at = int(claims["iat"])
        expires_at = int(claims["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PermissionError("身份凭证时间字段无效") from exc
    now = int(time.time())
    if issued_at > now + CLOCK_SKEW_SECONDS:
        raise PermissionError("身份凭证尚未生效")
    if expires_at < now:
        raise PermissionError("身份凭证已过期")
    if expires_at <= issued_at:
        raise PermissionError("身份凭证时间范围无效")
    if expires_at - issued_at > MAX_TOKEN_LIFETIME_SECONDS:
        raise PermissionError("身份凭证有效期过长")

    platform = str(claims.get("platform") or "").strip().lower()
    subject = str(claims.get("sub") or "").strip()
    raw_chat_type = str(claims.get("chat_type") or "").strip().lower()
    message_id = str(claims.get("message_id") or "").strip()
    jti = str(claims.get("jti") or "").strip()

    if not subject or len(subject) > 128:
        raise PermissionError("外部用户身份无效")
    if platform == CRON_PLATFORM:
        expected_subject = settings.suning_cron_service_subject.strip()
        if not expected_subject or subject != expected_subject:
            raise PermissionError("Cron 服务主体无效")
        if raw_chat_type != "dm":
            raise PermissionError("Cron 服务主体必须使用私聊会话")
        chat_type = "dm"
    elif platform not in TRUSTED_PLATFORMS:
        raise PermissionError("身份凭证平台无效")
    elif raw_chat_type in DIRECT_CHAT_TYPES:
        chat_type = "dm"
    elif raw_chat_type in GROUP_CHAT_TYPES:
        chat_type = raw_chat_type
    else:
        raise PermissionError("身份凭证会话类型无效")
    if not jti or len(jti) > 128:
        raise PermissionError("身份凭证 jti 无效")

    _consume_once(jti, expires_at - now)
    return AuthenticatedPrincipal(
        platform=platform,
        identity_issuer=identity_issuer,
        external_subject=subject,
        chat_type=chat_type,
        message_id=message_id,
        jti=jti,
    )


__all__ = [
    "AuthenticatedPrincipal",
    "mint_delegated_attestation",
    "verify_attestation",
]
