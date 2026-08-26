import base64
import hashlib
import hmac
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import mcp_suning.security.attestation as attestation


TEST_SECRET = "test-secret-with-at-least-thirty-two-bytes"
TEST_ISSUER = "suning-feishu-primary"
PLUGIN_ROOT = (
    Path(__file__).resolve().parents[2]
    / ".hermes"
    / "plugins"
    / "suning-rbac-bridge"
)


def _b64url(value: bytes) -> str:
    """输入：参数 ``value``。

    输出：返回类型为 ``str`` 的测试数据或测试替身结果。
    功能：按生产凭证格式生成不含填充符的 Base64URL 测试文本。
    """
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _token(**overrides: object) -> str:
    """输入：参数 ``**overrides``。

    输出：返回类型为 ``str`` 的测试数据或测试替身结果。
    功能：构造可覆盖声明字段的签名身份测试凭证。
    """
    now = int(time.time())
    claims = {
        "iss": "suning-rbac-bridge",
        "aud": "suning-mcp",
        "identity_issuer": TEST_ISSUER,
        "platform": "feishu",
        "sub": "ou_real_sender",
        "chat_type": "dm",
        "message_id": "om_test",
        "tool": "search_orders",
        "iat": now,
        "exp": now + 30,
        "jti": "jti_test",
    }
    claims.update(overrides)
    payload = _b64url(
        json.dumps(
            claims,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    signing_input = f"v1.{payload}".encode("ascii")
    signature = hmac.new(
        TEST_SECRET.encode("utf-8"),
        signing_input,
        hashlib.sha256,
    ).digest()
    return f"v1.{payload}.{_b64url(signature)}"


def _context(token: object) -> SimpleNamespace:
    """输入：参数 ``token``。

    输出：返回类型为 ``SimpleNamespace`` 的测试数据或测试替身结果。
    功能：把测试凭证放入与 FastMCP 一致的私有元数据结构。
    """
    return SimpleNamespace(
        request_context=SimpleNamespace(
            meta={"suning/authn": token},
        )
    )


def _load_real_bridge(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """输入：参数 ``monkeypatch``。

    输出：返回类型为 ``ModuleType`` 的测试数据或测试替身结果。
    功能：在后端测试环境中加载真实插件模块，而不依赖 Hermes 安装路径。
    """

    package_name = "_suning_rbac_bridge_contract_test"
    package = ModuleType(package_name)
    package.__path__ = [str(PLUGIN_ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)

    gateway = ModuleType("gateway")
    gateway.__path__ = []  # type: ignore[attr-defined]
    session_context = ModuleType("gateway.session_context")
    session_context.get_session_env = lambda *_args: ""  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)

    tools_package = ModuleType("tools")
    tools_package.__path__ = []  # type: ignore[attr-defined]
    registry = ModuleType("tools.registry")
    registry.tool_error = lambda value: value  # type: ignore[attr-defined]
    registry.tool_result = lambda value: value  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools", tools_package)
    monkeypatch.setitem(sys.modules, "tools.registry", registry)

    module_name = f"{package_name}.bridge"
    spec = importlib.util.spec_from_file_location(
        module_name,
        PLUGIN_ROOT / "bridge.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def configured_attestation(monkeypatch: pytest.MonkeyPatch) -> None:
    """输入：参数 ``monkeypatch``。

    输出：无显式返回值；用于当前测试或辅助流程。
    功能：为每个凭证测试配置固定密钥、身份命名空间和本地防重放策略。
    """
    monkeypatch.setattr(
        attestation.settings,
        "suning_mcp_bridge_secret",
        TEST_SECRET,
    )
    monkeypatch.setattr(
        attestation.settings,
        "suning_identity_issuer",
        TEST_ISSUER,
    )
    monkeypatch.setattr(attestation.settings, "redis_url", "")
    monkeypatch.setattr(
        attestation.settings,
        "suning_authn_require_redis",
        False,
    )
    monkeypatch.setattr(
        attestation.settings,
        "suning_cron_service_subject",
        "precompute-daily",
    )


def test_valid_attestation_returns_verified_principal() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证合法凭证会解析为包含真实平台主体的可信 Principal。
    """
    principal = attestation.verify_attestation(
        _context(_token()),
        expected_tool="search_orders",
    )

    assert principal.platform == "feishu"
    assert principal.external_subject == "ou_real_sender"
    assert principal.chat_type == "dm"
    assert principal.identity_issuer == TEST_ISSUER


def test_cron_attestation_requires_the_configured_service_subject() -> None:
    """输入：Cron 平台的正确与错误服务主体凭证。

    输出：无；主体映射或拒绝边界错误时由 pytest 报告失败。
    功能：验证 Cron 只能使用配置中的固定服务主体，且强制归一为私聊会话。
    """

    principal = attestation.verify_attestation(
        _context(_token(platform="cron", sub="precompute-daily", chat_type="dm")),
        expected_tool="search_orders",
    )

    assert principal.platform == "cron"
    assert principal.external_subject == "precompute-daily"
    with pytest.raises(PermissionError, match="Cron 服务主体无效"):
        attestation.verify_attestation(
            _context(_token(platform="cron", sub="someone-else", jti="cron-invalid")),
            expected_tool="search_orders",
        )


def test_bridge_mints_cron_identity_only_for_scheduler_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：Hermes Cron 标记、服务主体配置和真实 Bridge 模块。

    输出：无；Cron 身份未固定或普通会话可伪造时由 pytest 报告失败。
    功能：验证 Bridge 只在调度器进程标记存在时签发固定 ``cron`` 服务身份。
    """

    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.setenv("SUNING_CRON_SERVICE_SUBJECT", "precompute-daily")
    bridge = _load_real_bridge(monkeypatch)
    monkeypatch.setattr(
        bridge,
        "get_session_env",
        lambda name, default="": "cron_test_20260823" if name == "HERMES_SESSION_ID" else default,
    )

    assert bridge.current_identity() == {
        "platform": "cron",
        "external_subject": "precompute-daily",
        "chat_type": "dm",
        "message_id": "",
    }


def test_delegated_attestation_preserves_subject_and_rebinds_tool() -> None:
    """输入：无；使用已验证顶层主体签发一个物流工具的内部委托凭证。

    输出：无；断言失败时由 pytest 报告主体继承或工具绑定错误。
    功能：验证订单全链路聚合服务可为每个下游 MCP 使用新 JTI 委托原用户身份，且不能扩大到其他工具。
    """

    principal = attestation.verify_attestation(
        _context(_token()),
        expected_tool="search_orders",
    )
    delegated = attestation.mint_delegated_attestation(
        principal,
        tool_name="query_logistics",
    )

    verified = attestation.verify_attestation(
        _context(delegated),
        expected_tool="query_logistics",
    )

    assert verified.external_subject == principal.external_subject
    assert verified.platform == principal.platform
    assert verified.jti != principal.jti
    with pytest.raises(PermissionError, match="当前工具"):
        attestation.verify_attestation(
            _context(delegated),
            expected_tool="get_refund_status",
        )


def test_tampered_attestation_is_rejected() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证修改 HMAC 签名后的身份凭证会被拒绝。
    """
    token = _token()
    version, payload, signature = token.split(".")
    tampered_signature = (
        ("A" if signature[0] != "A" else "B") + signature[1:]
    )
    tampered = f"{version}.{payload}.{tampered_signature}"

    with pytest.raises(PermissionError, match="签名无效"):
        attestation.verify_attestation(
            _context(tampered),
            expected_tool="search_orders",
        )


def test_tampered_payload_is_rejected() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证修改载荷但保留原签名的身份凭证会被拒绝。
    """
    version, payload, signature = _token().split(".")
    decoded = json.loads(
        base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    )
    decoded["sub"] = "ou_attacker"
    tampered_payload = _b64url(
        json.dumps(
            decoded,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )

    with pytest.raises(PermissionError, match="签名无效"):
        attestation.verify_attestation(
            _context(f"{version}.{tampered_payload}.{signature}"),
            expected_tool="search_orders",
        )


def test_real_bridge_token_matches_backend_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：参数 ``monkeypatch``。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：防止插件签发字段或编码与 MCP 验证逻辑悄然漂移。
    """

    monkeypatch.setenv("SUNING_MCP_BRIDGE_SECRET", TEST_SECRET)
    monkeypatch.setenv("SUNING_IDENTITY_ISSUER", TEST_ISSUER)
    bridge = _load_real_bridge(monkeypatch)
    token = bridge.mint_attestation(
        tool_name="search_orders",
        identity={
            "platform": "feishu",
            "external_subject": "ou_real_sender",
            "chat_type": "dm",
            "message_id": "om_contract",
        },
    )

    principal = attestation.verify_attestation(
        _context(token),
        expected_tool="search_orders",
    )

    assert principal.platform == "feishu"
    assert principal.external_subject == "ou_real_sender"
    assert principal.chat_type == "dm"
    assert principal.message_id == "om_contract"


def test_attestation_is_bound_to_one_tool() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证为一个工具签发的凭证不能用于另一个工具。
    """
    with pytest.raises(PermissionError, match="当前工具"):
        attestation.verify_attestation(
            _context(_token()),
            expected_tool="get_order_detail",
        )


def test_expired_attestation_is_rejected() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证超过有效期的身份凭证会被拒绝。
    """
    now = int(time.time())
    with pytest.raises(PermissionError, match="已过期"):
        attestation.verify_attestation(
            _context(_token(iat=now - 40, exp=now - 10)),
            expected_tool="search_orders",
        )


@pytest.mark.parametrize(
    ("claim", "value", "message"),
    [
        ("iss", "wrong-issuer", "签发方无效"),
        ("aud", "wrong-audience", "接收方无效"),
        ("identity_issuer", "wrong-identity-space", "身份命名空间无效"),
        ("platform", "unknown", "平台无效"),
        ("sub", "", "外部用户身份无效"),
        ("jti", "", "jti 无效"),
    ],
)
def test_invalid_security_claim_is_rejected(
    claim: str,
    value: str,
    message: str,
) -> None:
    """输入：参数 ``claim``、``value``、``message``。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证签发方、接收方、平台、主体和 JTI 等非法声明会被拒绝。
    """
    with pytest.raises(PermissionError, match=message):
        attestation.verify_attestation(
            _context(_token(**{claim: value})),
            expected_tool="search_orders",
        )


def test_future_attestation_is_rejected() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证签发时间明显晚于当前时间的凭证尚未生效。
    """
    now = int(time.time())
    with pytest.raises(PermissionError, match="尚未生效"):
        attestation.verify_attestation(
            _context(_token(iat=now + 10, exp=now + 40)),
            expected_tool="search_orders",
        )


def test_excessive_attestation_lifetime_is_rejected() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证有效期超过服务端上限的凭证会被拒绝。
    """
    now = int(time.time())
    with pytest.raises(PermissionError, match="有效期过长"):
        attestation.verify_attestation(
            _context(_token(iat=now, exp=now + 61)),
            expected_tool="search_orders",
        )


def test_non_positive_attestation_lifetime_is_rejected() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证过期时间不晚于签发时间的凭证会被拒绝。
    """
    now = int(time.time())
    with pytest.raises(PermissionError, match="时间范围无效"):
        attestation.verify_attestation(
            _context(_token(iat=now, exp=now)),
            expected_tool="search_orders",
        )


def test_unknown_chat_type_is_rejected() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证不受支持的会话类型不能成为可信调用主体。
    """
    with pytest.raises(PermissionError, match="会话类型无效"):
        attestation.verify_attestation(
            _context(_token(chat_type="unknown")),
            expected_tool="search_orders",
        )


def test_legacy_unsigned_metadata_is_rejected() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证旧版未签名 Hermes 身份元数据不再被 MCP 接受。
    """
    context = SimpleNamespace(
        request_context=SimpleNamespace(
            meta={
                "hermes/platform": "feishu",
                "hermes/user_id": "ou_legacy_sender",
            }
        )
    )

    with pytest.raises(PermissionError, match="缺少苏宁业务身份凭证"):
        attestation.verify_attestation(
            context,
            expected_tool="search_orders",
        )


def test_short_bridge_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """输入：参数 ``monkeypatch``。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证长度不足的桥接密钥不能用于身份验签。
    """
    monkeypatch.setattr(
        attestation.settings,
        "suning_mcp_bridge_secret",
        "too-short",
    )

    with pytest.raises(PermissionError, match="密钥长度不足"):
        attestation.verify_attestation(
            _context(_token()),
            expected_tool="search_orders",
        )


def test_replay_is_rejected_by_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """输入：参数 ``monkeypatch``。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证 Redis 原子消费同一 JTI 后会拒绝第二次凭证使用。
    """
    seen: set[str] = set()

    class FakeRedis:
        def set(
            self,
            key: str,
            _value: str,
            *,
            ex: int,
            nx: bool,
        ) -> bool:
            """输入：参数 ``self``、``key``、``_value``、``ex``、``nx``。

            输出：返回类型为 ``bool`` 的测试数据或测试替身结果。
            功能：模拟 Redis ``SET NX``，记录并拒绝重复键。
            """
            assert ex > 0
            assert nx is True
            if key in seen:
                return False
            seen.add(key)
            return True

    monkeypatch.setattr(attestation.settings, "redis_url", "redis://test/0")
    monkeypatch.setattr(
        attestation.Redis,
        "from_url",
        lambda *_args, **_kwargs: FakeRedis(),
    )
    token = _token(jti="one-time-jti")

    attestation.verify_attestation(
        _context(token),
        expected_tool="search_orders",
    )
    with pytest.raises(PermissionError, match="已被使用"):
        attestation.verify_attestation(
            _context(token),
            expected_tool="search_orders",
        )


def test_missing_required_replay_store_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：参数 ``monkeypatch``。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证生产要求防重放时，未配置 Redis 会默认拒绝请求。
    """
    monkeypatch.setattr(
        attestation.settings,
        "suning_authn_require_redis",
        True,
    )

    with pytest.raises(PermissionError, match="Redis 未配置"):
        attestation.verify_attestation(
            _context(_token()),
            expected_tool="search_orders",
        )


def test_replay_store_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：参数 ``monkeypatch``。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证 Redis 防重放服务异常时不会绕过身份校验。
    """
    class BrokenRedis:
        def set(self, *_args: object, **_kwargs: object) -> bool:
            """输入：参数 ``self``、``*_args``、``**_kwargs``。

            输出：返回类型为 ``bool`` 的测试数据或测试替身结果。
            功能：模拟 Redis 写入故障，触发防重放服务不可用分支。
            """
            raise attestation.RedisError("offline")

    monkeypatch.setattr(attestation.settings, "redis_url", "redis://test/0")
    monkeypatch.setattr(
        attestation.Redis,
        "from_url",
        lambda *_args, **_kwargs: BrokenRedis(),
    )

    with pytest.raises(PermissionError, match="重放保护服务不可用"):
        attestation.verify_attestation(
            _context(_token()),
            expected_tool="search_orders",
        )
