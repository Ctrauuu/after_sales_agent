"""MCP 统一授权入口：映射平台用户、收窄查询范围并脱敏结果。"""
# MCP Tool 收到请求
#     ↓
# 1. 验证身份凭证
#     ↓
# 2. 平台账号映射内部用户
#     ↓
# 3. 加载角色权限
#     ↓
# 4. 角色权限 ∩ 用户权限 ∩ 请求范围
#     ↓
# 5. 生成 safe_filters
#     ↓
# 6. safe_filters 转成 SQL 权限条件
#     ↓
# 7. 执行业务 SQL
#     ↓
# 8. 根据 data_scope 脱敏
#     ↓
# 返回结果

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

import sqlalchemy as sa
from fastmcp import Context

from mcp_suning.security.attestation import AuthenticatedPrincipal, verify_attestation
from mcp_suning.database import engine
from mcp_suning.domain_registry import CATEGORY_ALIASES, REGION_ALIASES


class DataScope(str, Enum):
    AGGREGATED = "aggregated"
    ANONYMIZED = "anonymized"
    FULL = "full"


@dataclass(frozen=True)
class PermissionPolicy:
    role_name: str
    region_filter: tuple[str, ...] | None = None
    city_filter: tuple[str, ...] | None = None
    category_filter: tuple[str, ...] | None = None
    max_date_range_days: int = 365
    data_scope: DataScope = DataScope.FULL


@dataclass(frozen=True)
class UserContext:
    user_id: str
    employee_id: str
    role: str
    region: tuple[str, ...] | None = None
    categories: tuple[str, ...] | None = None
    cities: tuple[str, ...] | None = None
    data_scope: DataScope | None = None
    display_name: str = ""


AGGREGATE_TOOLS = {"query_return_stats_nl2sql", "query_aftersale_nl2sql"}
TRUSTED_PLATFORMS = {"feishu", "wecom", "dingtalk"}
GROUP_CHAT_TYPES = {"group", "channel", "forum", "thread"}
LOCATION_SCOPED_ROLES = {"regional_manager", "cs_supervisor"}
CATEGORY_SCOPED_ROLES = {"quality_engineer"}
KNOWN_CITY_CODES = {"NJ", "SH", "HZ", "SZ"}
SCOPE_RANK = {
    DataScope.AGGREGATED: 0,
    DataScope.ANONYMIZED: 1,
    DataScope.FULL: 2,
}


def _json_value(value: Any) -> Any:
    """输入：数据库返回的 JSON 对象、数组、字符串或空值 ``value``。

    输出：解析后的 Python 对象或 ``None``。
    功能：统一解析权限 JSON，并拒绝损坏或不支持的字段类型。
    """
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value) if value.strip() else None
        except json.JSONDecodeError as exc:
            raise ValueError("权限 JSON 格式无效") from exc
    raise ValueError("权限字段必须是 JSON")


def _codes(values: Any) -> tuple[str, ...] | None:
    """输入：单个权限编码、编码序列或空值 ``values``。

    输出：去空去重的不可变编码元组；没有有效编码时返回 ``None``。
    功能：规范化角色、用户和请求中的范围编码。
    """
    if values is None:
        return None
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence):
        raise ValueError("权限范围必须是字符串数组")
    result = tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
    return result or None


def _normalize(value: str, aliases: Mapping[str, str]) -> str:
    """输入：原始编码或中文名 ``value`` 及别名映射 ``aliases``。

    输出：别名对应编码，或转为大写的原始编码。
    功能：统一区域、品类的中文名称和内部编码。
    """
    value = value.strip()
    return aliases.get(value, value.upper())


def _region_scope(value: Any) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None]:
    """输入：角色表中的区域权限 JSON ``value``。

    输出：``(区域编码, 城市编码)`` 两个可选元组。
    功能：兼容区域列表和 ``区域 -> 城市列表`` 两种配置格式。
    """
    value = _json_value(value)
    if value is None:
        return None, None
    if isinstance(value, dict):
        cities = [city for items in value.values() for city in (items or [])]
        return _codes(list(value)), _codes(cities)
    return _codes(value), None


def _within(candidate: str, allowed: str, hierarchical: bool = False) -> bool:
    """输入：候选编码、允许编码和是否启用品类层级匹配。

    输出：候选值位于允许范围内时返回 ``True``。
    功能：执行精确范围比较，并支持 ``父品类-子品类`` 层级判断。
    """
    return candidate == allowed or (
        hierarchical and candidate.startswith(f"{allowed}-")
    )


def _intersect(
    role_values: Sequence[str] | None,
    user_values: Sequence[str] | None,
    *,
    hierarchical: bool = False,
) -> tuple[str, ...] | None:
    """输入：角色范围、用户范围以及是否启用层级匹配。

    输出：两侧权限的最小有效交集；均无限制时返回 ``None``。
    功能：保证用户附加权限只能收窄角色权限，空交集时默认拒绝。
    """
    role_scope = _codes(role_values)
    user_scope = _codes(user_values)
    if role_scope is None:
        return user_scope
    if user_scope is None:
        return role_scope

    result: list[str] = []
    for role_value in role_scope:
        for user_value in user_scope:
            if _within(user_value, role_value, hierarchical):
                result.append(user_value)
            elif _within(role_value, user_value, hierarchical):
                result.append(role_value)
    if not result:
        raise PermissionError("用户权限与角色权限没有交集")
    return tuple(dict.fromkeys(result))


def _narrower(first: DataScope, second: DataScope | str | None) -> DataScope:
    """输入：两个数据粒度 ``first`` 和 ``second``。

    输出：两者中限制更严格的 ``DataScope``。
    功能：防止用户配置或群聊规则扩大角色的数据展示粒度。
    """
    if second is None:
        return first
    second = second if isinstance(second, DataScope) else DataScope(second)
    return min((first, second), key=SCOPE_RANK.__getitem__)


def load_permission_policy(role: str) -> PermissionPolicy:
    """输入：内部角色编码 ``role``。

    输出：数据库中经过格式校验的 ``PermissionPolicy``。
    功能：以 ``user_role_permission`` 为唯一策略源，加载角色范围、时间和粒度。
    """

    role = role.strip()
    if not role:
        raise PermissionError("用户未配置角色")
    with engine.connect() as connection:
        row = connection.execute(
            sa.text(
                """
                SELECT description, region_filter, category_filter,
                       max_date_range_days, data_scope
                FROM user_role_permission
                WHERE role_name = :role
                LIMIT 1
                """
            ),
            {"role": role},
        ).mappings().first()

    if row is None:
        raise PermissionError(f"未识别的角色: {role}")

    raw_regions = _json_value(row["region_filter"])
    regions, cities = _region_scope(raw_regions)
    raw_categories = _json_value(row["category_filter"])
    categories = _codes(raw_categories)
    if raw_regions is not None and regions is None and cities is None:
        raise ValueError(f"角色 {role} 的区域权限配置为空")
    if raw_categories is not None and categories is None:
        raise ValueError(f"角色 {role} 的品类权限配置为空")

    regions = tuple(_normalize(value, REGION_ALIASES) for value in regions or ()) or None
    cities = tuple(value.upper() for value in cities or ()) or None
    categories = (
        tuple(_normalize(value, CATEGORY_ALIASES) for value in categories or ()) or None
    )
    try:
        max_days = int(row["max_date_range_days"])
        scope = DataScope(str(row["data_scope"]).strip().lower())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"角色 {role} 的权限配置无效") from exc
    if max_days < 1:
        raise ValueError(f"角色 {role} 的最大查询天数必须大于 0")
    if role in LOCATION_SCOPED_ROLES and not (regions or cities):
        raise PermissionError(f"角色 {role} 未配置区域权限")
    if role in CATEGORY_SCOPED_ROLES and categories is None:
        raise PermissionError(f"角色 {role} 未配置品类权限")

    return PermissionPolicy(
        role_name=str(row["description"] or role),
        region_filter=regions,
        city_filter=cities,
        category_filter=categories,
        max_date_range_days=max_days,
        data_scope=scope,
    )


def resolve_user_context(*, platform: str, platform_user_id: str) -> UserContext:
    """输入：可信平台名 ``platform`` 和平台用户 ID ``platform_user_id``。

    输出：启用用户的内部 ``UserContext``。
    功能：通过平台绑定表映射内部用户，并解析用户级区域、城市、品类和粒度。
    """

    platform = platform.strip().lower()
    platform_user_id = platform_user_id.strip()
    if platform not in TRUSTED_PLATFORMS:
        raise PermissionError(f"不支持的消息平台: {platform}")
    if not platform_user_id:
        raise PermissionError("平台用户身份为空")

    with engine.connect() as connection:
        row = connection.execute(
            sa.text(
                """
                SELECT u.hermes_user_id, u.employee_id, u.display_name,
                       u.role, u.permissions, u.is_active
                FROM user_platform_binding AS b
                JOIN user_identity AS u
                  ON u.hermes_user_id = b.hermes_user_id
                WHERE b.platform = :platform
                  AND b.platform_user_id = :platform_user_id
                LIMIT 1
                """
            ),
            {"platform": platform, "platform_user_id": platform_user_id},
        ).mappings().first()

    if row is None:
        raise PermissionError("用户未绑定或不存在")
    if not bool(row["is_active"]):
        raise PermissionError("用户已被禁用")

    role = str(row["role"] or "").strip()
    permissions = _json_value(row["permissions"]) or {}
    if not isinstance(permissions, dict):
        raise ValueError("用户 permissions 必须是 JSON 对象")

    raw_locations = permissions.get("region", permissions.get("regions"))
    location_codes = _codes(raw_locations)
    if raw_locations is not None and location_codes is None:
        raise PermissionError("用户区域权限配置为空")
    regions: list[str] = []
    cities: list[str] = []
    for value in location_codes or ():
        code = _normalize(value, REGION_ALIASES)
        (cities if code in KNOWN_CITY_CODES else regions).append(code)

    raw_categories = permissions.get("categories", permissions.get("category"))
    category_codes = _codes(raw_categories)
    if raw_categories is not None and category_codes is None:
        raise PermissionError("用户品类权限配置为空")
    categories = (
        tuple(_normalize(value, CATEGORY_ALIASES) for value in category_codes or ())
        or None
    )
    if role in LOCATION_SCOPED_ROLES and not (regions or cities):
        raise PermissionError("用户未配置区域权限")
    if role in CATEGORY_SCOPED_ROLES and categories is None:
        raise PermissionError("用户未配置品类权限")

    raw_scope = permissions.get("data_scope")
    try:
        user_scope = DataScope(str(raw_scope).lower()) if raw_scope else None
    except ValueError as exc:
        raise ValueError("用户 data_scope 权限配置无效") from exc

    return UserContext(
        user_id=str(row["hermes_user_id"]),
        employee_id=str(row["employee_id"]),
        role=role,
        region=tuple(regions) or None,
        categories=categories,
        cities=tuple(cities) or None,
        data_scope=user_scope,
        display_name=str(row["display_name"] or ""),
    )


class PermissionInterceptor:
    def __init__(
        self,
        policy_loader: Callable[[str], PermissionPolicy] = load_permission_policy,
    ) -> None:
        """输入：按角色加载权限策略的 ``policy_loader``。

        输出：初始化后的权限拦截器实例状态。
        功能：保存策略加载入口，便于生产读取数据库和测试注入固定策略。
        """
        self.policy_loader = policy_loader

    def intercept(
        self,
        user: UserContext,
        tool_name: str,
        filters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """输入：可信用户、工具名和模型提供的业务筛选条件。

        输出：只能用于后续 SQL 的服务端安全筛选字典。
        功能：过滤控制字段、求权限交集、校验越权并收窄时间与数据粒度。
        """
        policy = self.policy_loader(user.role)
        # 权限字段始终由服务端生成，即使内部调用绕过插件 schema 也不接受。
        safe = {
            key: value
            for key, value in (filters or {}).items()
            if key not in {
                "data_scope",
                "detail_level",
                "user_context",
                "allowed_regions",
                "allowed_cities",
                "allowed_categories",
            }
        }
        scope = _narrower(policy.data_scope, user.data_scope)
        if scope is DataScope.AGGREGATED and tool_name not in AGGREGATE_TOOLS:
            raise PermissionError(
                f"角色 {policy.role_name} 仅允许汇总查询，不能调用 {tool_name}"
            )

        regions = _intersect(policy.region_filter, user.region)
        cities = _intersect(policy.city_filter, user.cities)
        categories = _intersect(
            policy.category_filter,
            user.categories,
            hierarchical=True,
        )
        self._check_requested(safe, "region", regions, REGION_ALIASES)
        self._check_requested(safe, "city", cities, {})
        self._check_requested(
            safe,
            "category",
            categories,
            CATEGORY_ALIASES,
            hierarchical=True,
        )

        if regions is not None:
            safe["allowed_regions"] = list(regions)
        if cities is not None:
            safe["allowed_cities"] = list(cities)
        if categories is not None:
            safe["allowed_categories"] = list(categories)

        requested_days = safe.get("date_range_days")
        try:
            days = policy.max_date_range_days if requested_days in (None, "") else int(requested_days)
        except (TypeError, ValueError) as exc:
            raise ValueError("date_range_days 必须是整数") from exc
        if days < 1:
            raise ValueError("date_range_days 必须大于或等于 1")
        safe["date_range_days"] = min(days, policy.max_date_range_days)
        safe["data_scope"] = scope.value
        return safe

    @staticmethod
    def _check_requested(
        filters: Mapping[str, Any],
        key: str,
        allowed: Sequence[str] | None,
        aliases: Mapping[str, str],
        *,
        hierarchical: bool = False,
    ) -> None:
        """输入：请求筛选、字段名、允许范围、别名和层级匹配开关。

        输出：无；请求值超出权限时抛出 ``PermissionError``。
        功能：验证用户显式指定的区域、城市或品类是否位于授权范围。
        """
        requested = filters.get(key)
        if requested in (None, "", []) or allowed is None:
            return
        for value in _codes(requested) or ():
            normalized = _normalize(value, aliases)
            if not any(_within(normalized, item, hierarchical) for item in allowed):
                raise PermissionError(
                    f"请求的 {key}={value} 超出权限范围 {list(allowed)}"
                )

    def mask_sensitive_data(self, data: Any, data_scope: DataScope | str) -> Any:
        """输入：任意嵌套查询结果 ``data`` 和最终数据粒度 ``data_scope``。

        输出：原始完整数据或递归脱敏后的数据副本。
        功能：按授权粒度统一隐藏手机号、人员标识和金额字段。
        """
        scope = data_scope if isinstance(data_scope, DataScope) else DataScope(data_scope)
        return data if scope is DataScope.FULL else self._mask(data)

    def _mask(self, value: Any, key: str = "") -> Any:
        """输入：当前结果节点 ``value`` 和对应字段名 ``key``。

        输出：保持容器结构的脱敏节点值。
        功能：递归遍历映射和序列，并按敏感字段类型执行遮蔽。
        """
        if isinstance(value, Mapping):
            return {
                item_key: self._mask(item_value, str(item_key).lower())
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [self._mask(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._mask(item) for item in value)
        if key in {"phone", "telephone", "mobile"}:
            text = str(value or "")
            return f"{text[:3]}****{text[-4:]}" if len(text) >= 11 else "***"
        if key in {
            "user_id", "customer_id", "employee_id", "engineer_id", "operator_id"
        }:
            text = str(value or "")
            return f"***{text[-4:]}" if text else ""
        if key in {
            "order_amount", "order_amount_yuan", "return_amount",
            "return_amount_yuan", "refund_amount", "refund_amount_yuan",
            "total_amount", "total_amount_yuan",
        }:
            return "***" if value is not None else None
        return value


interceptor = PermissionInterceptor()


def authorize_mcp_request(
    ctx: Context,
    tool_name: str,
    filters: Mapping[str, Any] | None = None,
) -> tuple[UserContext, dict[str, Any]]:
    """输入：FastMCP 上下文、当前工具名和普通业务筛选条件。

    输出：可信内部用户及最终 ``safe_filters``。
    功能：复用保留主体的授权流程，并仅向普通 MCP 工具返回用户和经过 RBAC 收窄的查询范围。
    """
    _, user, safe = authorize_mcp_request_with_principal(ctx, tool_name, filters)
    return user, safe


def authorize_mcp_request_with_principal(
    ctx: Context,
    tool_name: str,
    filters: Mapping[str, Any] | None = None,
) -> tuple[AuthenticatedPrincipal, UserContext, dict[str, Any]]:
    """输入：FastMCP 上下文、当前工具名和模型提供的普通业务筛选条件。

    输出：已验证外部主体、可信内部用户及最终 ``safe_filters``。
    功能：在标准授权结果之外保留经过验证的身份，供跨 MCP 聚合服务签发最小范围的下游委托凭证。
    """

    principal = verify_attestation(ctx, expected_tool=tool_name)
    user = resolve_user_context(
        platform=principal.platform,
        platform_user_id=principal.external_subject,
    )
    safe = interceptor.intercept(user, tool_name, filters)
    if principal.chat_type in GROUP_CHAT_TYPES:
        safe["data_scope"] = _narrower(
            DataScope(safe["data_scope"]),
            DataScope.ANONYMIZED,
        ).value
    return principal, user, safe


def build_scope_clause(
    safe_filters: Mapping[str, Any],
    *,
    region_column: str | None = None,
    city_column: str | None = None,
    category_column: str | None = None,
    parameter_prefix: str = "rbac",
) -> tuple[str, dict[str, Any]]:
    """输入：安全筛选、业务代码指定的列名和参数名前缀。

    输出：以 ``AND`` 开头的 SQL 片段及 SQLAlchemy 绑定参数。
    功能：将区域、城市和品类授权范围转换为参数化行级过滤条件。
    """

    clauses: list[str] = []
    params: dict[str, Any] = {}
    for values, column, suffix in (
        (safe_filters.get("allowed_regions"), region_column, "region"),
        (safe_filters.get("allowed_cities"), city_column, "city"),
    ):
        if column is None or not values:
            continue
        names = []
        for index, value in enumerate(values):
            name = f"{parameter_prefix}_{suffix}_{index}"
            names.append(f":{name}")
            params[name] = value
        clauses.append(f"{column} IN ({', '.join(names)})")

    categories = safe_filters.get("allowed_categories")
    if category_column is not None and categories:
        category_clauses = []
        for index, value in enumerate(categories):
            exact = f"{parameter_prefix}_category_{index}"
            child = f"{parameter_prefix}_category_child_{index}"
            category_clauses.append(
                f"({category_column} = :{exact} OR {category_column} LIKE :{child})"
            )
            params[exact] = value
            params[child] = f"{value}-%"
        clauses.append(f"({' OR '.join(category_clauses)})")

    if not clauses:
        return "", params
    return " AND " + " AND ".join(f"({clause})" for clause in clauses), params


__all__ = [
    "AGGREGATE_TOOLS",
    "CATEGORY_ALIASES",
    "DataScope",
    "PermissionInterceptor",
    "PermissionPolicy",
    "UserContext",
    "authorize_mcp_request",
    "authorize_mcp_request_with_principal",
    "build_scope_clause",
    "interceptor",
    "load_permission_policy",
    "resolve_user_context",
]
