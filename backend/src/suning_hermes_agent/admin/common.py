"""管理 API 的认证、错误协议、参数校验与共享常量。"""

from __future__ import annotations

import hmac
import json
from datetime import datetime
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from mcp_suning.domain_registry import CATEGORY_ALIASES, REGION_ALIASES

from .runtime import LOCAL_TIMEZONE


PLATFORMS = {"feishu", "wecom", "dingtalk"}
CITY_CODES = {"NJ", "SH", "HZ", "SZ"}
DATA_SCOPE_LABELS = {
    "full": "完整明细",
    "anonymized": "脱敏明细",
    "aggregated": "仅汇总",
}
LABEL_DATA_SCOPES = {label: value for value, label in DATA_SCOPE_LABELS.items()}

class ApiError(Exception):
    """表示可安全返回给管理后台的 HTTP 业务错误。"""

    def __init__(self, status_code: int, message: str) -> None:
        """输入：HTTP 状态码 ``status_code`` 与可展示错误 ``message``。

        输出：初始化携带状态和消息的异常对象。
        功能：让路由校验与统一 JSON 异常响应保持解耦。
        """

        super().__init__(message)
        self.status_code = status_code
        self.message = message


class AdminAuthMiddleware(BaseHTTPMiddleware):
    """用共享 Bearer Token 保护全部管理接口，未配置时仅允许环回访问。"""

    def __init__(self, app: Any, token: str = "") -> None:
        """输入：下游 ASGI 应用 ``app`` 与可选管理令牌 ``token``。

        输出：初始化中间件实例。
        功能：保存令牌；空令牌仅用于本机反向代理联调。
        """

        super().__init__(app)
        self.token = token.strip()

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        """输入：管理请求 ``request`` 与下游调用器 ``call_next``。

        输出：授权后的下游响应，或 401 JSON 响应。
        功能：恒定时间比较 Bearer Token；无令牌配置时限制来源为环回地址。
        """

        if self.token:
            authorization = request.headers.get("authorization", "")
            supplied = authorization.removeprefix("Bearer ").strip()
            if not supplied or not hmac.compare_digest(supplied, self.token):
                return JSONResponse({"message": "管理接口认证失败"}, status_code=401)
        elif request.client is None or request.client.host not in {"127.0.0.1", "::1", "localhost"}:
            return JSONResponse({"message": "未配置管理令牌，仅允许本机访问"}, status_code=401)
        return await call_next(request)


async def api_error_handler(request: Request, error: ApiError) -> JSONResponse:
    """输入：触发异常的请求和 ``ApiError``。

    输出：包含稳定 ``message`` 字段的 JSON 错误响应。
    功能：把业务校验失败转换为前端统一错误协议。
    """

    del request
    return JSONResponse({"message": error.message}, status_code=error.status_code)


async def _request_payload(request: Request) -> Any:
    """输入：需要 JSON 请求体的管理请求 ``request``。

    输出：解析后的 JSON 值；格式损坏时抛出 400 ``ApiError``。
    功能：集中处理所有写接口的无效 JSON，避免返回内部 500 错误。
    """

    try:
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ApiError(400, "请求体不是有效 JSON") from exc


def _query_int(
    request: Request,
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """输入：请求、参数名、默认值及允许的整数范围。

    输出：限制在范围内的查询整数；格式或范围无效时抛出 400。
    功能：统一校验分页和数量参数，阻止异常值扩大查询成本。
    """

    raw = request.query_params.get(name)
    try:
        value = default if raw in {None, ""} else int(raw)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, f"{name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise ApiError(400, f"{name} 必须在 {minimum}-{maximum} 之间")
    return value


def _query_date(request: Request, name: str, default: str = "") -> str:
    """输入：请求、日期参数名 ``name`` 与可选默认日期 ``default``。

    输出：合法 ``YYYY-MM-DD`` 字符串或空字符串。
    功能：统一验证日志和仪表盘日期边界，避免无效日期触发 500。
    """

    value = request.query_params.get(name) or default
    if not value:
        return ""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ApiError(400, f"{name} 必须是 YYYY-MM-DD 日期") from exc
    return value


def _json_value(value: Any, default: Any) -> Any:
    """输入：数据库 JSON 值 ``value`` 与解析失败默认值 ``default``。

    输出：字典/数组原值、解析后的 JSON 或默认值。
    功能：兼容 MySQL JSON、SQLite 测试文本和空字段。
    """

    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return default



