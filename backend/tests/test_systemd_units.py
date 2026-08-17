"""验证统一 MCP systemd 模板及聚合 target。"""

from pathlib import Path


SYSTEMD_ROOT = Path(__file__).resolve().parents[1] / "infra" / "systemd"
MCP_INSTANCES = {"order", "aftersale", "product", "logistics", "payment", "timeline"}


def test_systemd_template_manages_every_mcp_instance() -> None:
    """输入：仓库中的 systemd 模板、聚合 target 和固定 MCP 实例集合。

    输出：无；模板入口、统一生命周期或实例清单缺失时断言失败。
    功能：保证一个参数化 service 启动六个 Python 模块，target 可统一启停全部实例。
    """

    template = (SYSTEMD_ROOT / "suning-mcp@.service").read_text(encoding="utf-8")
    target = (SYSTEMD_ROOT / "suning-mcp.target").read_text(encoding="utf-8")

    assert "mcp_suning.servers.%i" in template
    assert "EnvironmentFile=/home/ctrau/suning-hermes-agent/backend/.env" in template
    assert "PartOf=suning-mcp.target" in template
    assert {
        line.removeprefix("Wants=suning-mcp@").removesuffix(".service")
        for line in target.splitlines()
        if line.startswith("Wants=suning-mcp@")
    } == MCP_INSTANCES
