from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastmcp import Client, Context, FastMCP

import mcp_suning.security.rbac as auth
from mcp_suning.security.attestation import AuthenticatedPrincipal
from mcp_suning.security.rbac import (
    DataScope,
    PermissionInterceptor,
    PermissionPolicy,
    UserContext,
    build_scope_clause,
)


def _interceptor(policy: PermissionPolicy) -> PermissionInterceptor:
    """输入：参数 ``policy``。

    输出：返回类型为 ``PermissionInterceptor`` 的测试数据或测试替身结果。
    功能：创建使用固定角色策略的权限拦截器，隔离数据库依赖。
    """
    return PermissionInterceptor(lambda _role: policy)


def _principal(chat_type: str = "dm") -> AuthenticatedPrincipal:
    """输入：参数 ``chat_type``。

    输出：返回类型为 ``AuthenticatedPrincipal`` 的测试数据或测试替身结果。
    功能：构造签名验证后的固定飞书主体，供统一授权测试使用。
    """
    return AuthenticatedPrincipal(
        platform="feishu",
        identity_issuer="suning-feishu-primary",
        external_subject="ou_real_sender",
        chat_type=chat_type,
        message_id="om_test",
        jti="jti_test",
    )


class _FakeResult:
    def __init__(self, row: dict) -> None:
        """输入：参数 ``self``、``row``。

        输出：无显式返回值；初始化测试替身实例状态。
        功能：保存数据库测试替身需要返回的单行数据。
        """
        self._row = row

    def mappings(self) -> "_FakeResult":
        """输入：参数 ``self``。

        输出：返回类型为 ``'_FakeResult'`` 的测试数据或测试替身结果。
        功能：模拟 SQLAlchemy 结果对象的 ``mappings`` 链式接口。
        """
        return self

    def first(self) -> dict:
        """输入：参数 ``self``。

        输出：返回类型为 ``dict`` 的测试数据或测试替身结果。
        功能：模拟 SQLAlchemy 查询返回第一行映射数据。
        """
        return self._row


class _FakeConnection:
    def __init__(self, row: dict) -> None:
        """输入：参数 ``self``、``row``。

        输出：无显式返回值；初始化测试替身实例状态。
        功能：保存连接测试替身执行查询后需要返回的单行数据。
        """
        self._row = row

    def __enter__(self) -> "_FakeConnection":
        """输入：参数 ``self``。

        输出：返回类型为 ``'_FakeConnection'`` 的测试数据或测试替身结果。
        功能：模拟数据库连接上下文管理器进入操作。
        """
        return self

    def __exit__(self, *_args: object) -> None:
        """输入：参数 ``self``、``*_args``。

        输出：无显式返回值；用于当前测试或辅助流程。
        功能：模拟数据库连接上下文管理器退出操作。
        """
        return None

    def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
        """输入：参数 ``self``、``*_args``、``**_kwargs``。

        输出：返回类型为 ``_FakeResult`` 的测试数据或测试替身结果。
        功能：模拟执行 SQL，并返回支持映射读取的结果替身。
        """
        return _FakeResult(self._row)


class _FakeEngine:
    def __init__(self, row: dict) -> None:
        """输入：参数 ``self``、``row``。

        输出：无显式返回值；初始化测试替身实例状态。
        功能：保存引擎测试替身创建连接时使用的固定行数据。
        """
        self._row = row

    def connect(self) -> _FakeConnection:
        """输入：参数 ``self``。

        输出：返回类型为 ``_FakeConnection`` 的测试数据或测试替身结果。
        功能：模拟 SQLAlchemy Engine 创建数据库连接。
        """
        return _FakeConnection(self._row)


def test_intercept_injects_all_dynamic_query_boundaries() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证角色、用户、请求、时间和粒度边界都会进入安全筛选。
    """
    middleware = _interceptor(
        PermissionPolicy(
            role_name="区域经理",
            region_filter=("HD",),
            city_filter=("NJ", "SH"),
            category_filter=("C1",),
            max_date_range_days=90,
            data_scope=DataScope.ANONYMIZED,
        )
    )
    user = UserContext(
        user_id="U-H002",
        employee_id="E10002",
        role="regional_manager",
        region=("HD",),
        cities=("NJ",),
        categories=("C1-AC",),
        # 用户声明 full 也不能突破角色的 anonymized 上限。
        data_scope=DataScope.FULL,
    )

    safe = middleware.intercept(
        user,
        "search_orders",
        {
            "region": "华东",
            "city": "NJ",
            "category": "壁挂式空调",
            "date_range_days": 365,
            "detail_level": "full",
        },
    )

    assert safe["allowed_regions"] == ["HD"]
    assert safe["allowed_cities"] == ["NJ"]
    assert safe["allowed_categories"] == ["C1-AC"]
    assert safe["date_range_days"] == 90
    assert "detail_level" not in safe
    assert safe["data_scope"] == "anonymized"


def test_intercept_rejects_requested_scope_outside_role() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证权限拦截器拒绝超出角色区域范围的显式请求。
    """
    middleware = _interceptor(
        PermissionPolicy(
            role_name="区域经理",
            region_filter=("HD",),
            data_scope=DataScope.ANONYMIZED,
        )
    )
    user = UserContext("U-H002", "E10002", "regional_manager")

    with pytest.raises(PermissionError, match="region=华北"):
        middleware.intercept(user, "search_orders", {"region": "华北"})


def test_category_scope_allows_children_but_rejects_siblings() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证品类权限允许子品类，但拒绝同级或无关品类。
    """
    middleware = _interceptor(
        PermissionPolicy(
            role_name="品控工程师",
            category_filter=("C1-AC",),
        )
    )
    user = UserContext("U-H003", "E10003", "quality_engineer")

    safe = middleware.intercept(
        user,
        "search_products",
        {"category": "C1-AC-WG"},
    )
    assert safe["allowed_categories"] == ["C1-AC"]

    with pytest.raises(PermissionError, match="category=电视"):
        middleware.intercept(
            user,
            "search_products",
            {"category": "电视"},
        )


def test_aggregated_scope_blocks_detail_tools_and_allows_real_aggregate() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证汇总角色只能调用明确登记的统计工具，不能查询明细。
    """
    middleware = _interceptor(
        PermissionPolicy(
            role_name="高层决策",
            data_scope=DataScope.AGGREGATED,
        )
    )
    user = UserContext("U-H004", "E10004", "vp_executive")

    with pytest.raises(PermissionError, match="仅允许汇总查询"):
        middleware.intercept(user, "get_refund_status", {})

    safe = middleware.intercept(user, "query_return_stats_nl2sql", {})
    assert safe["data_scope"] == "aggregated"
    assert safe["date_range_days"] == 365

    nl2sql_safe = middleware.intercept(
        user,
        "query_aftersale_nl2sql",
        {},
    )
    assert nl2sql_safe["data_scope"] == "aggregated"


def test_aggregated_scope_blocks_unknown_future_tool() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证未来新增的未知工具不会默认开放给汇总角色。
    """
    middleware = _interceptor(
        PermissionPolicy(
            role_name="高层决策",
            data_scope=DataScope.AGGREGATED,
        )
    )
    user = UserContext("U-H004", "E10004", "vp_executive")

    with pytest.raises(PermissionError, match="仅允许汇总查询"):
        middleware.intercept(user, "new_detail_tool", {})


def test_database_is_the_only_role_policy_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：参数 ``monkeypatch``。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证角色时间和粒度直接以数据库策略为准，不叠加代码兜底。
    """
    monkeypatch.setattr(
        auth,
        "engine",
        _FakeEngine(
            {
                "role_name": "cs_supervisor",
                "description": "客服主管",
                "region_filter": {"HD": ["NJ"]},
                "category_filter": None,
                "max_date_range_days": 365,
                "data_scope": "full",
            }
        ),
    )
    policy = auth.load_permission_policy("cs_supervisor")

    assert policy.max_date_range_days == 365
    assert policy.data_scope is DataScope.FULL


def test_explicit_empty_user_category_scope_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：参数 ``monkeypatch``。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证品控用户显式配置空品类范围时默认拒绝授权。
    """
    monkeypatch.setattr(
        auth,
        "engine",
        _FakeEngine(
            {
                "hermes_user_id": "U-H003",
                "employee_id": "E10003",
                "display_name": "测试品控",
                "role": "quality_engineer",
                "permissions": {"categories": ["", ""]},
                "is_active": 1,
            }
        ),
    )
    with pytest.raises(PermissionError, match="用户品类权限配置为空"):
        auth.resolve_user_context(
            platform="dingtalk",
            platform_user_id="ding_test",
        )


def test_user_can_only_reduce_data_scope() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证用户级数据粒度只能比角色策略更严格，不能扩大权限。
    """
    middleware = _interceptor(
        PermissionPolicy(role_name="售后运营", data_scope=DataScope.FULL)
    )
    user = UserContext(
        "U-H001",
        "E10001",
        "aftersale_ops",
        data_scope=DataScope.ANONYMIZED,
    )

    safe = middleware.intercept(user, "search_orders", {})
    assert safe["data_scope"] == "anonymized"


def test_mask_sensitive_data_is_recursive_and_does_not_mutate_input() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证脱敏递归处理嵌套字段且不修改原始查询结果。
    """
    middleware = _interceptor(PermissionPolicy(role_name="区域经理"))
    source = {
        "phone": "13800000001",
        "order_amount": 329900,
        "nested": [
            {
                "user_id": "U12001",
                "engineer_id": "E001",
                "refund_amount_yuan": 2999,
            }
        ],
    }
    original = deepcopy(source)

    masked = middleware.mask_sensitive_data(source, DataScope.ANONYMIZED)

    assert source == original
    assert masked["phone"] == "138****0001"
    assert masked["order_amount"] == "***"
    assert masked["nested"][0]["user_id"] == "***2001"
    assert masked["nested"][0]["engineer_id"] == "***E001"
    assert masked["nested"][0]["refund_amount_yuan"] == "***"


def test_scope_clause_uses_bound_parameters_and_category_hierarchy() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证行级 SQL 使用绑定参数并包含父子品类范围。
    """
    clause, params = build_scope_clause(
        {
            "allowed_regions": ["HD"],
            "allowed_cities": ["NJ"],
            "allowed_categories": ["C1-AC"],
        },
        region_column="o.region_code",
        city_column="o.city_code",
        category_column="s.category_l3_code",
    )

    assert "o.region_code IN (:rbac_region_0)" in clause
    assert "o.city_code IN (:rbac_city_0)" in clause
    assert "s.category_l3_code = :rbac_category_0" in clause
    assert "s.category_l3_code LIKE :rbac_category_child_0" in clause
    assert "HD" not in clause
    assert params == {
        "rbac_region_0": "HD",
        "rbac_city_0": "NJ",
        "rbac_category_0": "C1-AC",
        "rbac_category_child_0": "C1-AC-%",
    }


def test_authorize_mcp_request_only_uses_verified_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：参数 ``monkeypatch``。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证统一授权只使用验签主体映射用户，忽略业务参数中的伪造身份。
    """
    expected_user = UserContext("U-H001", "E10001", "aftersale_ops")
    captured: dict[str, str] = {}

    def fake_resolve_user_context(*, platform: str, platform_user_id: str) -> UserContext:
        """输入：参数 ``platform``、``platform_user_id``。

        输出：返回类型为 ``UserContext`` 的测试数据或测试替身结果。
        功能：记录平台身份映射参数并返回固定内部用户。
        """
        captured.update(platform=platform, platform_user_id=platform_user_id)
        return expected_user

    monkeypatch.setattr(auth, "resolve_user_context", fake_resolve_user_context)
    monkeypatch.setattr(
        auth,
        "verify_attestation",
        lambda _ctx, *, expected_tool: (
            captured.update(expected_tool=expected_tool) or _principal()
        ),
    )
    monkeypatch.setattr(
        auth,
        "interceptor",
        _interceptor(PermissionPolicy(role_name="售后运营")),
    )
    context = SimpleNamespace(request_context=SimpleNamespace(meta={}))

    user, safe = auth.authorize_mcp_request(
        context,
        "search_orders",
        # 这里即使出现看似身份的普通过滤值，也不会参与身份解析。
        {"user_context": {"role": "vp_executive"}},
    )

    assert user is expected_user
    assert captured == {
        "expected_tool": "search_orders",
        "platform": "feishu",
        "platform_user_id": "ou_real_sender",
    }
    assert safe["data_scope"] == "full"


def test_authorize_mcp_request_propagates_attestation_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：参数 ``monkeypatch``。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证凭证校验失败会直接终止统一授权流程。
    """
    def reject(_ctx: Context, *, expected_tool: str) -> None:
        """输入：参数 ``_ctx``、``expected_tool``。

        输出：无显式返回值；用于当前测试或辅助流程。
        功能：模拟身份凭证签名验证失败。
        """
        del expected_tool
        raise PermissionError("身份凭证签名无效")

    monkeypatch.setattr(auth, "verify_attestation", reject)
    context = SimpleNamespace(
        request_context=SimpleNamespace(
            meta={
                "hermes/platform": "feishu",
                "hermes/user_id": "ou_legacy_sender",
            }
        )
    )

    with pytest.raises(PermissionError, match="签名无效"):
        auth.authorize_mcp_request(context, "search_orders", {})


def test_group_chat_restricts_full_user_to_anonymized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：参数 ``monkeypatch``。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证群聊请求会把完整明细权限降为匿名明细。
    """
    user = UserContext("U-H001", "E10001", "aftersale_ops")
    monkeypatch.setattr(
        auth,
        "verify_attestation",
        lambda _ctx, *, expected_tool: _principal("group"),
    )
    monkeypatch.setattr(auth, "resolve_user_context", lambda **_kwargs: user)
    monkeypatch.setattr(
        auth,
        "interceptor",
        _interceptor(PermissionPolicy(role_name="售后运营")),
    )

    _, safe = auth.authorize_mcp_request(
        SimpleNamespace(request_context=SimpleNamespace(meta={})),
        "search_orders",
        {},
    )

    assert safe["data_scope"] == "anonymized"
    assert "detail_level" not in safe


@pytest.mark.asyncio
async def test_real_fastmcp_call_meta_reaches_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：参数 ``monkeypatch``。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证 Hermes 修改写入的 tools/call._meta 能穿过真实 FastMCP 调用链。
    """

    expected_user = UserContext("U-H001", "E10001", "aftersale_ops")
    captured: dict[str, str] = {}

    def fake_resolve_user_context(*, platform: str, platform_user_id: str) -> UserContext:
        """输入：参数 ``platform``、``platform_user_id``。

        输出：返回类型为 ``UserContext`` 的测试数据或测试替身结果。
        功能：记录真实 FastMCP 调用传入的平台主体并返回固定用户。
        """
        captured.update(platform=platform, platform_user_id=platform_user_id)
        return expected_user

    def fake_verify_attestation(
        ctx: Context,
        *,
        expected_tool: str,
    ) -> AuthenticatedPrincipal:
        """输入：参数 ``ctx``、``expected_tool``。

        输出：返回类型为 ``AuthenticatedPrincipal`` 的测试数据或测试替身结果。
        功能：从真实 FastMCP Context 读取私有元数据并返回固定验签主体。
        """
        metadata = ctx.request_context.meta
        if hasattr(metadata, "model_dump"):
            metadata = metadata.model_dump(exclude_none=True)
        captured["token"] = metadata["suning/authn"]
        captured["expected_tool"] = expected_tool
        return _principal()

    monkeypatch.setattr(auth, "resolve_user_context", fake_resolve_user_context)
    monkeypatch.setattr(auth, "verify_attestation", fake_verify_attestation)
    monkeypatch.setattr(
        auth,
        "interceptor",
        _interceptor(PermissionPolicy(role_name="售后运营")),
    )

    server = FastMCP("rbac-meta-probe")

    @server.tool
    def probe(ctx: Context) -> dict[str, str]:
        """输入：参数 ``ctx``。

        输出：返回类型为 ``dict[str, str]`` 的测试数据或测试替身结果。
        功能：通过测试 MCP 工具执行统一授权并返回最终数据粒度。
        """
        _, safe = auth.authorize_mcp_request(ctx, "query_return_stats_nl2sql", {})
        return {"data_scope": safe["data_scope"]}

    async with Client(server) as client:
        result = await client.call_tool(
            "probe",
            meta={"suning/authn": "signed-test-token"},
        )

    assert result.data == {"data_scope": "full"}
    assert captured == {
        "token": "signed-test-token",
        "expected_tool": "query_return_stats_nl2sql",
        "platform": "feishu",
        "platform_user_id": "ou_real_sender",
    }
