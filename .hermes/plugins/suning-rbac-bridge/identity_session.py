"""统一 IM 身份解析、私聊会话路由及 Hook 转发。"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from gateway.session_context import get_session_env  # type: ignore


TRUSTED_PLATFORMS = frozenset({"feishu", "wecom", "dingtalk"})
CRON_PLATFORM = "cron"
CRON_SERVICE_SUBJECT_ENV = "SUNING_CRON_SERVICE_SUBJECT"
DIRECT_CHAT_TYPES = frozenset({"dm", "direct", "private", "p2p"})
GROUP_CHAT_TYPES = frozenset({"group", "channel", "forum", "thread"})
ACTIVE_SESSION_TTL_SECONDS = 30 * 60
TURN_LEASE_TTL_SECONDS = 5 * 60
TURN_LEASE_WAIT_SECONDS = 0.0
TURN_LEASE_RENEW_SECONDS = TURN_LEASE_TTL_SECONDS / 3
UNAUTHORIZED_REPLY = "未授权用户，请联系管理员绑定账号。"
CONCURRENT_TURN_REPLY = "该账号的跨平台会话正在处理中，请稍后重试。"
LEASE_LOST_REPLY = "当前会话协调状态已失效，本轮结果未写入上下文，请重试。"


logger = logging.getLogger(__name__)


_RENEW_TURN_LEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE_TURN_LEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class IdentityResolutionError(PermissionError):
    """可信 IM 身份不能映射到启用 Hermes 用户时抛出的异常。"""


@dataclass(frozen=True)
class SessionRoute:
    """一次 Hermes 请求对应的统一用户和逻辑上下文会话。"""

    hermes_user_id: str
    session_id: str
    platform: str
    chat_type: str


class ConversationTurnLease:
    """用 Redis 令牌租约串行化同一逻辑会话的完整 Agent 回合。"""

    def __init__(self, redis_client: Any, session_id: str) -> None:
        """输入：共享 Redis 客户端 ``redis_client`` 与已隔离的逻辑会话 ID ``session_id``。

        输出：初始化尚未获取的、带随机所有者令牌的租约对象。
        功能：为跨进程/跨平台的同一逻辑会话建立可比较、可续期且可安全释放的 Redis 锁状态。
        """

        self._redis = redis_client
        self._key = f"im:turn-lease:{session_id}"
        self._token = secrets.token_urlsafe(24)
        self._stop_renewal = threading.Event()
        self._lost = threading.Event()
        self._acquired = False
        self._released = False

    def acquire(self, wait_seconds: float = TURN_LEASE_WAIT_SECONDS) -> bool:
        """输入：等待已有同会话回合结束的最大秒数 ``wait_seconds``。

        输出：获得租约返回 ``True``；超时或 Redis 协调不可用返回 ``False``。
        功能：使用 Redis ``SET NX EX`` 原子选出唯一回合所有者，并在获得后启动自动续租以覆盖长任务。
        """

        deadline = time.monotonic() + max(wait_seconds, 0.0)
        while True:
            try:
                acquired = bool(
                    self._redis.set(
                        self._key,
                        self._token,
                        ex=TURN_LEASE_TTL_SECONDS,
                        nx=True,
                    )
                )
            except Exception:
                return False
            if acquired:
                self._acquired = True
                self._start_renewal()
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))

    def is_held(self) -> bool:
        """输入：无；读取本租约的本地状态和 Redis 当前值。

        输出：令牌仍是 Redis 锁所有者返回 ``True``，否则返回 ``False``。
        功能：在持久化上下文或执行副作用工具前拒绝失去所有权的旧回合，防止其覆盖新回合状态。
        """

        if not self._acquired or self._released or self._lost.is_set():
            return False
        try:
            held = _text(self._redis.get(self._key)) == self._token
        except Exception:
            held = False
        if not held:
            self._lost.set()
        return held

    def release(self) -> None:
        """输入：无；使用本租约创建时的随机所有者令牌。

        输出：无；停止续租，并且仅在 Redis 令牌匹配时删除锁键。
        功能：保证已过期或被替换的旧回合无法误删后来回合持有的租约。
        """

        self._stop_renewal.set()
        if not self._acquired or self._released:
            return
        self._released = True
        try:
            self._redis.eval(_RELEASE_TURN_LEASE, 1, self._key, self._token)
        except Exception:
            logger.warning("释放会话回合租约失败", exc_info=True)

    def _start_renewal(self) -> None:
        """输入：无；要求租约已通过 ``acquire`` 成功获得。

        输出：无；原地启动守护续租线程。
        功能：让长时间的 LLM、工具或多 Agent 编排持续保有同一令牌租约，同时保留 Redis TTL 的故障恢复边界。
        """

        renewal_thread = threading.Thread(
            target=self._renew_forever,
            name="suning-im-turn-lease",
            daemon=True,
        )
        renewal_thread.start()

    def _renew_forever(self) -> None:
        """输入：无；依赖本对象的停止事件、Redis 键和所有者令牌。

        输出：无；续租失败时标记租约丢失并结束守护线程。
        功能：以比较令牌的 Lua 脚本刷新 TTL，避免网络抖动后的旧线程续写不属于自己的锁。
        """

        while not self._stop_renewal.wait(TURN_LEASE_RENEW_SECONDS):
            try:
                renewed = bool(
                    self._redis.eval(
                        _RENEW_TURN_LEASE,
                        1,
                        self._key,
                        self._token,
                        str(TURN_LEASE_TTL_SECONDS),
                    )
                )
            except Exception:
                renewed = False
            if not renewed:
                self._lost.set()
                logger.warning("会话回合租约续租失败，已停止写入上下文")
                return


def _text(value: Any) -> str:
    """输入：可能为 ``str``、``bytes`` 或空值的 Redis/Hook 字段 ``value``。

    输出：去除首尾空白后的文本；空值返回空字符串。
    功能：统一 Redis 二进制响应和 Hermes 生命周期字段的安全文本化方式。
    """

    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value or "").strip()


def _token_count(value: Any) -> int:
    """输入：Hermes API 用量字段 ``value``。

    输出：非负整数 Token 数；缺失、布尔值或无效值返回 ``0``。
    功能：安全规范化不同模型供应商返回的 Token 计数，避免观测 Hook 因异常用量字段影响业务回复。
    """

    if isinstance(value, bool):
        return 0
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _api_usage(usage: Any) -> tuple[int, int]:
    """输入：Hermes ``post_api_request`` 的 ``usage`` 映射或其他值。

    输出：输入 Token 与输出 Token 的非负整数元组；没有可靠用量时返回 ``(0, 0)``。
    功能：兼容 Hermes 的 ``input_tokens/output_tokens`` 与通用 ``prompt_tokens/completion_tokens`` 字段。
    """

    if not isinstance(usage, dict):
        return 0, 0
    return (
        _token_count(usage.get("prompt_tokens", usage.get("input_tokens"))),
        _token_count(usage.get("completion_tokens", usage.get("output_tokens"))),
    )


def _api_prompt_cache_usage(usage: Any) -> tuple[int, int]:
    """输入：Hermes ``post_api_request`` 的 ``usage`` 映射或其他值。

    输出：缓存命中与未命中的输入 Token 数；字段缺失或无效时均返回 ``0``。
    功能：提取 DeepSeek 自动上下文缓存的原生用量字段，供观测层验证 Prompt 前缀复用效果。
    """

    if not isinstance(usage, dict):
        return 0, 0
    return (
        _token_count(usage.get("prompt_cache_hit_tokens")),
        _token_count(usage.get("prompt_cache_miss_tokens")),
    )


def _api_duration_ms(value: Any) -> float | None:
    """输入：Hermes ``post_api_request`` 的秒级 API 耗时字段 ``value``。

    输出：非负且有限的毫秒数；无效值返回 ``None``。
    功能：将宿主在实际模型请求边界测得的时长转换为 Span 属性，避免把工具执行时间算入 LLM 耗时。
    """

    try:
        duration_ms = float(value) * 1000
    except (TypeError, ValueError):
        return None
    return duration_ms if duration_ms >= 0 and duration_ms < float("inf") else None


class IdentitySessionRouter:
    """将可信 IM 发送者映射为 Hermes 用户并选择上下文逻辑会话。"""

    def __init__(self, database_engine: Any, redis_client: Any) -> None:
        """输入：已有 MySQL ``database_engine`` 与 Redis ``redis_client``。

        输出：保存只读身份查询与活跃会话路由依赖。
        功能：复用现有身份表和 Context Hooks 的连接，不创建新的用户中心或会话表。
        """

        self._database_engine = database_engine
        self._redis = redis_client

    def resolve(
        self,
        *,
        session_id: str,
        sender_id: str = "",
        platform: str = "",
        chat_type: str = "",
    ) -> SessionRoute:
        """输入：原始 Hermes 会话、可选发送者及可选平台/会话类型；缺失字段从可信 ContextVar 或 Cron 服务配置读取。

        输出：绑定启用 ``hermes_user_id`` 的逻辑会话路由；未绑定、停用或不可信字段时抛出 ``IdentityResolutionError``。
        功能：私聊以 Redis 30 分钟活跃键跨平台复用结构化上下文，群聊仅创建用户隔离的群内上下文。
        """

        source_session = _text(session_id)
        is_cron = (
            os.getenv("HERMES_CRON_SESSION", "").strip() == "1"
            and source_session.startswith("cron_")
        )
        resolved_platform = _text(platform or get_session_env("HERMES_SESSION_PLATFORM", "")).lower()
        external_subject = _text(sender_id or get_session_env("HERMES_SESSION_USER_ID", ""))
        resolved_chat_type = _text(chat_type or get_session_env("HERMES_SESSION_CHAT_TYPE", "")).lower()
        if not source_session:
            raise IdentityResolutionError("当前请求没有可信的 Hermes 会话标识")
        if is_cron:
            resolved_platform = CRON_PLATFORM
            external_subject = _text(os.getenv(CRON_SERVICE_SUBJECT_ENV, ""))
            resolved_chat_type = "dm"
        elif resolved_platform == CRON_PLATFORM:
            raise IdentityResolutionError("cron 身份仅允许 Hermes 调度器使用")
        elif resolved_platform not in TRUSTED_PLATFORMS:
            raise IdentityResolutionError("当前请求的平台身份无效")
        if not external_subject or len(external_subject) > 128:
            raise IdentityResolutionError("当前请求的平台用户身份无效")
        if resolved_chat_type in DIRECT_CHAT_TYPES:
            resolved_chat_type = "dm"
        elif resolved_chat_type not in GROUP_CHAT_TYPES:
            raise IdentityResolutionError("当前请求没有可信的会话类型")

        hermes_user_id = self._resolve_hermes_user_id(resolved_platform, external_subject)
        if resolved_chat_type == "dm":
            logical_session_id = self._active_session(hermes_user_id)
        else:
            logical_session_id = (
                f"im:group:{resolved_platform}:{source_session}:{hermes_user_id}"
            )
        return SessionRoute(
            hermes_user_id=hermes_user_id,
            session_id=logical_session_id,
            platform=resolved_platform,
            chat_type=resolved_chat_type,
        )

    def new_turn_lease(self, session_id: str) -> ConversationTurnLease:
        """输入：已由身份路由生成的逻辑会话 ID ``session_id``。

        输出：尚未获取的 Redis 回合租约对象。
        功能：让统一身份 Hook 用与活跃会话相同的 Redis 连接对同一私聊或群聊逻辑会话执行跨进程串行化。
        """

        return ConversationTurnLease(self._redis, session_id)

    def _resolve_hermes_user_id(self, platform: str, external_subject: str) -> str:
        """输入：已校验平台 ``platform`` 与可信平台用户 ID ``external_subject``。

        输出：已启用的 Hermes 内部用户 ID；查无绑定、用户停用或查询异常时抛出 ``IdentityResolutionError``。
        功能：按 MCP RBAC 相同的绑定表关联规则验证身份，不基于手机号或姓名推断跨平台关系。
        """

        try:
            from sqlalchemy import text

            with self._database_engine.connect() as connection:
                row = connection.execute(
                    text(
                        """
                        SELECT u.hermes_user_id, u.is_active
                        FROM user_platform_binding AS b
                        JOIN user_identity AS u
                          ON u.hermes_user_id = b.hermes_user_id
                        WHERE b.platform = :platform
                          AND b.platform_user_id = :platform_user_id
                        LIMIT 1
                        """
                    ),
                    {
                        "platform": platform,
                        "platform_user_id": external_subject,
                    },
                ).mappings().first()
        except Exception as exc:
            raise IdentityResolutionError("身份验证服务暂时不可用") from exc
        if row is None:
            raise IdentityResolutionError("用户未绑定或不存在")
        if not bool(row["is_active"]):
            raise IdentityResolutionError("用户已被禁用")
        hermes_user_id = _text(row["hermes_user_id"])
        if not hermes_user_id:
            raise IdentityResolutionError("用户内部身份无效")
        return hermes_user_id

    def _active_session(self, hermes_user_id: str) -> str:
        """输入：已验证的内部用户 ID ``hermes_user_id``。

        输出：30 分钟内稳定的私聊逻辑会话 ID；Redis 写入或读取失败时抛出 ``IdentityResolutionError``。
        功能：用 ``im:active-session:<hermes_user_id>`` 原子地首写会话并刷新有效期，实现跨 IM 私聊延续。
        """

        key = f"im:active-session:{hermes_user_id}"
        candidate = f"im:dm:{hermes_user_id}:{secrets.token_urlsafe(12)}"
        try:
            active = _text(self._redis.get(key))
            if not active:
                created = self._redis.set(
                    key,
                    candidate,
                    ex=ACTIVE_SESSION_TTL_SECONDS,
                    nx=True,
                )
                active = candidate if created else _text(self._redis.get(key))
            if not active:
                raise RuntimeError("活跃会话创建冲突")
            self._redis.expire(key, ACTIVE_SESSION_TTL_SECONDS)
            return active
        except Exception as exc:
            raise IdentityResolutionError("会话路由服务暂时不可用") from exc


class UnifiedIdentityHooks:
    """把统一身份路由应用到短期上下文、长期记忆和知识检索 Hooks。"""

    def __init__(
        self,
        router: IdentitySessionRouter,
        conversation_hooks: Any,
        memory_hooks: Any = None,
        knowledge_hooks: Any = None,
        skill_hooks: Any = None,
        observability_client: Any = None,
        tool_governor: Any = None,
    ) -> None:
        """输入：身份路由器、短期会话 Hooks、可选扩展 Hooks 和观测客户端。

        输出：保存同一请求中共用的 Hook 依赖和 ContextVar 路由状态。
        功能：在一个可信身份解析结果下依次运行既有能力，并可选注入工具治理和记录 Agent 回合观测数据。
        """

        self._router = router
        self._conversation_hooks = conversation_hooks
        self._memory_hooks = memory_hooks
        self._knowledge_hooks = knowledge_hooks
        self._skill_hooks = skill_hooks
        self._observability = observability_client
        self._tool_governor = tool_governor
        self._route: ContextVar[SessionRoute | None] = ContextVar(
            "suning_identity_session_route",
            default=None,
        )
        self._denied: ContextVar[bool] = ContextVar(
            "suning_identity_session_denied",
            default=False,
        )
        self._busy: ContextVar[bool] = ContextVar(
            "suning_identity_session_busy",
            default=False,
        )
        self._lease_lost: ContextVar[bool] = ContextVar(
            "suning_identity_session_lease_lost",
            default=False,
        )
        self._turn_lease: ContextVar[ConversationTurnLease | None] = ContextVar(
            "suning_identity_session_turn_lease",
            default=None,
        )
        self._trace: ContextVar[Any] = ContextVar(
            "suning_identity_session_trace",
            default=None,
        )
        self._api_spans: ContextVar[dict[str, Any]] = ContextVar(
            "suning_identity_session_api_spans",
            default={},
        )

    @staticmethod
    def _context_text(result: Any) -> str:
        """输入：单个既有 Hook 的返回值 ``result``。

        输出：可注入本轮用户消息的上下文文本；无上下文或不兼容返回值时返回空字符串。
        功能：兼容 Hermes Hook 的 ``{\"context\": ...}`` 合约，并让多种上下文按原有顺序合并。
        """

        if not isinstance(result, dict):
            return ""
        return _text(result.get("context"))

    def pre_llm_call(
        self,
        *,
        session_id: str = "",
        sender_id: str = "",
        **kwargs: Any,
    ) -> dict[str, str] | None:
        """输入：Hermes 原始会话、发送者和完整生命周期参数 ``kwargs``。

        输出：合并后的短期、长期和知识上下文；身份失败时返回拒绝提示。
        功能：先解析统一用户与逻辑会话，再以 ``hermes_user_id`` 转发既有回答前 Hooks，拒绝时不读取任何用户数据。
        """

        try:
            route = self._router.resolve(session_id=session_id, sender_id=sender_id)
        except IdentityResolutionError:
            self._route.set(None)
            self._denied.set(True)
            self._busy.set(False)
            return {"context": "身份校验失败。不得调用工具或提供业务数据，只能回复用户未授权。"}

        self._route.set(route)
        self._denied.set(False)
        self._busy.set(False)
        self._lease_lost.set(False)
        turn_lease = self._router.new_turn_lease(route.session_id)
        if not turn_lease.acquire(wait_seconds=TURN_LEASE_WAIT_SECONDS):
            self._turn_lease.set(None)
            self._busy.set(True)
            return {"context": "同一账号已有跨平台会话正在处理。不得调用工具，只能提示用户稍后重试。"}
        self._turn_lease.set(turn_lease)
        if self._observability is not None:
            trace_record, _ = self._observability.ensure_trace(
                conversation_id=route.session_id,
                user_id=route.hermes_user_id,
                platform=route.platform,
                query=_text(kwargs.get("user_message")),
            )
            self._trace.set(trace_record)
        routed_kwargs = {
            **kwargs,
            "session_id": route.session_id,
            "sender_id": route.hermes_user_id,
        }
        contexts = [
            self._context_text(self._conversation_hooks.pre_llm_call(**routed_kwargs))
        ]
        if self._memory_hooks is not None:
            contexts.append(self._context_text(self._memory_hooks.pre_llm_call(**routed_kwargs)))
        if self._knowledge_hooks is not None:
            contexts.append(self._context_text(self._knowledge_hooks.pre_llm_call(**routed_kwargs)))
        if self._skill_hooks is not None:
            contexts.append(self._context_text(self._skill_hooks.pre_llm_call(**routed_kwargs)))
        if self._tool_governor is not None:
            scene = self._scene(route.session_id)
            whitelist = self._tool_governor.begin_turn(
                session_id=route.session_id,
                turn_id=_text(kwargs.get("turn_id")) or secrets.token_urlsafe(12),
                scene=scene,
            )
            contexts.append(self._tool_governor.build_tool_prompt(whitelist))
        combined = "\n\n".join(context for context in contexts if context)
        return {"context": combined} if combined else None

    def _scene(self, session_id: str) -> str:
        """输入：统一身份层生成的逻辑会话 ID ``session_id``。

        输出：当前已确认的槽位话题；读取失败或无话题时返回 ``general``。
        功能：复用 ConversationHooks 已持久化的意图结果，为工具白名单选择场景而不新增路由模型。
        """

        try:
            context = self._conversation_hooks.manager.load_context(session_id)
            return _text(context.slots.topic) or "general"
        except Exception:
            return "general"

    def transform_llm_output(self, *, response_text: str = "", **_kwargs: Any) -> str | None:
        """输入：模型最终文本 ``response_text`` 与其余 Hermes 输出生命周期字段。

        输出：身份拒绝、并发超时或租约失效时返回固定文案；正常会话返回 ``None`` 保留原回答。
        功能：在所有工具调用完成后覆盖不可安全执行的回合输出，确保身份校验和跨平台并发控制均默认拒绝。
        """

        del response_text
        if self._denied.get():
            return UNAUTHORIZED_REPLY
        if self._busy.get():
            return CONCURRENT_TURN_REPLY
        if self._lease_lost.get():
            return LEASE_LOST_REPLY
        return None

    def pre_tool_call(self, *, tool_name: str = "", **kwargs: Any) -> dict[str, str] | None:
        """输入：模型即将调用的工具名 ``tool_name`` 与其余 Hermes 工具生命周期字段。

        输出：身份拒绝、并发超时或租约失效时返回 Hermes ``block`` 指令；正常会话返回 ``None`` 继续执行。
        功能：在未绑定、并发未获租约或失去所有权时阻断工具副作用，并只为唯一租约所有者记录 Skill 轨迹。
        """

        if self._denied.get():
            message = UNAUTHORIZED_REPLY
        elif self._busy.get():
            message = CONCURRENT_TURN_REPLY
        elif not self._has_turn_lease():
            message = LEASE_LOST_REPLY
        else:
            route = self._route.get()
            if route is not None and self._skill_hooks is not None:
                routed_kwargs = {
                    **kwargs,
                    "session_id": route.session_id,
                    "tool_name": tool_name,
                }
                self._skill_hooks.pre_tool_call(
                    **routed_kwargs,
                )
            return None
        return {
            "action": "block",
            "message": f"工具 {tool_name or '调用'} 已被阻止：{message}",
        }

    def pre_api_request(self, *, api_request_id: str = "", **kwargs: Any) -> None:
        """输入：Hermes 单次模型请求 ID ``api_request_id`` 与其余 API 生命周期字段。

        输出：无；为该 API 请求保存一个未结束的 LLM Span。
        功能：在真实模型网络请求开始处创建 Span，使 MCP 调用时间不再混入 LLM 耗时。
        """

        if not self._has_turn_lease() or self._observability is None or self._trace.get() is None:
            return
        key = api_request_id or str(kwargs.get("api_call_count") or "default")
        spans = dict(self._api_spans.get())
        spans[key] = self._observability.start_llm_span("api_request")
        self._api_spans.set(spans)

    def post_api_request(self, *, api_request_id: str = "", **kwargs: Any) -> None:
        """输入：Hermes 模型请求 ID ``api_request_id`` 和包含用量、耗时的完成字段。

        输出：无；结束匹配 LLM Span 并写入真实 Token 和 API 耗时。
        功能：使用 Hermes 在模型 API 边界给出的 ``usage`` 和 ``api_duration``，精确记录每次模型调用。
        """

        key = api_request_id or str(kwargs.get("api_call_count") or "default")
        spans = dict(self._api_spans.get())
        span = spans.pop(key, None)
        self._api_spans.set(spans)
        if span is None or self._observability is None:
            return
        prompt_tokens, completion_tokens = _api_usage(kwargs.get("usage"))
        prompt_cache_hit_tokens, prompt_cache_miss_tokens = _api_prompt_cache_usage(
            kwargs.get("usage")
        )
        self._observability.end_llm_span(
            span,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt_cache_hit_tokens=prompt_cache_hit_tokens,
            prompt_cache_miss_tokens=prompt_cache_miss_tokens,
            model=_text(kwargs.get("response_model") or kwargs.get("model")),
            duration_ms=_api_duration_ms(kwargs.get("api_duration")),
        )

    def api_request_error(self, *, api_request_id: str = "", **kwargs: Any) -> None:
        """输入：失败模型请求 ID ``api_request_id`` 与错误生命周期字段。

        输出：无；结束匹配 LLM Span 并将其标记为错误。
        功能：保证模型请求失败或重试不会遗留未结束 Span，方便按失败原因检索 Agent 回合。
        """

        key = api_request_id or str(kwargs.get("api_call_count") or "default")
        spans = dict(self._api_spans.get())
        span = spans.pop(key, None)
        self._api_spans.set(spans)
        if span is None or self._observability is None:
            return
        self._observability.end_llm_span(
            span,
            model=_text(kwargs.get("model")),
            error=_text(kwargs.get("error_message") or kwargs.get("error"))[:500],
        )

    def post_llm_call(
        self,
        *,
        session_id: str = "",
        sender_id: str = "",
        **kwargs: Any,
    ) -> None:
        """输入：Hermes 原始会话、可能为空的发送者和完整回答后生命周期参数 ``kwargs``。

        输出：无；唯一租约所有者写入短期上下文并提交长期记忆，其他请求不产生用户数据副作用。
        功能：复用回答前保存的逻辑会话路由，在释放回合租约前完成持久化，避免跨 IM 并发回合覆盖上下文。
        """

        del session_id, sender_id
        route = self._route.get()
        try:
            if route is None or self._denied.get() or self._busy.get():
                return
            if not self._has_turn_lease():
                return
            routed_kwargs = {
                **kwargs,
                "session_id": route.session_id,
                "sender_id": route.hermes_user_id,
            }
            self._conversation_hooks.post_llm_call(**routed_kwargs)
            if self._memory_hooks is not None:
                self._memory_hooks.post_llm_call(**routed_kwargs)
            if self._skill_hooks is not None:
                self._skill_hooks.post_llm_call(**routed_kwargs)
        finally:
            trace_record = self._trace.get()
            if trace_record is not None and self._observability is not None:
                self._observability.finish_trace(
                    trace_record,
                    _text(kwargs.get("response_text") or kwargs.get("assistant_response")),
                )
            self._api_spans.set({})
            self._trace.set(None)
            self._route.set(None)
            self._denied.set(False)
            self._busy.set(False)
            self._lease_lost.set(False)
            self._release_turn_lease()

    def on_session_end(self, **kwargs: Any) -> None:
        """输入：Hermes 回合结束状态及可选最终回复字段 ``kwargs``。

        输出：无；在失败、中断或正常完成后停止续租并清理本请求状态。
        功能：覆盖 ``post_llm_call`` 不会触发的异常和中断路径，确保崩溃外的所有回合及时释放统一会话租约。
        """

        trace_record = self._trace.get()
        if trace_record is not None and self._observability is not None:
            self._observability.finish_trace(
                trace_record,
                _text(kwargs.get("response_text") or kwargs.get("assistant_response")),
            )
        self._api_spans.set({})
        self._trace.set(None)
        self._route.set(None)
        self._denied.set(False)
        self._busy.set(False)
        self._lease_lost.set(False)
        self._release_turn_lease()

    def _has_turn_lease(self) -> bool:
        """输入：无；读取当前异步请求的 ContextVar 租约。

        输出：当前请求仍是逻辑会话唯一 Redis 租约所有者时返回 ``True``，否则返回 ``False``。
        功能：集中标记失去租约的回合，使其无法继续调用工具或将旧的读改写结果保存到共享上下文。
        """

        turn_lease = self._turn_lease.get()
        if turn_lease is None or not turn_lease.is_held():
            if turn_lease is not None:
                self._lease_lost.set(True)
            return False
        return True

    def _release_turn_lease(self) -> None:
        """输入：无；读取当前异步请求的 ContextVar 租约。

        输出：无；原地停止续租、条件释放 Redis 键并清空该请求的租约引用。
        功能：让正常 post Hook 和兜底 session-end Hook 以相同的令牌安全、幂等地结束会话排他权。
        """

        turn_lease = self._turn_lease.get()
        self._turn_lease.set(None)
        if turn_lease is not None:
            turn_lease.release()


__all__ = [
    "ACTIVE_SESSION_TTL_SECONDS",
    "CONCURRENT_TURN_REPLY",
    "ConversationTurnLease",
    "IdentityResolutionError",
    "IdentitySessionRouter",
    "SessionRoute",
    "LEASE_LOST_REPLY",
    "UNAUTHORIZED_REPLY",
    "UnifiedIdentityHooks",
]
