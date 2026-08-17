"""用户、角色和 IM 身份绑定管理路由。"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any, Mapping

import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from mcp_suning.domain_registry import CATEGORY_ALIASES, REGION_ALIASES

from . import runtime
from .common import (
    CITY_CODES,
    DATA_SCOPE_LABELS,
    LABEL_DATA_SCOPES,
    PLATFORMS,
    ApiError,
    _json_value,
    _query_int,
    _request_payload,
)
from .runtime import LOCAL_TIMEZONE


router = APIRouter(prefix="/api/admin")
REGION_NAMES = {value: key for key, value in REGION_ALIASES.items()}
CATEGORY_NAMES = {value: key for key, value in CATEGORY_ALIASES.items()}
_EMPLOYEE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,20}$")


def _friendly_region(value: Any) -> str:
    """输入：区域中文名或内部编码 ``value``。

    输出：管理后台可读的区域名称。
    功能：避免编辑已有权限时把内部编码直接暴露给管理员。
    """

    text = str(value).strip()
    return REGION_NAMES.get(text, text)


def _friendly_category(value: Any) -> str:
    """输入：品类中文名或内部编码 ``value``。

    输出：管理后台可读的精确品类名称。
    功能：保留空调、冰箱等细粒度范围，防止编辑时无意扩大权限。
    """

    text = str(value).strip()
    return CATEGORY_NAMES.get(text, text)


def _stored_permissions(payload: Mapping[str, Any]) -> dict[str, Any]:
    """输入：已通过校验的用户表单 ``payload``。

    输出：RBAC 可直接读取的内部编码权限字典。
    功能：转换区域、品类与数据粒度，同时保留最小权限语义。
    """

    return {
        "region": [REGION_ALIASES.get(item, item) for item in payload["regions"]],
        "categories": [CATEGORY_ALIASES.get(item, item) for item in payload["categories"]],
        "data_scope": LABEL_DATA_SCOPES[payload["granularity"]],
    }


def _bindings_for_user(connection: sa.Connection, user_id: str) -> list[dict[str, str]]:
    """输入：数据库连接 ``connection`` 与内部用户 ID ``user_id``。

    输出：该用户按平台排序的绑定数组。
    功能：把现有 ``user_platform_binding`` 表转换为前端标签结构。
    """

    rows = connection.execute(
        sa.text(
            """
            SELECT platform, platform_user_id
            FROM user_platform_binding
            WHERE hermes_user_id = :user_id
            ORDER BY platform
            """
        ),
        {"user_id": user_id},
    ).mappings()
    return [
        {"platform": str(row["platform"]), "user_id": str(row["platform_user_id"])}
        for row in rows
    ]


def _serialize_user(connection: sa.Connection, row: Mapping[str, Any]) -> dict[str, Any]:
    """输入：数据库连接及 ``user_identity`` 联合角色行 ``row``。

    输出：符合用户管理页字段约定的字典。
    功能：解析权限 JSON、角色中文名、状态、时间与平台绑定。
    """

    permissions = _json_value(row.get("permissions"), {})
    regions = permissions.get("region", permissions.get("regions", []))
    categories = permissions.get("categories", permissions.get("category", []))
    if not regions:
        role_regions = _json_value(row.get("role_region_filter"), None)
        regions = list(role_regions) if isinstance(role_regions, dict) else role_regions
        if role_regions is None:
            regions = list(REGION_ALIASES.values())
    if not categories:
        role_categories = _json_value(row.get("role_category_filter"), None)
        categories = role_categories
        if role_categories is None:
            categories = [code for code in CATEGORY_NAMES if "-" not in code]
    if isinstance(regions, str):
        regions = [regions]
    if isinstance(categories, str):
        categories = [categories]
    scope = str(permissions.get("data_scope") or row.get("role_data_scope") or "full")
    created_at = row.get("created_at")
    return {
        "id": str(row["hermes_user_id"]),
        "employee_id": str(row["employee_id"]),
        "name": str(row.get("display_name") or ""),
        "role": str(row.get("role") or ""),
        "role_label": str(row.get("role_label") or row.get("role") or ""),
        "regions": [_friendly_region(item) for item in regions or []],
        "categories": [_friendly_category(item) for item in categories or []],
        "granularity": DATA_SCOPE_LABELS.get(scope, scope),
        "enabled": bool(row.get("is_active")),
        "created_at": created_at.strftime("%Y-%m-%d %H:%M") if hasattr(created_at, "strftime") else str(created_at or ""),
        "bindings": _bindings_for_user(connection, str(row["hermes_user_id"])),
    }


def _user_query() -> str:
    """输入：无。

    输出：用户与角色信息的公共 SQL。
    功能：让列表、详情和写操作响应复用同一字段契约。
    """

    return """
        SELECT u.hermes_user_id, u.employee_id, u.display_name, u.role,
               u.permissions, u.is_active, u.created_at,
               r.description AS role_label, r.data_scope AS role_data_scope,
               r.region_filter AS role_region_filter,
               r.category_filter AS role_category_filter
        FROM user_identity AS u
        LEFT JOIN user_role_permission AS r ON r.role_name = u.role
    """


def _get_user(connection: sa.Connection, user_id: str) -> dict[str, Any]:
    """输入：数据库连接 ``connection`` 与内部用户 ID ``user_id``。

    输出：序列化用户；不存在时抛出 404 ``ApiError``。
    功能：集中处理用户写接口保存后的最新响应。
    """

    row = connection.execute(
        sa.text(_user_query() + " WHERE u.hermes_user_id = :user_id LIMIT 1"),
        {"user_id": user_id},
    ).mappings().first()
    if row is None:
        raise ApiError(404, "用户不存在")
    return _serialize_user(connection, row)


def _validate_user_payload(connection: sa.Connection, payload: Any) -> dict[str, Any]:
    """输入：数据库连接与用户新增/编辑 JSON ``payload``。

    输出：去除首尾空白且字段完整的表单字典。
    功能：在管理写入边界校验工号、角色、权限范围和数据粒度。
    """

    if not isinstance(payload, dict):
        raise ApiError(400, "请求体必须是 JSON 对象")
    normalized = {
        "employee_id": str(payload.get("employee_id") or "").strip(),
        "name": str(payload.get("name") or "").strip(),
        "role": str(payload.get("role") or "").strip(),
        "regions": payload.get("regions"),
        "categories": payload.get("categories"),
        "granularity": str(payload.get("granularity") or "").strip(),
    }
    if not _EMPLOYEE_ID_RE.fullmatch(normalized["employee_id"]):
        raise ApiError(400, "员工工号格式无效")
    if not normalized["name"] or len(normalized["name"]) > 80:
        raise ApiError(400, "姓名不能为空且不能超过 80 字")
    if not isinstance(normalized["regions"], list) or not normalized["regions"]:
        raise ApiError(400, "至少选择一个区域权限")
    if not isinstance(normalized["categories"], list) or not normalized["categories"]:
        raise ApiError(400, "至少选择一个品类权限")
    if normalized["granularity"] not in LABEL_DATA_SCOPES:
        raise ApiError(400, "数据粒度无效")
    role_exists = connection.execute(
        sa.text("SELECT 1 FROM user_role_permission WHERE role_name = :role LIMIT 1"),
        {"role": normalized["role"]},
    ).first()
    if role_exists is None:
        raise ApiError(400, "角色不存在")
    normalized["regions"] = [str(item).strip() for item in normalized["regions"] if str(item).strip()]
    normalized["categories"] = [str(item).strip() for item in normalized["categories"] if str(item).strip()]
    valid_regions = set(REGION_ALIASES) | set(REGION_ALIASES.values()) | CITY_CODES
    valid_categories = set(CATEGORY_ALIASES) | set(CATEGORY_ALIASES.values())
    if not normalized["regions"] or any(item not in valid_regions for item in normalized["regions"]):
        raise ApiError(400, "区域权限包含未登记值")
    if not normalized["categories"] or any(item not in valid_categories for item in normalized["categories"]):
        raise ApiError(400, "品类权限包含未登记值")
    return normalized


@router.get("/roles")
def list_roles(request: Request) -> JSONResponse:
    """输入：管理后台角色请求 ``request``。

    输出：角色、可选区域、精确品类和数据粒度 JSON。
    功能：从 RBAC 角色表加载选项，并让用户编辑表单覆盖现有细粒度权限。
    """

    del request
    with runtime.engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT role_name, description FROM user_role_permission ORDER BY role_name")
        ).mappings()
        items = [{"value": str(row["role_name"]), "label": str(row["description"] or row["role_name"])} for row in rows]
    return JSONResponse(
        {
            "items": items,
            "regions": [*REGION_ALIASES, *sorted(CITY_CODES)],
            "categories": list(CATEGORY_ALIASES),
            "granularities": list(LABEL_DATA_SCOPES),
        }
    )


@router.get("/users")
def list_users(request: Request) -> JSONResponse:
    """输入：包含角色、状态、关键词和分页参数的请求 ``request``。

    输出：分页用户与总数 JSON。
    功能：查询现有身份表，支持验收要求的筛选和分页。
    """

    params = request.query_params
    clauses = ["1=1"]
    values: dict[str, Any] = {}
    role = params.get("role", "").strip()
    status = params.get("status", "").strip()
    keyword = params.get("keyword", "").strip()
    if role:
        clauses.append("u.role = :role")
        values["role"] = role
    if status in {"enabled", "disabled"}:
        clauses.append("u.is_active = :is_active")
        values["is_active"] = status == "enabled"
    if keyword:
        clauses.append("(u.employee_id LIKE :keyword OR u.display_name LIKE :keyword)")
        values["keyword"] = f"%{keyword}%"
    page = _query_int(request, "page", 1, 1, 100000)
    size = _query_int(request, "size", 10, 1, 100)
    where = " AND ".join(clauses)
    with runtime.engine.connect() as connection:
        total = int(
            connection.execute(
                sa.text(f"SELECT COUNT(*) FROM user_identity AS u WHERE {where}"), values
            ).scalar_one()
        )
        rows = connection.execute(
            sa.text(_user_query() + f" WHERE {where} ORDER BY u.created_at DESC LIMIT :size OFFSET :offset"),
            {**values, "size": size, "offset": (page - 1) * size},
        ).mappings()
        items = [_serialize_user(connection, row) for row in rows]
    return JSONResponse({"items": items, "total": total})


@router.post("/users")
async def create_user(request: Request) -> JSONResponse:
    """输入：包含工号、姓名、角色和权限范围的 JSON 请求 ``request``。

    输出：创建后的用户 JSON；重复工号返回 409。
    功能：在现有 ``user_identity`` 表中创建可供 Agent 身份映射的真实用户。
    """

    payload = await _request_payload(request)
    try:
        with runtime.engine.begin() as connection:
            user = _validate_user_payload(connection, payload)
            existing = connection.execute(
                sa.text("SELECT 1 FROM user_identity WHERE employee_id = :employee_id LIMIT 1"),
                {"employee_id": user["employee_id"]},
            ).first()
            if existing is not None:
                raise ApiError(409, "该工号已存在")
            user_id = f"U-H{uuid.uuid4().hex[:12].upper()}"
            connection.execute(
                sa.text(
                    """
                    INSERT INTO user_identity (
                        hermes_user_id, employee_id, display_name, role,
                        permissions, is_active, created_at
                    ) VALUES (
                        :user_id, :employee_id, :display_name, :role,
                        :permissions, :is_active, :created_at
                    )
                    """
                ),
                {
                    "user_id": user_id,
                    "employee_id": user["employee_id"],
                    "display_name": user["name"],
                    "role": user["role"],
                    "permissions": json.dumps(_stored_permissions(user), ensure_ascii=False),
                    "is_active": True,
                    "created_at": datetime.now(LOCAL_TIMEZONE).replace(tzinfo=None),
                },
            )
            result = _get_user(connection, user_id)
    except IntegrityError as exc:
        raise ApiError(409, "该工号已存在") from exc
    return JSONResponse(result, status_code=201)


@router.put("/users/{user_id}")
async def update_user(request: Request) -> JSONResponse:
    """输入：路径用户 ID 与完整用户表单 JSON。

    输出：更新后的用户 JSON。
    功能：原地修改现有身份的姓名、角色和最小权限范围。
    """

    user_id = request.path_params["user_id"]
    payload = await _request_payload(request)
    with runtime.engine.begin() as connection:
        user = _validate_user_payload(connection, payload)
        if connection.execute(
            sa.text("SELECT 1 FROM user_identity WHERE hermes_user_id = :user_id LIMIT 1"),
            {"user_id": user_id},
        ).first() is None:
            raise ApiError(404, "用户不存在")
        connection.execute(
            sa.text(
                """
                UPDATE user_identity
                SET display_name = :display_name, role = :role, permissions = :permissions
                WHERE hermes_user_id = :user_id
                """
            ),
            {
                "display_name": user["name"],
                "role": user["role"],
                "permissions": json.dumps(_stored_permissions(user), ensure_ascii=False),
                "user_id": user_id,
            },
        )
        result = _get_user(connection, user_id)
    return JSONResponse(result)


@router.put("/users/{user_id}/status")
async def update_user_status(request: Request) -> JSONResponse:
    """输入：路径用户 ID 与 ``enabled`` 布尔 JSON。

    输出：更新后的用户 JSON。
    功能：启用或禁用真实 Agent 身份，现有 RBAC 下一次请求立即生效。
    """

    user_id = request.path_params["user_id"]
    payload = await _request_payload(request)
    if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
        raise ApiError(400, "enabled 必须是布尔值")
    with runtime.engine.begin() as connection:
        result = connection.execute(
            sa.text("UPDATE user_identity SET is_active = :enabled WHERE hermes_user_id = :user_id"),
            {"enabled": payload["enabled"], "user_id": user_id},
        )
        if result.rowcount == 0:
            raise ApiError(404, "用户不存在")
        user = _get_user(connection, user_id)
    return JSONResponse(user)


@router.post("/users/{user_id}/bindings")
async def bind_user_platform(request: Request) -> JSONResponse:
    """输入：路径用户 ID、平台编码和平台用户 ID JSON。

    输出：更新后的用户 JSON。
    功能：写入真实 IM 身份映射，并阻止同一平台身份绑定给多个用户。
    """

    user_id = request.path_params["user_id"]
    payload = await _request_payload(request)
    platform = str(payload.get("platform") or "").strip().lower() if isinstance(payload, dict) else ""
    platform_user_id = str(payload.get("user_id") or "").strip() if isinstance(payload, dict) else ""
    if platform not in PLATFORMS:
        raise ApiError(400, "IM 平台无效")
    if len(platform_user_id) < 3 or len(platform_user_id) > 160:
        raise ApiError(400, "平台用户 ID 无效")
    with runtime.engine.begin() as connection:
        _get_user(connection, user_id)
        owner = connection.execute(
            sa.text(
                """
                SELECT hermes_user_id FROM user_platform_binding
                WHERE platform = :platform AND platform_user_id = :platform_user_id
                LIMIT 1
                """
            ),
            {"platform": platform, "platform_user_id": platform_user_id},
        ).scalar()
        if owner is not None and str(owner) != user_id:
            raise ApiError(409, "该平台用户 ID 已绑定其他用户")
        connection.execute(
            sa.text(
                "DELETE FROM user_platform_binding WHERE hermes_user_id = :user_id AND platform = :platform"
            ),
            {"user_id": user_id, "platform": platform},
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO user_platform_binding (
                    hermes_user_id, platform, platform_user_id, platform_token, bound_at
                ) VALUES (:user_id, :platform, :platform_user_id, :platform_token, :bound_at)
                """
            ),
            {
                "user_id": user_id,
                "platform": platform,
                "platform_user_id": platform_user_id,
                "platform_token": "",
                "bound_at": datetime.now(LOCAL_TIMEZONE).replace(tzinfo=None),
            },
        )
        user = _get_user(connection, user_id)
    return JSONResponse(user)


@router.delete("/users/{user_id}/bindings/{platform}")
def unbind_user_platform(request: Request) -> JSONResponse:
    """输入：路径用户 ID 与平台编码。

    输出：更新后的用户 JSON。
    功能：删除指定真实 IM 绑定，不影响该用户其他平台身份。
    """

    user_id = request.path_params["user_id"]
    platform = request.path_params["platform"].lower()
    if platform not in PLATFORMS:
        raise ApiError(400, "IM 平台无效")
    with runtime.engine.begin() as connection:
        _get_user(connection, user_id)
        connection.execute(
            sa.text(
                "DELETE FROM user_platform_binding WHERE hermes_user_id = :user_id AND platform = :platform"
            ),
            {"user_id": user_id, "platform": platform},
        )
        user = _get_user(connection, user_id)
    return JSONResponse(user)


