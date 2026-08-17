"""多 Agent 进程的生命周期、灰度路由与金丝雀健康管理。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class AgentStatus(str, Enum):
    """一个 Agent 实例可被 Gateway 使用的运行状态。"""

    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    STOPPED = "stopped"


@dataclass
class AgentInstance:
    """已启动或待启动的单个业务 Agent 实例。"""

    agent_id: str
    agent_type: str
    version: str
    port: int
    process: Any | None = None
    status: AgentStatus = AgentStatus.STARTING
    canary_failures: int = 0
    canary_max_failures: int = 3
    traffic_weight: float = 1.0
    hot_reload_enabled: bool = True
    config_files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CanaryProbe:
    """验证 Agent 真实业务能力的固定查询。"""

    probe_id: str
    query: str
    expected_tools: list[str]
    min_result_rows: int = 0
    timeout: int = 15


class AgentHarness:
    """统一管理业务 Agent 的进程、配置、灰度流量和金丝雀检查。"""

    CANARY_PROBES = {
        "return_analysis": CanaryProbe(
            probe_id="canary-return-001",
            query="近7天退单量",
            expected_tools=["query_return_stats_nl2sql"],
            min_result_rows=0,
            timeout=10,
        ),
        "order_trace": CanaryProbe(
            probe_id="canary-order-001",
            query="查订单202406010001的售后链路",
            expected_tools=["get_order_detail", "get_aftersale_workflow"],
            min_result_rows=1,
            timeout=15,
        ),
    }
    ROLLING_TRAFFIC_WEIGHTS = (0.1, 0.3, 0.6, 1.0)
    ROLLOUT_OBSERVATION_SECONDS = 30
    CONFIG_SUFFIXES = frozenset({".yaml", ".yml", ".prompt"})

    def __init__(self, redis_client: Any, feishu_alerter: Any) -> None:
        """输入：同步 Redis 客户端 ``redis_client`` 与异步告警器 ``feishu_alerter``。

        输出：初始化空实例注册表。
        功能：保存 Harness 对 Gateway 流量配置和运维告警的两个外部边界。
        """

        self.redis = redis_client
        self.alerter = feishu_alerter
        self._instances: dict[str, list[AgentInstance]] = {}

    async def start_agent(
        self, agent_type: str, version: str, port: int, **env_vars: str
    ) -> AgentInstance:
        """输入：Agent 类型 ``agent_type``、版本 ``version``、端口 ``port`` 及附加环境变量。

        输出：已健康注册或标为 ``UNHEALTHY`` 的实例。
        功能：启动子 Agent 进程，等待 TCP 健康检查通过后才把它加入可路由注册表。
        """

        instance = AgentInstance(
            agent_id=f"{agent_type}-{version}-{port}",
            agent_type=agent_type,
            version=version,
            port=port,
        )
        environment = os.environ.copy()
        environment.update(env_vars)
        instance.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                f"agents.{agent_type}",
                "--port",
                str(port),
                "--version",
                version,
            ],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if await self._wait_healthy(instance, timeout=30):
            instance.status = AgentStatus.HEALTHY
            self._register(instance)
            self._publish_traffic_config(agent_type)
        else:
            instance.status = AgentStatus.UNHEALTHY
            await self._terminate_process(instance)
        return instance

    async def rolling_restart(self, agent_type: str, new_version: str) -> bool:
        """输入：待升级的 Agent 类型 ``agent_type`` 与目标版本 ``new_version``。

        输出：新版本健康并完成切流时返回 ``True``，启动失败时返回 ``False``。
        功能：先启动健康新实例，再按 10%、30%、60%、100% 灰度并最终停止旧实例。
        """

        old_instances = list(self._instances.get(agent_type, []))
        new_instance = await self.start_agent(
            agent_type, new_version, self._next_port(agent_type)
        )
        if new_instance.status is not AgentStatus.HEALTHY:
            await self.alerter.send(f"新版本 {agent_type} {new_version} 启动失败")
            return False
        for weight in self.ROLLING_TRAFFIC_WEIGHTS:
            new_instance.traffic_weight = weight
            for old_instance in old_instances:
                old_instance.traffic_weight = (1.0 - weight) / len(old_instances)
            self._publish_traffic_config(agent_type)
            await asyncio.sleep(self.ROLLOUT_OBSERVATION_SECONDS)
        for old_instance in old_instances:
            await self.stop_agent(old_instance)
        return True

    async def stop_agent(self, instance: AgentInstance) -> None:
        """输入：已注册或启动中的 ``instance``。

        输出：无；进程被终止、实例标为 ``STOPPED`` 并从注册表移除。
        功能：优雅发送 SIGTERM，超时后强杀，避免旧版本在切流完成后继续接收请求。
        """

        await self._terminate_process(instance)
        instance.status = AgentStatus.STOPPED
        self._unregister(instance)
        self._publish_traffic_config(instance.agent_type)

    async def watch_config(
        self, agent_type: str, config_path: Path, interval: float = 1.0
    ) -> None:
        """输入：Agent 类型 ``agent_type``、待观察文件 ``config_path`` 与轮询间隔 ``interval``。

        输出：持续运行直到任务被取消；配置变更时向可热加载实例发布 Redis 指令。
        功能：以标准库文件时间戳监视 YAML 或 Prompt 配置，避免为单一监视功能新增运行时依赖。
        """

        if config_path.suffix not in self.CONFIG_SUFFIXES:
            raise ValueError("仅支持 YAML 或 Prompt 配置文件")
        previous_mtime = self._modified_at(config_path)
        while True:
            await asyncio.sleep(interval)
            current_mtime = self._modified_at(config_path)
            if current_mtime != previous_mtime:
                previous_mtime = current_mtime
                self._reload_config(agent_type, str(config_path))

    def route_request(self, agent_type: str, user_id: str) -> AgentInstance:
        """输入：目标 Agent 类型 ``agent_type`` 与可信用户标识 ``user_id``。

        输出：按稳定哈希和实例权重选择的健康实例；无可用实例时抛出 ``RuntimeError``。
        功能：让同一用户稳定落到同一灰度版本，并自动排除已切流或停止的实例。
        """

        healthy = [
            instance
            for instance in self._instances.get(agent_type, [])
            if instance.status in (AgentStatus.HEALTHY, AgentStatus.DEGRADED)
            and instance.traffic_weight > 0
        ]
        if not healthy:
            raise RuntimeError(f"{agent_type} 无可用实例")
        total_weight = sum(instance.traffic_weight for instance in healthy)
        target = (
            int(hashlib.md5(user_id.encode("utf-8")).hexdigest(), 16) % 10_000
        ) / 10_000 * total_weight
        cumulative = 0.0
        for instance in healthy:
            cumulative += instance.traffic_weight
            if target < cumulative:
                return instance
        return healthy[-1]

    async def canary_loop(self, interval: int = 30) -> None:
        """输入：每轮探测间隔 ``interval`` 秒。

        输出：持续运行直到任务被取消；原地更新全部受支持 Agent 的健康状态。
        功能：独立于用户请求定时运行固定业务查询，及时发现进程存活但工具调用失效的实例。
        """

        while True:
            for agent_type, probe in self.CANARY_PROBES.items():
                for instance in list(self._instances.get(agent_type, [])):
                    if instance.status is not AgentStatus.STOPPED:
                        await self._run_canary(instance, probe)
            await asyncio.sleep(interval)

    async def _run_canary(self, instance: AgentInstance, probe: CanaryProbe) -> None:
        """输入：待验证实例 ``instance`` 与固定探测 ``probe``。

        输出：无；原地记录连续失败次数，并在阈值后切走实例流量和发送告警。
        功能：校验 Agent 实际调用的工具和返回行数，而不是仅检查其 TCP 端口是否打开。
        """

        try:
            result = await asyncio.wait_for(
                self._send_query(instance, probe.query), timeout=probe.timeout
            )
            tools_used = result.get("tools_called", [])
            rows = len(result.get("data", []))
            if (
                all(tool in tools_used for tool in probe.expected_tools)
                and rows >= probe.min_result_rows
            ):
                instance.canary_failures = 0
                if instance.status is AgentStatus.DEGRADED:
                    instance.status = AgentStatus.HEALTHY
                    await self.alerter.send(
                        f"{instance.agent_id} 金丝雀探测恢复，状态恢复为 HEALTHY"
                    )
                return
        except (asyncio.TimeoutError, OSError, TypeError, ValueError, AttributeError):
            pass
        await self._record_canary_failure(instance)

    async def _record_canary_failure(self, instance: AgentInstance) -> None:
        """输入：本轮业务探测失败的 ``instance``。

        输出：无；原地调整降级状态，达到连续失败阈值时发布零流量和一次告警。
        功能：集中复用错误结果和超时结果的熔断处理，防止失效 Agent 继续接收灰度流量。
        """

        instance.canary_failures += 1
        if instance.canary_failures >= instance.canary_max_failures:
            if instance.status is not AgentStatus.UNHEALTHY:
                instance.status = AgentStatus.UNHEALTHY
                instance.traffic_weight = 0
                self._publish_traffic_config(instance.agent_type)
                await self.alerter.send(
                    f"报警: {instance.agent_id} 连续{instance.canary_max_failures}次金丝雀探测失败，已自动切流"
                )
        elif instance.status is AgentStatus.HEALTHY:
            instance.status = AgentStatus.DEGRADED

    def _reload_config(self, agent_type: str, config_file: str) -> None:
        """输入：Agent 类型 ``agent_type`` 与已变更配置文件路径 ``config_file``。

        输出：无；向所有允许热加载的同类实例发布 Redis 消息。
        功能：让 Prompt 和权限类配置在进程内重载，避免不必要的冷重启和会话状态丢失。
        """

        payload = json.dumps({"file": config_file, "timestamp": time.time()})
        for instance in self._instances.get(agent_type, []):
            if instance.hot_reload_enabled:
                self.redis.publish(f"agent:{instance.agent_id}:config_reload", payload)

    def _publish_traffic_config(self, agent_type: str) -> None:
        """输入：需要刷新 Gateway 配置的 Agent 类型 ``agent_type``。

        输出：无；将带 120 秒 TTL 的版本流量配置写入 Redis。
        功能：向 Gateway 暴露实例端口、权重和健康状态，使切流无需改动请求路由代码。
        """

        config = {
            instance.version: {
                "port": instance.port,
                "weight": instance.traffic_weight,
                "status": instance.status.value,
            }
            for instance in self._instances.get(agent_type, [])
        }
        self.redis.setex(f"traffic:{agent_type}", 120, json.dumps(config))

    def _register(self, instance: AgentInstance) -> None:
        """输入：健康启动完成的 ``instance``。

        输出：无；实例加入其 Agent 类型的注册列表。
        功能：维护 Harness 的唯一运行时实例注册表，供路由、灰度和探测共享。
        """

        self._instances.setdefault(instance.agent_type, []).append(instance)

    def _unregister(self, instance: AgentInstance) -> None:
        """输入：要从运行注册表移除的 ``instance``。

        输出：无；存在时从所属 Agent 类型列表删除。
        功能：在停止或切流淘汰后防止旧实例再次被路由或探测。
        """

        instances = self._instances.get(instance.agent_type, [])
        if instance in instances:
            instances.remove(instance)

    async def _wait_healthy(self, instance: AgentInstance, timeout: int) -> bool:
        """输入：新启动的 ``instance`` 与等待上限 ``timeout`` 秒。

        输出：端口可连接且进程仍存活时返回 ``True``，超时或进程退出时返回 ``False``。
        功能：在注册新版本前进行 TCP 就绪检查，保证滚动升级不会提前切流。
        """

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if instance.process is not None and instance.process.poll() is not None:
                return False
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("localhost", instance.port), timeout=2
                )
                writer.close()
                await writer.wait_closed()
                return True
            except OSError:
                await asyncio.sleep(1)
        return False

    def _next_port(self, agent_type: str) -> int:
        """输入：Agent 类型 ``agent_type``。

        输出：该类型约定端口段中尚未被注册实例占用的最小端口。
        功能：为滚动升级分配与稳定版本并存的新端口，默认类型使用 9000 段。
        """

        used_ports = {
            instance.port for instance in self._instances.get(agent_type, [])
        }
        port = {"return_analysis": 8101, "order_trace": 8201, "report_gen": 8301}.get(
            agent_type, 9000
        )
        while port in used_ports:
            port += 1
        return port

    async def _send_query(self, instance: AgentInstance, query: str) -> dict[str, Any]:
        """输入：目标 ``instance`` 与金丝雀自然语言查询 ``query``。

        输出：Agent 返回的 JSON 对象；网络或 JSON 异常向调用方传播。
        功能：通过 Agent 的本地换行分隔 JSON 协议执行独立业务探测，不混入用户会话流量。
        """

        reader, writer = await asyncio.open_connection("localhost", instance.port)
        try:
            writer.write(
                json.dumps({"query": query, "is_canary": True}).encode("utf-8")
                + b"\n"
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.readline(), timeout=10)
            return json.loads(response.decode("utf-8"))
        finally:
            writer.close()
            await writer.wait_closed()

    async def _terminate_process(self, instance: AgentInstance) -> None:
        """输入：持有可选子进程对象的 ``instance``。

        输出：无；存活进程在 30 秒内退出，超时后被强制终止。
        功能：复用停止和启动失败的进程清理，避免留下未注册的孤儿 Agent。
        """

        process = instance.process
        if process is None or process.poll() is not None:
            return
        process.send_signal(signal.SIGTERM)
        try:
            await asyncio.to_thread(process.wait, timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait)

    @staticmethod
    def _modified_at(config_path: Path) -> int | None:
        """输入：待监视的配置文件路径 ``config_path``。

        输出：文件纳秒级修改时间；文件当前不存在时返回 ``None``。
        功能：把文件创建、更新和删除统一为可比较的监视状态，而无需第三方文件观察器。
        """

        try:
            return config_path.stat().st_mtime_ns
        except FileNotFoundError:
            return None


__all__ = ["AgentHarness", "AgentInstance", "AgentStatus", "CanaryProbe"]
