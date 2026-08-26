"""验证 Cron 热点预计算结果只在相同 RBAC 范围内复用。"""

from typing import Any

import pytest

from mcp_suning.servers import aftersale


class _FakeRedis:
    """记录预计算缓存键和值的最小 Redis 替身。"""

    def __init__(self) -> None:
        """输入：无。

        输出：初始化空缓存和过期时间记录。
        功能：提供无需网络的 Redis ``get`` 与 ``setex`` 验证环境。
        """

        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        """输入：预计算 Redis 键 ``key``。

        输出：已保存的文本或 ``None``。
        功能：模拟热点缓存读取。
        """

        return self.values.get(key)

    def setex(self, key: str, seconds: int, value: str) -> None:
        """输入：Redis 键、过期秒数和 JSON 文本值。

        输出：无；原地保存缓存记录。
        功能：模拟 Cron 预热的一小时写入操作。
        """

        self.values[key] = value
        self.ttls[key] = seconds


def test_precomputed_return_stats_requires_an_exact_authorized_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：同一热点查询的全量与匿名 RBAC 安全范围。

    输出：无；跨权限范围复用或 TTL 错误时由 pytest 报告失败。
    功能：验证 Cron 写入的统计仅能被安全筛选条件完全一致的调用读取。
    """

    redis = _FakeRedis()
    monkeypatch.setattr(aftersale.settings, "redis_url", "redis://test/0")
    monkeypatch.setattr(
        aftersale.Redis,
        "from_url",
        lambda *_args, **_kwargs: redis,
    )
    full_scope: dict[str, Any] = {
        "date_range_days": 1,
        "category": "",
        "data_scope": "full",
    }
    anonymous_scope = {**full_scope, "data_scope": "anonymized"}
    rows = [{"dimension_name": "大家电", "return_count": 12}]

    aftersale._store_precomputed_return_stats("category", full_scope, rows)

    assert aftersale._load_precomputed_return_stats("category", full_scope) == rows
    assert aftersale._load_precomputed_return_stats("category", anonymous_scope) is None
    assert set(redis.ttls.values()) == {3600}
