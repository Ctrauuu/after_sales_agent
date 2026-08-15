"""在只读连接上执行 EXPLAIN 和 SELECT。"""

from typing import Any, Mapping

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class ReadOnlyExecutor:
    def __init__(self, engine: Engine, max_execution_ms: int = 5000) -> None:
        """输入：用于 NL2SQL 查询的 SQLAlchemy ``engine`` 和最大执行毫秒数。

        输出：初始化后的只读执行器实例状态。
        功能：保存数据库连接池和 SELECT 执行上限；账号只读权限由部署配置保证。
        """
        self.engine = engine
        self.max_execution_ms = max(1, int(max_execution_ms))

    def run(
        self,
        sql: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """输入：已通过校验的 ``sql`` 和可选绑定参数 ``params``。

        输出：包含 ``explain`` 执行计划和 ``rows`` 查询行的字典。
        功能：在同一只读连接上设置超时，依次执行 EXPLAIN 和真实 SELECT。
        """
        params = dict(params or {})
        with self.engine.connect() as connection:
            connection.execute(
                sa.text("SET SESSION MAX_EXECUTION_TIME = :max_execution_ms"),
                {"max_execution_ms": self.max_execution_ms},
            )
            explain = connection.execute(
                sa.text("EXPLAIN " + sql),
                params,
            ).mappings().all()
            rows = connection.execute(sa.text(sql), params).mappings().all()
        return {
            "explain": [dict(row) for row in explain],
            "rows": [dict(row) for row in rows],
        }
