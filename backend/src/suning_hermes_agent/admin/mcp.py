"""MCP 服务注册、工具开关和真实心跳管理路由。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

import httpx
import redis
import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mcp_suning.config import settings

from . import runtime
from .common import ApiError, _json_value, _request_payload
from .runtime import LOCAL_TIMEZONE


router = APIRouter(prefix="/api/admin/mcp")


def _mcp_address(host: str, port: int) -> str:
    """输入：MCP 主机 ``host`` 与端口 ``port``。

    输出：标准 HTTP MCP Endpoint。
    功能：兼容数据库中带或不带协议的主机字段。
    """

    normalized = host.rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        normalized = f"http://{normalized}"
    return f"{normalized}:{port}/mcp"


def _heartbeat_text(value: Any) -> str:
    """输入：数据库最后心跳时间 ``value``。

    输出：刚刚、秒前、分钟前或格式化日期。
    功能：把真实时间转换为 MCP 服务卡片需要的相对文案。
    """

    if value is None:
        return "尚未检测"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if getattr(value, "tzinfo", None) is not None:
        value = value.astimezone(LOCAL_TIMEZONE).replace(tzinfo=None)
    seconds = max(0, int((datetime.now(LOCAL_TIMEZONE).replace(tzinfo=None) - value).total_seconds()))
    if seconds < 10:
        return "刚刚"
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    return value.strftime("%m-%d %H:%M")


def _serialize_server(row: Mapping[str, Any]) -> dict[str, Any]:
    """输入：MCP 服务注册表行 ``row``。

    输出：服务卡片字段字典。
    功能：补充标准地址、工具数和相对心跳时间。
    """

    host = str(row["host"])
    port = int(row["port"])
    return {
        "id": str(row["server_id"]),
        "name": str(row["server_name"]),
        "host": host,
        "port": port,
        "address": _mcp_address(host, port),
        "status": str(row.get("status") or "offline"),
        "tool_count": int(row.get("tool_count") or 0),
        "last_heartbeat": _heartbeat_text(row.get("last_heartbeat")),
    }


def _get_server(connection: sa.Connection, server_id: str) -> dict[str, Any]:
    """输入：数据库连接与 MCP 服务 ID ``server_id``。

    输出：服务卡片字典；不存在时抛出 404。
    功能：复用带工具数量的服务查询。
    """

    row = connection.execute(
        sa.text(
            """
            SELECT s.server_id, s.server_name, s.host, s.port, s.status,
                   s.last_heartbeat, COUNT(t.tool_id) AS tool_count
            FROM mcp_server_registry AS s
            LEFT JOIN mcp_tool_registry AS t ON t.server_id = s.server_id
            WHERE s.server_id = :server_id
            GROUP BY s.server_id, s.server_name, s.host, s.port, s.status, s.last_heartbeat
            """
        ),
        {"server_id": server_id},
    ).mappings().first()
    if row is None:
        raise ApiError(404, "MCP 服务不存在")
    return _serialize_server(row)


@router.get("/servers")
def list_mcp_servers(request: Request) -> JSONResponse:
    """输入：MCP 服务列表请求 ``request``。

    输出：全部真实注册服务及心跳状态 JSON。
    功能：读取 ``mcp_server_registry`` 和工具数量。
    """

    del request
    with runtime.engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                """
                SELECT s.server_id, s.server_name, s.host, s.port, s.status,
                       s.last_heartbeat, COUNT(t.tool_id) AS tool_count
                FROM mcp_server_registry AS s
                LEFT JOIN mcp_tool_registry AS t ON t.server_id = s.server_id
                GROUP BY s.server_id, s.server_name, s.host, s.port, s.status, s.last_heartbeat
                ORDER BY s.port, s.server_id
                """
            )
        ).mappings()
        items = [_serialize_server(row) for row in rows]
    return JSONResponse({"items": items})


@router.get("/servers/{server_id}/tools")
def list_mcp_tools(request: Request) -> JSONResponse:
    """输入：路径 MCP 服务 ID。

    输出：该服务注册工具数组 JSON。
    功能：读取真实工具描述、参数 Schema 与启用状态。
    """

    server_id = request.path_params["server_id"]
    with runtime.engine.connect() as connection:
        _get_server(connection, server_id)
        rows = connection.execute(
            sa.text(
                """
                SELECT tool_id, tool_name, description, param_schema, is_enabled
                FROM mcp_tool_registry
                WHERE server_id = :server_id
                ORDER BY tool_name
                """
            ),
            {"server_id": server_id},
        ).mappings()
        items = [
            {
                "id": str(row["tool_id"]),
                "name": str(row["tool_name"]),
                "description": str(row.get("description") or ""),
                "schema": _json_value(row.get("param_schema"), {}),
                "enabled": bool(row.get("is_enabled")),
            }
            for row in rows
        ]
    return JSONResponse({"items": items})


def _validated_server_payload(payload: Any) -> dict[str, Any]:
    """输入：MCP 服务新增/编辑 JSON ``payload``。

    输出：规范化名称、主机和端口字典。
    功能：在网络探测和数据库写入前阻断无效地址。
    """

    if not isinstance(payload, dict):
        raise ApiError(400, "请求体必须是 JSON 对象")
    name = str(payload.get("name") or "").strip()
    host = str(payload.get("host") or "").strip().rstrip("/")
    try:
        port = int(payload.get("port"))
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "端口必须是整数") from exc
    if not name or len(name) > 100:
        raise ApiError(400, "服务名称不能为空且不能超过 100 字")
    if not re.fullmatch(r"(?:https?://)?(?:localhost|[A-Za-z0-9][A-Za-z0-9.-]*|(?:\d{1,3}\.){3}\d{1,3})", host):
        raise ApiError(400, "主机地址格式无效")
    if not 1 <= port <= 65535:
        raise ApiError(400, "端口必须在 1-65535 之间")
    return {"name": name, "host": host, "port": port}


@router.post("/servers")
async def create_mcp_server(request: Request) -> JSONResponse:
    """输入：MCP 服务名称、主机和端口 JSON。

    输出：新注册服务 JSON。
    功能：写入现有服务注册表，初始为待检测的降级状态。
    """

    server = _validated_server_payload(await _request_payload(request))
    server_id = f"mcp-{uuid.uuid4().hex[:10]}"
    with runtime.engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO mcp_server_registry (
                    server_id, server_name, host, port, status, last_heartbeat
                ) VALUES (:server_id, :name, :host, :port, :status, :last_heartbeat)
                """
            ),
            {"server_id": server_id, **server, "status": "degraded", "last_heartbeat": None},
        )
        result = _get_server(connection, server_id)
    return JSONResponse(result, status_code=201)


@router.put("/servers/{server_id}")
async def update_mcp_server(request: Request) -> JSONResponse:
    """输入：路径服务 ID 与名称、主机、端口 JSON。

    输出：更新后的服务 JSON。
    功能：修改现有 MCP 注册信息并保留工具与心跳状态。
    """

    server_id = request.path_params["server_id"]
    server = _validated_server_payload(await _request_payload(request))
    with runtime.engine.begin() as connection:
        result = connection.execute(
            sa.text(
                """
                UPDATE mcp_server_registry
                SET server_name = :name, host = :host, port = :port
                WHERE server_id = :server_id
                """
            ),
            {"server_id": server_id, **server},
        )
        if result.rowcount == 0:
            raise ApiError(404, "MCP 服务不存在")
        updated = _get_server(connection, server_id)
    return JSONResponse(updated)


@router.put("/tools/{tool_id}/toggle")
async def toggle_mcp_tool(request: Request) -> JSONResponse:
    """输入：路径工具 ID 与 ``enabled`` 布尔 JSON。

    输出：更新后的工具 ID 与状态 JSON。
    功能：切换真实工具注册表开关，作为 Agent 工具可用性的管理事实源。
    """

    tool_id = request.path_params["tool_id"]
    payload = await _request_payload(request)
    if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
        raise ApiError(400, "enabled 必须是布尔值")
    with runtime.engine.begin() as connection:
        tool_name = connection.execute(
            sa.text("SELECT tool_name FROM mcp_tool_registry WHERE tool_id = :tool_id LIMIT 1"),
            {"tool_id": tool_id},
        ).scalar()
        if tool_name is None:
            raise ApiError(404, "MCP 工具不存在")
        result = connection.execute(
            sa.text("UPDATE mcp_tool_registry SET is_enabled = :enabled WHERE tool_id = :tool_id"),
            {"enabled": payload["enabled"], "tool_id": tool_id},
        )
        if result.rowcount == 0:
            raise ApiError(404, "MCP 工具不存在")
    _publish_tool_status(str(tool_name), payload["enabled"])
    return JSONResponse({"id": tool_id, "enabled": payload["enabled"]})


def _publish_tool_status(tool_name: str, enabled: bool) -> None:
    """输入：MCP 工具名 ``tool_name`` 与目标状态 ``enabled``。

    输出：无；Redis 可用时写入持久状态键，失败时保留数据库结果。
    功能：让独立运行的 Hermes Agent 在下一次真实调用前即时看到管理开关。
    """

    if not settings.redis_url:
        return
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        client.set(f"mcp:tool:enabled:{tool_name}", "1" if enabled else "0")
    except redis.RedisError:
        return
    finally:
        client.close()


async def probe_mcp_server(address: str) -> bool:
    """输入：完整 MCP HTTP 地址 ``address``。

    输出：服务可建立 HTTP 连接且未返回 5xx 时为 ``True``。
    功能：用 30 秒上限执行真实心跳；MCP 协议级 4xx 仍证明进程在线。
    """

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(address, headers={"accept": "application/json, text/event-stream"})
    return response.status_code < 500


@router.post("/servers/{server_id}/heartbeat")
async def heartbeat_mcp_server(request: Request) -> JSONResponse:
    """输入：路径 MCP 服务 ID。

    输出：检测后的服务 JSON。
    功能：真实探测服务地址，成功写在线和心跳时间，失败写降级并保留最后成功时间。
    """

    server_id = request.path_params["server_id"]
    with runtime.engine.connect() as connection:
        server = _get_server(connection, server_id)
    try:
        online = await probe_mcp_server(server["address"])
    except (httpx.HTTPError, OSError):
        online = False
    with runtime.engine.begin() as connection:
        if online:
            connection.execute(
                sa.text(
                    "UPDATE mcp_server_registry SET status = :status, last_heartbeat = :heartbeat WHERE server_id = :server_id"
                ),
                {
                    "status": "online",
                    "heartbeat": datetime.now(LOCAL_TIMEZONE).replace(tzinfo=None),
                    "server_id": server_id,
                },
            )
        else:
            connection.execute(
                sa.text("UPDATE mcp_server_registry SET status = :status WHERE server_id = :server_id"),
                {"status": "degraded", "server_id": server_id},
            )
        updated = _get_server(connection, server_id)
    if not online:
        raise ApiError(504, "心跳超时或服务不可用")
    return JSONResponse(updated)


