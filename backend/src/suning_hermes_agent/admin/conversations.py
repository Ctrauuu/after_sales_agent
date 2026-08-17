"""Hermes 会话审计、消息详情和 Trace 数据读取路由。"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from operator import itemgetter
from pathlib import Path
from typing import Any, Mapping

import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import runtime
from .common import ApiError, _json_value, _query_date, _query_int
from .runtime import LOCAL_TIMEZONE


router = APIRouter(prefix="/api/admin/logs")
_LOG_JSON_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[^\{]*(\{.*\})\s*$")
_trace_cache_key: tuple[str, int, int] | None = None
_trace_cache: list[dict[str, Any]] = []


def _state_database() -> Path:
    """输入：无；读取当前 ``HERMES_HOME``。

    输出：Hermes 统一状态数据库路径。
    功能：集中定位对话 Session 与消息的只读数据源。
    """

    return runtime.HERMES_HOME / "state.db"


def _open_state_database() -> sqlite3.Connection:
    """输入：无；读取 Hermes 状态数据库路径。

    输出：启用 Row 工厂的 SQLite 只读连接。
    功能：避免管理后台写入或锁住 Hermes 自有状态。
    """

    path = _state_database()
    if not path.is_file():
        raise ApiError(503, "Hermes 状态数据库不存在")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _trace_events() -> list[dict[str, Any]]:
    """输入：无；读取 Hermes Agent 结构化日志。

    输出：按时间升序去重的 ``suning_agent_trace`` 事件。
    功能：以文件 mtime 缓存解析结果，为日志耗时和仪表盘指标提供真实观测数据。
    """

    global _trace_cache_key, _trace_cache
    path = runtime.HERMES_HOME / "logs" / "agent.log"
    if not path.is_file():
        return []
    stat = path.stat()
    cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
    if cache_key == _trace_cache_key:
        return _trace_cache
    by_trace: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = _LOG_JSON_RE.match(line)
            if match is None or '"event": "suning_agent_trace"' not in line:
                continue
            try:
                event = json.loads(match.group(2))
            except json.JSONDecodeError:
                continue
            trace_id = str(event.get("trace_id") or "")
            if not trace_id:
                continue
            event["timestamp"] = match.group(1)
            by_trace[trace_id] = event
    _trace_cache = list(by_trace.values())
    _trace_cache.sort(key=itemgetter("timestamp"))
    _trace_cache_key = cache_key
    return _trace_cache


def _trace_for_session(session_id: str) -> dict[str, Any]:
    """输入：Hermes 原始 Session ID ``session_id``。

    输出：最新匹配 Trace 指标；没有观测记录时为空字典。
    功能：关联统一身份逻辑会话和 Hermes Session，补充耗时与 MCP 次数。
    """

    matched: dict[str, Any] = {}
    for event in _trace_events():
        if session_id in str(event.get("conversation_id") or ""):
            matched = event
    return matched


def _identity_directory() -> tuple[dict[tuple[str, str], dict[str, str]], list[dict[str, str]]]:
    """输入：无；读取现有用户和平台绑定表。

    输出：``(平台身份映射, 用户筛选选项)``。
    功能：把 Hermes 外部 Session 关联到 RBAC 内部用户和显示姓名。
    """

    bindings: dict[tuple[str, str], dict[str, str]] = {}
    users: dict[str, str] = {}
    with runtime.engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                """
                SELECT u.hermes_user_id, u.display_name, b.platform, b.platform_user_id
                FROM user_identity AS u
                LEFT JOIN user_platform_binding AS b ON b.hermes_user_id = u.hermes_user_id
                """
            )
        ).mappings()
        for row in rows:
            user_id = str(row["hermes_user_id"])
            name = str(row.get("display_name") or user_id)
            users[user_id] = name
            if row.get("platform") and row.get("platform_user_id"):
                bindings[(str(row["platform"]), str(row["platform_user_id"]))] = {"id": user_id, "name": name}
    return bindings, [{"id": user_id, "name": name} for user_id, name in sorted(users.items())]


def _origin(row: Mapping[str, Any]) -> dict[str, Any]:
    """输入：含 ``origin_json`` 的 Hermes Session 行 ``row``。

    输出：解析后的来源字典。
    功能：容错读取平台用户和显示名元数据。
    """

    return _json_value(row.get("origin_json"), {})


def _session_identity(
    row: Mapping[str, Any], bindings: Mapping[tuple[str, str], dict[str, str]]
) -> dict[str, str]:
    """输入：Hermes Session 行和平台绑定映射 ``bindings``。

    输出：内部用户 ID、显示名、平台和外部 ID。
    功能：优先使用真实 RBAC 绑定，缺失时安全回退 Session 来源名称。
    """

    origin = _origin(row)
    platform = str(row.get("source") or origin.get("platform") or "")
    external_id = str(origin.get("user_id") or row.get("user_id") or "")
    bound = bindings.get((platform, external_id), {})
    fallback_name = str(origin.get("user_name") or row.get("display_name") or external_id or "未知用户")
    return {
        "id": str(bound.get("id") or external_id),
        "name": str(bound.get("name") or fallback_name),
        "platform": platform,
        "external_id": external_id,
    }


def _topic(text: str) -> str:
    """输入：会话标题或首条提问 ``text``。

    输出：管理页面支持的话题分类。
    功能：用透明关键词规则对历史 Session 做最低成本审计归类。
    """

    normalized = text.lower()
    if any(term in normalized for term in ("保修", "政策", "三包", "法规")):
        return "售后政策"
    if any(term in normalized for term in ("订单", "链路", "物流", "退款到哪", "卡在哪")):
        return "订单追踪"
    if any(term in normalized for term in ("退单", "退货", "原因")):
        return "退单分析"
    if any(term in normalized for term in ("趋势", "经营", "区域", "同比", "环比")):
        return "经营分析"
    return "其他"


def _format_timestamp(value: Any) -> str:
    """输入：Unix 时间戳 ``value``。

    输出：Asia/Shanghai 的 ``YYYY-MM-DD HH:MM`` 文本。
    功能：统一 Hermes 对话与消息在管理后台的时间显示。
    """

    try:
        return datetime.fromtimestamp(float(value), LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def _session_summary(
    state: sqlite3.Connection,
    row: Mapping[str, Any],
    bindings: Mapping[tuple[str, str], dict[str, str]],
) -> dict[str, Any]:
    """输入：Hermes 状态连接、Session 行和身份映射。

    输出：对话审计列表的一条摘要。
    功能：读取首问、轮次、工具次数并关联真实 Trace 耗时。
    """

    session_id = str(row["id"])
    first = state.execute(
        "SELECT content FROM messages WHERE session_id = ? AND role = 'user' AND active = 1 ORDER BY id LIMIT 1",
        (session_id,),
    ).fetchone()
    first_question = str(first[0] if first else row.get("title") or "")
    turn_count = int(
        state.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND role = 'user' AND active = 1",
            (session_id,),
        ).fetchone()[0]
    )
    mcp_count = int(
        state.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND role = 'tool' AND active = 1",
            (session_id,),
        ).fetchone()[0]
    )
    identity = _session_identity(row, bindings)
    trace = _trace_for_session(session_id)
    return {
        "session_id": session_id,
        "user_id": identity["id"],
        "user_name": identity["name"],
        "platform": identity["platform"],
        "first_question": first_question,
        "topic": _topic(str(row.get("title") or first_question)),
        "turn_count": turn_count,
        "mcp_count": int(trace.get("total_mcp_calls", mcp_count)),
        "duration_ms": int(float(trace.get("duration_ms", 0))),
        "created_at": _format_timestamp(row.get("started_at")),
    }


@router.get("/conversations")
def list_conversations(request: Request) -> JSONResponse:
    """输入：日期、用户、平台、话题、关键词和分页查询请求。

    输出：真实 Hermes 会话摘要、总数和用户筛选项 JSON。
    功能：联合 ``state.db``、RBAC 绑定与 Trace 日志完成对话审计搜索。
    """

    params = request.query_params
    page = _query_int(request, "page", 1, 1, 100000)
    size = _query_int(request, "size", 10, 1, 10000)
    start_date = _query_date(request, "start_date")
    end_date = _query_date(request, "end_date")
    selected_platforms = {item for item in params.get("platform", "").split(",") if item}
    bindings, users = _identity_directory()
    with closing(_open_state_database()) as state:
        rows = state.execute(
            """
            SELECT id, source, user_id, display_name, origin_json, title, started_at
            FROM sessions
            WHERE source IN ('feishu', 'wecom', 'dingtalk') AND message_count > 0
            ORDER BY started_at DESC
            """
        ).fetchall()
        items = []
        for raw_row in rows:
            row = dict(raw_row)
            item = _session_summary(state, row, bindings)
            date = item["created_at"][:10]
            if start_date and date < start_date:
                continue
            if end_date and date > end_date:
                continue
            if params.get("user_id") and item["user_id"] != params["user_id"]:
                continue
            if selected_platforms and item["platform"] not in selected_platforms:
                continue
            if params.get("topic") and item["topic"] != params["topic"]:
                continue
            keyword = params.get("keyword", "").strip().lower()
            if keyword and keyword not in item["first_question"].lower():
                continue
            items.append(item)
    start = (page - 1) * size
    return JSONResponse({"items": items[start : start + size], "total": len(items), "users": users})


def _tool_call_names(value: Any) -> list[str]:
    """输入：Hermes ``tool_calls`` JSON 文本或结构 ``value``。

    输出：保持顺序且去重的工具名数组。
    功能：兼容 OpenAI function call 结构，为审计详情提取 MCP 调用链。
    """

    calls = _json_value(value, [])
    names: list[str] = []
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        name = function.get("name") if isinstance(function, dict) else call.get("name")
        if name and str(name) not in names:
            names.append(str(name))
    return names


@router.get("/conversations/{session_id}/detail")
def conversation_detail(request: Request) -> JSONResponse:
    """输入：路径 Hermes Session ID。

    输出：完整用户/Agent 消息时间线、工具调用与会话汇总 JSON。
    功能：只读加载真实 Transcript，跳过工具原始结果以避免泄露业务明细。
    """

    session_id = request.path_params["session_id"]
    bindings, _ = _identity_directory()
    with closing(_open_state_database()) as state:
        raw_session = state.execute(
            """
            SELECT id, source, user_id, display_name, origin_json, title, started_at
            FROM sessions WHERE id = ? AND source IN ('feishu', 'wecom', 'dingtalk') LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if raw_session is None:
            raise ApiError(404, "会话不存在")
        session = dict(raw_session)
        identity = _session_identity(session, bindings)
        rows = state.execute(
            """
            SELECT id, role, content, tool_calls, tool_name, timestamp
            FROM messages
            WHERE session_id = ? AND active = 1
              AND role IN ('user', 'assistant', 'tool')
            ORDER BY id
            """,
            (session_id,),
        ).fetchall()
        messages: list[dict[str, Any]] = []
        pending_tools: list[str] = []
        last_user_timestamp = 0.0
        mcp_count = 0
        for raw_message in rows:
            message = dict(raw_message)
            role = str(message["role"])
            timestamp = float(message.get("timestamp") or 0)
            if role == "tool":
                mcp_count += 1
                tool_name = str(message.get("tool_name") or "")
                if tool_name and tool_name not in pending_tools:
                    pending_tools.append(tool_name)
                continue
            for name in _tool_call_names(message.get("tool_calls")):
                if name not in pending_tools:
                    pending_tools.append(name)
            content = str(message.get("content") or "").strip()
            if role == "assistant" and not content:
                continue
            if role == "user":
                last_user_timestamp = timestamp
            duration_ms = int(max(0, timestamp - last_user_timestamp) * 1000) if role == "assistant" and last_user_timestamp else 0
            chart_match = re.search(r"!?\[[^\]]*\]\(([^)]+\.(?:png|jpg|jpeg|svg))\)", content, re.IGNORECASE)
            messages.append(
                {
                    "id": str(message["id"]),
                    "sender": "agent" if role == "assistant" else "user",
                    "content": content,
                    "created_at": _format_timestamp(timestamp),
                    "duration_ms": duration_ms,
                    "chart_url": chart_match.group(1) if chart_match else "",
                    "tool_calls": [{"name": name, "duration_ms": 0} for name in pending_tools] if role == "assistant" else [],
                }
            )
            if role == "assistant":
                pending_tools = []
        summary = _session_summary(state, session, bindings)
    assistant_durations = [message["duration_ms"] for message in messages if message["sender"] == "agent"]
    average = int(sum(assistant_durations) / len(assistant_durations)) if assistant_durations else 0
    return JSONResponse(
        {
            "session_id": session_id,
            "user_name": identity["name"],
            "platform": identity["platform"],
            "messages": messages,
            "summary": {
                "turn_count": summary["turn_count"],
                "mcp_count": mcp_count,
                "avg_duration_ms": average,
            },
        }
    )


