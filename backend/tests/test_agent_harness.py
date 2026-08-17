"""验证多 Agent Harness 的生命周期、灰度与金丝雀行为。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from suning_hermes_agent.agent_harness import (
    AgentHarness,
    AgentInstance,
    AgentStatus,
)
import suning_hermes_agent.agent_harness as harness_module


class _FakeRedis:
    """记录 Harness 发往 Redis 的流量与热加载消息。"""

    def __init__(self) -> None:
        """输入：无。

        输出：初始化 Redis 调用记录。
        功能：提供不依赖 Redis 服务的最小同步客户端替身。
        """

        self.published: list[tuple[str, str]] = []
        self.values: dict[str, tuple[int, str]] = {}

    def publish(self, channel: str, message: str) -> None:
        """输入：Redis 频道 ``channel`` 与消息 ``message``。

        输出：无；保存发布记录。
        功能：模拟配置热加载的 Pub/Sub 通道。
        """

        self.published.append((channel, message))

    def setex(self, key: str, seconds: int, value: str) -> None:
        """输入：Redis 键 ``key``、TTL ``seconds`` 与 JSON 值 ``value``。

        输出：无；保存最新键值记录。
        功能：模拟 Gateway 读取的短期流量配置。
        """

        self.values[key] = (seconds, value)


class _FakeAlerter:
    """收集 Harness 告警文本的异步告警器替身。"""

    def __init__(self) -> None:
        """输入：无。

        输出：初始化空告警列表。
        功能：避免测试连接飞书，同时保留告警内容供断言。
        """

        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        """输入：待发送告警文本 ``message``。

        输出：无；记录告警文本。
        功能：模拟飞书告警器的异步发送接口。
        """

        self.messages.append(message)


class _FakeProcess:
    """记录 Harness 启动参数的子进程替身。"""

    def __init__(self, command: list[str], **kwargs: Any) -> None:
        """输入：子进程命令 ``command`` 和 Popen 参数 ``kwargs``。

        输出：保存命令及参数，进程初始视为存活。
        功能：避免测试启动不存在的业务 Agent 模块，同时模拟 ``Popen`` 必要接口。
        """

        self.command = command
        self.kwargs = kwargs

    def poll(self) -> None:
        """输入：无。

        输出：``None``，表示模拟进程仍在运行。
        功能：兼容 Harness 在健康检查前的早退判断。
        """

        return None


def _instance(
    version: str, port: int, weight: float = 1.0, status: AgentStatus = AgentStatus.HEALTHY
) -> AgentInstance:
    """输入：版本 ``version``、端口 ``port``、流量权重 ``weight`` 与状态 ``status``。

    输出：用于测试的退单分析 Agent 实例。
    功能：压缩重复的实例构造，使每个测试聚焦于 Harness 行为。
    """

    return AgentInstance(
        agent_id=f"return_analysis-{version}-{port}",
        agent_type="return_analysis",
        version=version,
        port=port,
        traffic_weight=weight,
        status=status,
    )


def test_route_request_is_stable_and_excludes_unhealthy_instances() -> None:
    """输入：无；构造两个可用版本和一个已切流版本。

    输出：无；路由不稳定或选择不可用实例时由 pytest 报告失败。
    功能：验证三平台传入的可信用户 ID 能稳定命中同一健康灰度实例。
    """

    harness = AgentHarness(_FakeRedis(), _FakeAlerter())
    stable = _instance("2.3", 8101, 0.9)
    canary = _instance("2.4", 8102, 0.1)
    removed = _instance("2.2", 8103, 1.0, AgentStatus.UNHEALTHY)
    for instance in (stable, canary, removed):
        harness._register(instance)

    users = [f"feishu:user-{index}" for index in range(100)]
    routes = [harness.route_request("return_analysis", user) for user in users]

    assert harness.route_request("return_analysis", users[0]) is routes[0]
    assert removed not in routes
    assert canary in routes
    assert sum(route is canary for route in routes) < 20


@pytest.mark.asyncio
async def test_start_agent_registers_only_after_health_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：pytest 补丁器 ``monkeypatch``、模拟 Popen 与健康检查结果。

    输出：无；启动命令、环境变量或健康后注册行为不正确时由 pytest 报告失败。
    功能：验证生命周期入口以运行解释器启动业务 Agent，且只在就绪后发布 Gateway 配置。
    """

    redis = _FakeRedis()
    harness = AgentHarness(redis, _FakeAlerter())
    processes: list[_FakeProcess] = []

    def launch(command: list[str], **kwargs: Any) -> _FakeProcess:
        """输入：Harness 提供的启动 ``command`` 及 Popen 参数 ``kwargs``。

        输出：记录命令的存活模拟进程。
        功能：替代真实 ``subprocess.Popen``，使测试可以检查命令和环境而不创建进程。
        """

        process = _FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    async def healthy(_instance: AgentInstance, timeout: int) -> bool:
        """输入：待检查实例 ``_instance`` 与健康超时 ``timeout``。

        输出：``True``。
        功能：模拟 Agent 已立即监听端口，并断言生产健康检查保留 30 秒上限。
        """

        assert timeout == 30
        return True

    monkeypatch.setattr(harness_module.subprocess, "Popen", launch)
    monkeypatch.setattr(harness, "_wait_healthy", healthy)
    instance = await harness.start_agent("return_analysis", "2.4", 8102, FEATURE="canary")

    assert instance.status is AgentStatus.HEALTHY
    assert harness.route_request("return_analysis", "wecom:zhangsan") is instance
    assert processes[0].command[1:] == [
        "-m",
        "agents.return_analysis",
        "--port",
        "8102",
        "--version",
        "2.4",
    ]
    assert processes[0].kwargs["env"]["FEATURE"] == "canary"
    assert "traffic:return_analysis" in redis.values


@pytest.mark.asyncio
async def test_canary_three_failures_switches_traffic_and_alerts_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：pytest 补丁器 ``monkeypatch`` 与固定失败的业务查询替身。

    输出：无；未在第三次失败后切流或重复告警时由 pytest 报告失败。
    功能：锁定金丝雀探测的 DEGRADED、UNHEALTHY 和自动切流阈值。
    """

    redis = _FakeRedis()
    alerter = _FakeAlerter()
    harness = AgentHarness(redis, alerter)
    instance = _instance("2.4", 8102)
    harness._register(instance)

    async def failed_query(*_args: Any) -> dict[str, Any]:
        """输入：Harness 传入的实例和查询参数 ``_args``。

        输出：缺少期望工具的响应对象。
        功能：模拟 TCP 存活但业务工具未实际执行的金丝雀失败。
        """

        return {"tools_called": [], "data": []}

    monkeypatch.setattr(harness, "_send_query", failed_query)
    probe = harness.CANARY_PROBES["return_analysis"]
    for _ in range(4):
        await harness._run_canary(instance, probe)

    assert instance.status is AgentStatus.UNHEALTHY
    assert instance.traffic_weight == 0
    assert instance.canary_failures == 4
    assert len(alerter.messages) == 1
    assert "已自动切流" in alerter.messages[0]
    assert "traffic:return_analysis" in redis.values


@pytest.mark.asyncio
async def test_degraded_canary_recovers_and_config_reload_skips_cold_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：pytest 补丁器 ``monkeypatch``、一个热加载和一个冷重启实例。

    输出：无；恢复状态或 Redis 热加载目标不正确时由 pytest 报告失败。
    功能：验证单次探测成功能恢复 DEGRADED 实例，且只通知允许内存重载的版本。
    """

    redis = _FakeRedis()
    alerter = _FakeAlerter()
    harness = AgentHarness(redis, alerter)
    warm = _instance("2.3", 8101, status=AgentStatus.DEGRADED)
    cold = _instance("2.4", 8102)
    cold.hot_reload_enabled = False
    harness._register(warm)
    harness._register(cold)

    async def successful_query(*_args: Any) -> dict[str, Any]:
        """输入：Harness 传入的实例和查询参数 ``_args``。

        输出：符合退单金丝雀约束的工具和空数据结果。
        功能：模拟业务查询正常完成，以验证降级实例恢复。
        """

        return {"tools_called": ["query_return_stats_nl2sql"], "data": []}

    monkeypatch.setattr(harness, "_send_query", successful_query)
    await harness._run_canary(warm, harness.CANARY_PROBES["return_analysis"])
    harness._reload_config("return_analysis", "backend/nl2sql/prompts/nl2sql_v1.1.yaml")

    assert warm.status is AgentStatus.HEALTHY
    assert warm.canary_failures == 0
    assert len(alerter.messages) == 1
    assert redis.published[0][0] == f"agent:{warm.agent_id}:config_reload"
    assert len(redis.published) == 1


@pytest.mark.asyncio
async def test_rolling_restart_starts_before_stopping_old_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：pytest 补丁器 ``monkeypatch``、已注册旧版本及模拟健康新版本。

    输出：无；新版本启动、分阶段切流或旧版本停止顺序不正确时由 pytest 报告失败。
    功能：验证滚动升级遵守先健康新实例、再灰度、最后下线旧实例的安全顺序。
    """

    harness = AgentHarness(_FakeRedis(), _FakeAlerter())
    old = _instance("2.3", 8101)
    harness._register(old)
    events: list[str] = []

    async def start(agent_type: str, version: str, port: int) -> AgentInstance:
        """输入：升级请求的类型 ``agent_type``、版本 ``version`` 与端口 ``port``。

        输出：已注册的健康新实例。
        功能：模拟进程健康检查成功，并记录新版本启动发生在旧版停止之前。
        """

        events.append(f"start:{version}:{port}")
        new = _instance(version, port)
        harness._register(new)
        return new

    async def stop(instance: AgentInstance) -> None:
        """输入：待停止的旧 ``instance``。

        输出：无；记录停止事件并从注册表移除实例。
        功能：模拟滚动升级末尾的旧版本下线，不启动真实子进程。
        """

        events.append(f"stop:{instance.version}")
        instance.status = AgentStatus.STOPPED
        harness._unregister(instance)

    async def no_wait(_seconds: float) -> None:
        """输入：原本的观察秒数 ``_seconds``。

        输出：无。
        功能：替代 30 秒灰度观察等待，使单元测试立即执行完整流量阶段。
        """

    monkeypatch.setattr(harness, "start_agent", start)
    monkeypatch.setattr(harness, "stop_agent", stop)
    monkeypatch.setattr(asyncio, "sleep", no_wait)

    assert await harness.rolling_restart("return_analysis", "2.4") is True
    assert events == ["start:2.4:8102", "stop:2.3"]
    assert list(harness._instances["return_analysis"])[0].version == "2.4"
