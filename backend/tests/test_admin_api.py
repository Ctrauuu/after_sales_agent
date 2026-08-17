"""验证管理后台 REST API 直接操作现有 Agent 数据源。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from fastapi.testclient import TestClient

from suning_hermes_agent import admin_api
from suning_hermes_agent.admin import conversations, mcp, runtime


def _management_engine(path: Path) -> sa.Engine:
    """输入：临时 SQLite 文件路径 ``path``。

    输出：已建用户、角色、平台绑定和 MCP 注册表的 SQLAlchemy Engine。
    功能：用与生产查询兼容的最小表结构验证管理写操作，不依赖真实 MySQL。
    """

    test_engine = sa.create_engine(f"sqlite:///{path}")
    with test_engine.begin() as connection:
        for statement in (
            """CREATE TABLE user_role_permission (
                role_name TEXT PRIMARY KEY, description TEXT, region_filter TEXT,
                category_filter TEXT, max_date_range_days INTEGER, data_scope TEXT
            )""",
            """CREATE TABLE user_identity (
                hermes_user_id TEXT PRIMARY KEY, employee_id TEXT UNIQUE,
                display_name TEXT, role TEXT, permissions TEXT,
                is_active INTEGER, created_at TIMESTAMP
            )""",
            """CREATE TABLE user_platform_binding (
                id INTEGER PRIMARY KEY AUTOINCREMENT, hermes_user_id TEXT,
                platform TEXT, platform_user_id TEXT, platform_token TEXT,
                bound_at TIMESTAMP
            )""",
            """CREATE TABLE mcp_server_registry (
                server_id TEXT PRIMARY KEY, server_name TEXT, host TEXT,
                port INTEGER, status TEXT, last_heartbeat TIMESTAMP
            )""",
            """CREATE TABLE mcp_tool_registry (
                tool_id TEXT PRIMARY KEY, server_id TEXT, tool_name TEXT,
                description TEXT, param_schema TEXT, is_enabled INTEGER
            )""",
        ):
            connection.exec_driver_sql(statement)
        connection.exec_driver_sql(
            "INSERT INTO user_role_permission VALUES (?, ?, ?, ?, ?, ?)",
            ("quality_engineer", "品控工程师", None, '["C1-AC"]', 90, "full"),
        )
        connection.exec_driver_sql(
            "INSERT INTO user_identity VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "U-H001",
                "E10001",
                "王小明",
                "quality_engineer",
                '{"region":["HD"],"categories":["C1-AC"],"data_scope":"full"}',
                1,
                datetime.now(),
            ),
        )
        connection.exec_driver_sql(
            "INSERT INTO user_platform_binding (hermes_user_id, platform, platform_user_id, platform_token, bound_at) VALUES (?, ?, ?, ?, ?)",
            ("U-H001", "feishu", "ou_test", "", datetime.now()),
        )
        connection.exec_driver_sql(
            "INSERT INTO mcp_server_registry VALUES (?, ?, ?, ?, ?, ?)",
            ("mcp-aftersale", "售后工单服务", "127.0.0.1", 8102, "degraded", None),
        )
        connection.exec_driver_sql(
            "INSERT INTO mcp_tool_registry VALUES (?, ?, ?, ?, ?, ?)",
            (
                "tool-001",
                "mcp-aftersale",
                "query_return_stats_nl2sql",
                "退单数据聚合统计",
                '{"type":"object"}',
                1,
            ),
        )
    return test_engine


def _hermes_home(path: Path) -> tuple[Path, str]:
    """输入：临时目录 ``path``。

    输出：含真实形态 ``state.db``、Agent Trace 日志的 Hermes Home 与 Session ID。
    功能：构造可被对话审计、趋势、MCP 分布和告警接口联合读取的数据。
    """

    home = path / "hermes"
    (home / "logs").mkdir(parents=True)
    session_id = "session-admin-test"
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    with sqlite3.connect(home / "state.db") as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, source TEXT, user_id TEXT, display_name TEXT,
                origin_json TEXT, title TEXT, started_at REAL,
                message_count INTEGER, tool_call_count INTEGER
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                tool_calls TEXT, tool_name TEXT, timestamp REAL, active INTEGER
            );
            """
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                "feishu",
                "ou_test",
                "测试群",
                json.dumps({"platform": "feishu", "user_id": "ou_test", "user_name": "王小明"}),
                "空调退单原因",
                now.timestamp(),
                4,
                1,
            ),
        )
        connection.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, session_id, "user", "分析近7天空调退单原因", None, None, now.timestamp(), 1),
                (
                    2,
                    session_id,
                    "assistant",
                    "",
                    json.dumps([{"function": {"name": "query_return_stats_nl2sql", "arguments": {}}}]),
                    None,
                    now.timestamp() + 0.5,
                    1,
                ),
                (3, session_id, "tool", "{}", None, "query_return_stats_nl2sql", now.timestamp() + 1.0, 1),
                (4, session_id, "assistant", "空调退单主要原因为安装问题。", None, None, now.timestamp() + 2.0, 1),
            ],
        )
    trace = {
        "event": "suning_agent_trace",
        "trace_id": "trace-admin-test",
        "conversation_id": f"im:group:feishu:{session_id}:U-H001",
        "user_id": "U-H001",
        "platform": "feishu",
        "duration_ms": 2000,
        "total_mcp_calls": 1,
        "total_mcp_failures": 0,
    }
    (home / "logs" / "agent.log").write_text(
        f"{now.strftime('%Y-%m-%d %H:%M:%S')},000 INFO test: {json.dumps(trace)}\n",
        encoding="utf-8",
    )
    return home, session_id


def _skills_dir(path: Path) -> Path:
    """输入：临时目录 ``path``。

    输出：含一个 Agent 原生 ``SKILL.md`` 的 Skills Hub 路径。
    功能：为 Skill 列表、详情、触发模式和状态持久化提供真实文件格式。
    """

    skills_dir = path / "skills"
    skill_dir = skills_dir / "skill-admin"
    skill_dir.mkdir(parents=True)
    now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    payload = {
        "skill_id": "skill-admin",
        "name": "退单原因分析",
        "description": "按品类分析退单原因",
        "version": 2,
        "trigger_patterns": ["分析退单原因"],
        "workflow": [
            {
                "step": 1,
                "tool": "query_return_stats_nl2sql",
                "params": {"group_by": "reason"},
                "output_key": "reasons",
            }
        ],
        "required_mcp_tools": ["query_return_stats_nl2sql"],
        "output_template": "{{reasons}}",
        "created_at": now,
        "updated_at": now,
        "usage_count": 8,
        "avg_quality_score": 0.94,
    }
    content = (
        "---\nname: 退单原因分析\ndescription: 按品类分析退单原因\n---\n\n"
        "# 退单原因分析\n\n```json\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n```\n"
    )
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skills_dir


async def _successful_probe(address: str) -> bool:
    """输入：待探测 MCP 地址 ``address``。

    输出：恒为 ``True``。
    功能：隔离管理 API 测试与真实 MCP 端口，同时保留心跳状态写入路径。
    """

    assert address.endswith(":8102/mcp")
    return True


def _ignore_tool_status(tool_name: str, enabled: bool) -> None:
    """输入：测试切换的工具名 ``tool_name`` 和状态 ``enabled``。

    输出：无。
    功能：隔离管理接口持久化测试与开发机真实 Redis，同时保留发布函数的调用边界。
    """

    assert tool_name == "query_return_stats_nl2sql"
    assert enabled is False


def test_admin_console_acceptance_uses_real_agent_stores(tmp_path: Path, monkeypatch: object) -> None:
    """输入：临时 MySQL 等价库、Hermes 状态、Skill Hub 与认证令牌。

    输出：无；任一管理后台验收接口或持久化副作用不正确时由断言报告失败。
    功能：覆盖用户增改禁用/绑定、MCP 心跳/工具开关、日志详情、Skill 启停和仪表盘。
    """

    test_engine = _management_engine(tmp_path / "management.db")
    home, session_id = _hermes_home(tmp_path)
    skills_dir = _skills_dir(tmp_path)
    monkeypatch.setattr(runtime, "engine", test_engine)
    monkeypatch.setattr(runtime, "HERMES_HOME", home)
    monkeypatch.setattr(runtime, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(mcp, "probe_mcp_server", _successful_probe)
    monkeypatch.setattr(mcp, "_publish_tool_status", _ignore_tool_status)
    monkeypatch.setattr(conversations, "_trace_cache_key", None)
    app = admin_api.create_app(admin_token="test-token")
    headers = {"Authorization": "Bearer test-token"}

    with TestClient(app) as client:
        assert client.get("/api/admin/users", headers=headers).json()["total"] == 1
        created = client.post(
            "/api/admin/users",
            headers=headers,
            json={
                "employee_id": "E10002",
                "name": "李美玲",
                "role": "quality_engineer",
                "regions": ["华东"],
                "categories": ["空调"],
                "granularity": "完整明细",
            },
        )
        assert created.status_code == 201
        user_id = created.json()["id"]
        edited = client.put(
            f"/api/admin/users/{user_id}",
            headers=headers,
            json={
                "employee_id": "E10002",
                "name": "李美玲-编辑",
                "role": "quality_engineer",
                "regions": ["华东"],
                "categories": ["空调"],
                "granularity": "完整明细",
            },
        )
        assert edited.json()["name"] == "李美玲-编辑"
        assert client.put(
            f"/api/admin/users/{user_id}/status", headers=headers, json={"enabled": False}
        ).json()["enabled"] is False
        assert client.post(
            f"/api/admin/users/{user_id}/bindings",
            headers=headers,
            json={"platform": "wecom", "user_id": "limeiling"},
        ).json()["bindings"][0]["platform"] == "wecom"

        servers = client.get("/api/admin/mcp/servers", headers=headers).json()["items"]
        assert servers[0]["status"] == "degraded"
        heartbeat = client.post(
            "/api/admin/mcp/servers/mcp-aftersale/heartbeat", headers=headers
        )
        assert heartbeat.json()["status"] == "online"
        assert client.put(
            "/api/admin/mcp/tools/tool-001/toggle",
            headers=headers,
            json={"enabled": False},
        ).json()["enabled"] is False

        logs = client.get(
            "/api/admin/logs/conversations?keyword=空调&page=1&size=10", headers=headers
        ).json()
        assert logs["total"] == 1
        detail = client.get(
            f"/api/admin/logs/conversations/{session_id}/detail", headers=headers
        ).json()
        assert detail["summary"]["mcp_count"] == 1
        assert detail["messages"][-1]["tool_calls"][0]["name"] == "query_return_stats_nl2sql"

        skills = client.get("/api/admin/skills", headers=headers).json()
        assert skills["items"][0]["quality_score"] == 94
        assert client.put(
            "/api/admin/skills/skill-admin/status",
            headers=headers,
            json={"enabled": False},
        ).json()["enabled"] is False
        assert client.get(
            "/api/admin/skills?status=disabled", headers=headers
        ).json()["total"] == 1

        overview = client.get("/api/admin/dashboard/overview", headers=headers).json()
        assert overview["conversations"]["value"] == 1
        assert client.get(
            "/api/admin/dashboard/trend", headers=headers
        ).json()["items"]
        assert client.get(
            "/api/admin/dashboard/mcp-distribution", headers=headers
        ).json()["items"][0]["value"] == 1
        assert client.get("/api/admin/dashboard/alerts", headers=headers).status_code == 200
        assert client.get("/api/admin/users").status_code == 401

    test_engine.dispose()
