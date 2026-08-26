"""NL2SQL 生成与安全校验的最小测试。"""

from unittest.mock import MagicMock, Mock

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy.exc import SQLAlchemyError

from nl2sql.generator import DeepSeekSQLGenerator
from nl2sql.pipeline import NL2SQLPipeline
from nl2sql.sql_sandbox import SQLValidator


VALID_SQL = """
SELECT
    c.category_name,
    COUNT(DISTINCT r.return_id) AS return_count
FROM t_aftersale_return AS r
JOIN t_order_main AS o
    ON o.order_id = r.order_id
JOIN t_product_sku AS s
    ON s.sku_code = r.sku_code
JOIN t_product_category AS c
    ON c.category_code = s.category_l3_code
WHERE 1 = 1
GROUP BY c.category_name
ORDER BY return_count DESC
"""


def test_validator_adds_time_range_and_limit() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证缺省时间范围和LIMIT会被自动补充。
    """

    validator = SQLValidator()
    passed, sql, error = validator.validate_and_fix(
        VALID_SQL,
    )

    assert passed is True
    assert error is None
    assert (
        "r.create_time >= (SELECT MAX(create_time) - "
        "(30 * 86400) FROM t_aftersale_return)" in sql
    )
    assert "NOW()" not in sql
    assert sql.endswith("LIMIT 500")


def test_validator_caps_large_limit() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证过大的LIMIT不会进入数据库。
    """

    validator = SQLValidator(max_limit=1000)
    passed, sql, error = validator.validate_and_fix(
        VALID_SQL + " LIMIT 5000"
    )

    assert passed is True
    assert error is None
    assert sql.endswith("LIMIT 1000")


@pytest.mark.parametrize(
    "dangerous_sql",
    [
        "DROP TABLE t_order_main",
        "DELETE FROM t_aftersale_return",
        "SELECT return_id FROM t_aftersale_return; "
        "DROP TABLE t_order_main",
        "SELECT SLEEP(10)",
    ],
)
def test_validator_rejects_dangerous_sql(
    dangerous_sql: str,
) -> None:
    """输入：参数 ``dangerous_sql``。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证危险操作和多语句均被拒绝。
    """

    validator = SQLValidator()
    passed, _sql, error = validator.validate_and_fix(
        dangerous_sql,
    )

    assert passed is False
    assert error


def test_validator_rejects_unknown_table() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证模型虚构的表名不能通过白名单。
    """

    sql = VALID_SQL.replace(
        "t_aftersale_return",
        "return_orders",
    )

    passed, _sql, error = (
        SQLValidator().validate_and_fix(sql)
    )

    assert passed is False
    assert "未登记" in str(error)


def test_rbac_is_inserted_before_group_by() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证行级权限在聚合前生效。
    """

    validator = SQLValidator()
    passed, sql, error = validator.validate_and_fix(
        VALID_SQL,
    )
    assert passed, error

    scoped_sql, params = validator.inject_rbac(
        sql,
        {
            "allowed_regions": ["HD"],
            "allowed_categories": ["C1-AC"],
        },
    )

    assert scoped_sql.index("o.region_code") < (
        scoped_sql.index("GROUP BY")
    )
    assert "HD" not in scoped_sql
    assert params["rbac_region_0"] == "HD"

from typing import Any, Mapping


class FakeExecutor:
    """代替真实数据库执行器，记录 Pipeline 最终提交的 SQL。"""

    def __init__(self) -> None:
        """输入：参数 ``self``。

        输出：无显式返回值；初始化测试替身实例状态。
        功能：初始化测试执行器。
        """

        self.calls: list[
            tuple[str, dict[str, Any]]
        ] = []

    def run(
        self,
        sql: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """输入：参数 ``self``、``sql``、``params``。

        输出：返回类型为 ``dict[str, list[dict[str, Any]]]`` 的测试数据或测试替身结果。
        功能：模拟 EXPLAIN 和只读查询。
        """

        copied_params = dict(params or {})
        self.calls.append((sql, copied_params))

        return {
            "explain": [{"table": "r", "type": "range"}],
            "rows": [
                {
                    "category_name": "壁挂式空调",
                    "return_count": 3,
                }
            ],
        }


def test_pipeline_runs_valid_sql() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证一条合法SQL可以经过完整Pipeline。
    """

    prompts: list[tuple[str, str]] = []

    def generator(system_prompt: str, user_prompt: str) -> str:
        """输入：Schema System Prompt 和用户问题消息。

        输出：返回类型为 ``str`` 的测试数据或测试替身结果。
        功能：返回固定合法 SQL，并分别记录 System 和 User 消息。
        """

        prompts.append((system_prompt, user_prompt))
        return VALID_SQL

    executor = FakeExecutor()
    pipeline = NL2SQLPipeline(
        generator=generator,
        validator=SQLValidator(),
        executor=executor,  # type: ignore[arg-type]
    )

    result = pipeline.run(
        "最近30天退单最多的品类",
        {
            "allowed_regions": ["HD"],
            "allowed_categories": ["C1-AC"],
        },
    )

    assert len(prompts) == 1
    assert "可用Schema" in prompts[0][0]
    assert "SELECT MAX(create_time)" in prompts[0][0]
    assert "NOW()" not in prompts[0][0]
    assert '"空调": "C1-AC"' in prompts[0][0]
    assert "禁止用 c.category_name LIKE" in prompts[0][0]
    assert prompts[0][1] == "最近30天退单最多的品类"
    assert len(executor.calls) == 1
    assert result["rows"][0]["category_name"] == "壁挂式空调"

    final_sql, params = executor.calls[0]

    # 时间范围和RBAC必须在聚合前出现。
    assert final_sql.index("r.create_time >=") < (
        final_sql.index("GROUP BY")
    )
    assert final_sql.index("o.region_code") < (
        final_sql.index("GROUP BY")
    )
    assert "NOW()" not in final_sql

    # 权限值必须使用绑定参数，不能直接出现在SQL中。
    assert "HD" not in final_sql
    assert params["rbac_region_0"] == "HD"


def test_pipeline_retries_invalid_sql_once() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证第一次生成危险SQL时会反馈错误并重新生成。
    """

    generated_sql = iter(
        [
            "DROP TABLE t_order_main",
            VALID_SQL,
        ]
    )
    prompts: list[tuple[str, str]] = []

    def generator(system_prompt: str, user_prompt: str) -> str:
        """输入：Schema System Prompt 和本轮用户修复消息。

        输出：返回类型为 ``str`` 的测试数据或测试替身结果。
        功能：按顺序返回危险 SQL 和合法 SQL，并记录两类消息。
        """

        prompts.append((system_prompt, user_prompt))
        return next(generated_sql)

    executor = FakeExecutor()
    pipeline = NL2SQLPipeline(
        generator=generator,
        validator=SQLValidator(),
        executor=executor,  # type: ignore[arg-type]
        max_attempts=2,
    )

    result = pipeline.run(
        "统计退单品类",
        {},
    )

    assert len(prompts) == 2
    assert len(executor.calls) == 1
    assert result["rows"]

    # 第二次Prompt应携带第一次校验失败的原因。
    assert prompts[0][0] == prompts[1][0]
    assert "上一条 SQL 失败" in prompts[1][1]
    assert "禁止使用 DROP" in prompts[1][1]


def test_pipeline_rejects_empty_question() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证空问题不会调用LLM和数据库。
    """

    generator_called = False

    def generator(_system_prompt: str, _user_prompt: str) -> str:
        """输入：未预期使用的 System Prompt 和 User Prompt。

        输出：返回类型为 ``str`` 的测试数据或测试替身结果。
        功能：记录意外的模型调用。
        """

        nonlocal generator_called
        generator_called = True
        return VALID_SQL

    pipeline = NL2SQLPipeline(
        generator=generator,
        validator=SQLValidator(),
        executor=FakeExecutor(),  # type: ignore[arg-type]
    )

    with pytest.raises(
        ValueError,
        match="question 不能为空",
    ):
        pipeline.run("   ", {})

    assert generator_called is False


def test_pipeline_stops_after_max_attempts() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证模型持续生成危险SQL时不会无限重试。
    """

    call_count = 0

    def generator(_system_prompt: str, _user_prompt: str) -> str:
        """输入：Schema System Prompt 和本轮用户消息。

        输出：返回类型为 ``str`` 的测试数据或测试替身结果。
        功能：始终返回危险SQL。
        """

        nonlocal call_count
        call_count += 1
        return "DELETE FROM t_aftersale_return"

    executor = FakeExecutor()
    pipeline = NL2SQLPipeline(
        generator=generator,
        validator=SQLValidator(),
        executor=executor,  # type: ignore[arg-type]
        max_attempts=2,
    )

    with pytest.raises(
        ValueError,
        match="无法生成安全SQL",
    ):
        pipeline.run("查询退单", {})

    assert call_count == 2
    assert executor.calls == []


def test_validator_allows_total_count_without_group_by() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：只有总量指标时不需要 GROUP BY。
    """

    sql = """
    SELECT COUNT(DISTINCT r.return_id) AS return_count
    FROM t_aftersale_return AS r
    JOIN t_order_main AS o ON o.order_id = r.order_id
    JOIN t_product_sku AS s ON s.sku_code = r.sku_code
    WHERE 1 = 1
    """
    passed, final_sql, error = SQLValidator().validate_and_fix(sql)

    assert passed is True
    assert error is None
    assert final_sql.endswith("LIMIT 500")


@pytest.mark.parametrize(
    ("field", "select_expression"),
    [
        ("return_id", "r.return_id, COUNT(*) AS return_count"),
        ("user_id", "r.user_id, COUNT(*) AS return_count"),
        ("return_amount", "r.return_amount, COUNT(*) AS return_count"),
    ],
)
def test_validator_rejects_detail_fields(
    field: str,
    select_expression: str,
) -> None:
    """输入：参数 ``field``、``select_expression``。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：主键、用户和原始金额不能作为聚合维度返回。
    """

    sql = f"""
    SELECT {select_expression}
    FROM t_aftersale_return AS r
    JOIN t_order_main AS o ON o.order_id = r.order_id
    JOIN t_product_sku AS s ON s.sku_code = r.sku_code
    WHERE 1 = 1
    GROUP BY {field}
    """
    passed, _final_sql, error = SQLValidator().validate_and_fix(sql)

    assert passed is False
    assert field in str(error)


def test_validator_allows_aggregated_amount() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：退单金额经过 SUM 聚合后可以安全返回。
    """

    sql = VALID_SQL.replace(
        "COUNT(DISTINCT r.return_id) AS return_count",
        "SUM(r.return_amount) AS total_amount",
    )
    passed, final_sql, error = SQLValidator().validate_and_fix(sql)

    assert passed is True
    assert error is None
    assert "SUM(r.return_amount)" in final_sql


def test_validator_requires_group_by_for_dimension() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：SELECT 包含维度和指标时必须显式分组。
    """

    sql = VALID_SQL.replace("GROUP BY c.category_name", "")
    passed, _final_sql, error = SQLValidator().validate_and_fix(sql)

    assert passed is False
    assert "GROUP BY" in str(error)


def test_validator_rejects_unknown_column() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：Schema Registry 中不存在的限定字段必须在 EXPLAIN 前拒绝。
    """

    sql = VALID_SQL.replace("c.category_name", "c.fake_name")
    passed, _final_sql, error = SQLValidator().validate_and_fix(sql)

    assert passed is False
    assert "c.fake_name" in str(error)


def test_validator_allows_registered_table_subquery_alias() -> None:
    """输入：使用 ``r2`` 显式别名读取退单表最大时间的聚合 SQL。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证已登记表的子查询别名不会被误判为未知字段来源。
    """
    sql = VALID_SQL.replace(
        "WHERE 1 = 1",
        """WHERE r.create_time >= (
            SELECT MAX(r2.create_time) - (30 * 86400)
            FROM t_aftersale_return AS r2
        )""",
    )

    passed, final_sql, error = SQLValidator().validate_and_fix(sql)

    assert passed is True
    assert error is None
    assert "r2.create_time" in final_sql
    assert "NOW()" not in final_sql


def test_rbac_always_caps_role_date_range() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：模型SQL之外还必须追加角色允许的最大查询天数。
    """

    validator = SQLValidator()
    passed, sql, error = validator.validate_and_fix(VALID_SQL)
    assert passed, error

    scoped_sql, params = validator.inject_rbac(
        sql,
        {"date_range_days": 90},
    )

    assert ":rbac_date_range_days" in scoped_sql
    assert params["rbac_date_range_days"] == 90
    assert scoped_sql.index(":rbac_date_range_days") < scoped_sql.index("GROUP BY")
    assert "SELECT MAX(create_time)" in scoped_sql
    assert "NOW()" not in scoped_sql


def test_pipeline_retries_database_validation_error() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：EXPLAIN 或查询失败时把错误反馈给模型并最多再生成一次。
    """

    prompts: list[tuple[str, str]] = []

    def generator(system_prompt: str, user_prompt: str) -> str:
        """输入：Schema System Prompt 和本轮用户修复消息。

        输出：返回类型为 ``str`` 的测试数据或测试替身结果。
        功能：记录两类消息并持续返回合法结构 SQL。
        """

        prompts.append((system_prompt, user_prompt))
        return VALID_SQL

    class FlakyExecutor(FakeExecutor):
        """第一次模拟数据库失败，第二次返回固定结果。"""

        def run(
            self,
            sql: str,
            params: Mapping[str, Any] | None = None,
        ) -> dict[str, list[dict[str, Any]]]:
            """输入：参数 ``self``、``sql``、``params``。

            输出：返回类型为 ``dict[str, list[dict[str, Any]]]`` 的测试数据或测试替身结果。
            功能：第一次抛 SQLAlchemyError，之后委托父类模拟成功。
            """

            if not self.calls:
                self.calls.append((sql, dict(params or {})))
                raise SQLAlchemyError("EXPLAIN failed")
            return super().run(sql, params)

    executor = FlakyExecutor()
    pipeline = NL2SQLPipeline(
        generator=generator,
        validator=SQLValidator(),
        executor=executor,  # type: ignore[arg-type]
        max_attempts=2,
    )
    result = pipeline.run("按品类统计退单", {})

    assert result["rows"]
    assert len(prompts) == 2
    assert "数据库校验失败" in prompts[1][1]


def test_deepseek_generator_uses_langchain_model() -> None:
    """输入：注入返回固定 SQL 的 LangChain 聊天模型替身。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证生成器通过标准 System/Human 消息调用公共模型接口。
    """

    chat_model = Mock()
    chat_model.invoke.return_value = AIMessage(content=VALID_SQL)
    generator = DeepSeekSQLGenerator(
        api_key="test-key",
        model="deepseek-v4-flash",
        chat_model=chat_model,
    )

    assert generator("schema prompt", "user question") == VALID_SQL.strip()
    messages = chat_model.invoke.call_args.args[0]
    assert messages[0].content == "schema prompt"
    assert messages[1].content == "user question"


def test_deepseek_generator_requires_api_key() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：API Key 缺失时不应发起外部请求。
    """

    generator = DeepSeekSQLGenerator(api_key="")

    with pytest.raises(RuntimeError, match="DEEPSEEK_API"):
        generator("schema prompt", "user question")


def test_dynamic_aftersale_tools_use_shared_pipeline() -> None:
    """输入：无显式参数；读取售后 MCP 与 NL2SQL Runtime 的模块级对象。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：确认所有售后动态分析工具引用统一创建的 Pipeline 实例。
    """
    from mcp_suning.servers import aftersale as aftersale_server
    from nl2sql.runtime import nl2sql_pipeline

    assert aftersale_server.nl2sql_pipeline is nl2sql_pipeline


def test_return_stats_nl2sql_authorizes_then_uses_shared_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：pytest ``monkeypatch`` 和固定统计参数。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证退单统计把结构化参数转成受控问题并交给 Lite Pipeline。
    """
    from mcp_suning.servers import aftersale as aftersale_server

    events: list[str] = []
    safe_filters = {
        "category": "空调",
        "date_range_days": 14,
        "data_scope": "full",
        "allowed_categories": ["C1-AC"],
    }

    def fake_authorize(
        _ctx: object,
        tool_name: str,
        filters: Mapping[str, Any],
    ) -> tuple[None, dict[str, Any]]:
        """输入：MCP 上下文、工具名和未经授权的业务筛选参数。

        输出：空用户和固定安全筛选字典。
        功能：记录鉴权顺序，并确认原始统计参数完整传入权限层。
        """
        events.append(f"authorize:{tool_name}")
        assert dict(filters) == {"date_range_days": 14, "category": "空调"}
        return None, dict(safe_filters)

    class FakePipeline:
        """记录退单统计生成的问题和授权筛选。"""

        def run(
            self,
            question: str,
            received_filters: Mapping[str, Any],
        ) -> dict[str, Any]:
            """输入：结构化参数形成的问题和安全筛选。

            输出：包含固定 SQL、执行计划和统计行的 Pipeline 测试结果。
            功能：确认日期维度、品类和权限被交给动态 SQL 流程。
            """
            events.append("pipeline")
            assert "最近 14 天" in question
            assert "按退单申请日期分组" in question
            assert "只统计品类 空调" in question
            assert dict(received_filters) == safe_filters
            return {
                "sql": "SELECT COUNT(*) AS return_count",
                "explain": [],
                "rows": [{"dimension_code": "2026-08-09", "return_count": 3}],
            }

    monkeypatch.setattr(
        aftersale_server,
        "authorize_mcp_request",
        fake_authorize,
    )
    monkeypatch.setattr(
        aftersale_server,
        "nl2sql_lite_pipeline",
        FakePipeline(),
    )

    result = aftersale_server.query_return_stats_nl2sql(
        None,  # type: ignore[arg-type]
        group_by="day",
        date_range_days=14,
        category="空调",
    )

    assert events == ["authorize:query_return_stats_nl2sql", "pipeline"]
    assert result == [{"dimension_code": "2026-08-09", "return_count": 3}]


def test_aftersale_nl2sql_tool_authorizes_before_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：参数 ``monkeypatch``。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：公开 MCP 工具必须把鉴权结果传入 NL2SQL Pipeline。
    """

    from mcp_suning.servers import aftersale as aftersale_server

    events: list[str] = []
    safe_filters = {
        "date_range_days": 90,
        "data_scope": "full",
        "allowed_regions": ["HD"],
    }

    def fake_authorize(
        _ctx: object,
        tool_name: str,
        _filters: Mapping[str, Any],
    ) -> tuple[None, dict[str, Any]]:
        """输入：参数 ``_ctx``、``tool_name``、``_filters``。

        输出：返回类型为 ``tuple[None, dict[str, Any]]`` 的测试数据或测试替身结果。
        功能：记录鉴权事件并返回固定安全范围。
        """

        events.append(f"authorize:{tool_name}")
        return None, dict(safe_filters)

    class FakePipeline:
        """记录 MCP 交给 Pipeline 的问题和权限。"""

        def run(
            self,
            question: str,
            received_filters: Mapping[str, Any],
        ) -> dict[str, Any]:
            """输入：参数 ``self``、``question``、``received_filters``。

            输出：返回类型为 ``dict[str, Any]`` 的测试数据或测试替身结果。
            功能：返回固定聚合结果并记录调用顺序。
            """

            events.append("pipeline")
            assert question == "最近7天空调退单量"
            assert dict(received_filters) == safe_filters
            return {
                "sql": "SELECT COUNT(*) AS return_count",
                "explain": [],
                "rows": [{"return_count": 3}],
            }

    monkeypatch.setattr(
        aftersale_server,
        "authorize_mcp_request",
        fake_authorize,
    )
    monkeypatch.setattr(
        aftersale_server,
        "nl2sql_pipeline",
        FakePipeline(),
    )

    result = aftersale_server.query_aftersale_nl2sql(
        "最近7天空调退单量",
        None,  # type: ignore[arg-type]
    )

    assert events == [
        "authorize:query_aftersale_nl2sql",
        "pipeline",
    ]
    assert result["success"] is True
    assert result["row_count"] == 1


def test_sku_return_rate_tool_uses_scoped_fixed_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：pytest ``monkeypatch``、固定授权范围和模拟 SQLAlchemy 结果。

    输出：无；断言固定 SQL 使用订单明细分母、左连接退单和绑定的 RBAC 参数。
    功能：防止 SKU 退单率退回 NL2SQL 或因 WHERE 过滤退单表而丢失无退单订单。
    """

    from mcp_suning.servers import aftersale as aftersale_server

    safe_filters = {
        "date_range_days": 14,
        "data_scope": "full",
        "allowed_regions": ["HD"],
        "allowed_categories": ["C1-AC"],
    }
    result = MagicMock()
    result.mappings.return_value.all.return_value = [
        {
            "sku_code": "SKU-AC-GL-15P",
            "product_name": "格力空调",
            "returned_order_count": 2,
            "order_count": 10,
            "return_rate_pct": 20.0,
        }
    ]
    connection = MagicMock()
    connection.execute.return_value = result
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection

    def fake_authorize(
        _ctx: object,
        tool_name: str,
        filters: Mapping[str, Any],
    ) -> tuple[None, dict[str, Any]]:
        """输入：MCP 上下文、工具名和调用参数。

        输出：空用户和固定的授权后筛选条件。
        功能：验证固定退单率工具使用正确工具身份和可收窄的业务筛选。
        """

        assert tool_name == "query_sku_return_rate"
        assert dict(filters) == {"date_range_days": 14, "category": "空调"}
        return None, dict(safe_filters)

    monkeypatch.setattr(aftersale_server, "authorize_mcp_request", fake_authorize)
    monkeypatch.setattr(aftersale_server, "engine", engine)

    payload = aftersale_server.query_sku_return_rate(
        None,  # type: ignore[arg-type]
        date_range_days=14,
        limit=5,
        category="空调",
    )

    statement, params = connection.execute.call_args.args
    assert "FROM t_order_item AS i" in statement.text
    assert "LEFT JOIN t_aftersale_return AS r" in statement.text
    assert "COUNT(DISTINCT r.order_id)" in statement.text
    assert "o.region_code IN (:rbac_region_0)" in statement.text
    assert params == {
        "rbac_region_0": "HD",
        "rbac_category_0": "C1-AC",
        "rbac_category_child_0": "C1-AC-%",
        "date_range_days": 14,
        "limit": 5,
    }
    assert payload == {
        "success": True,
        "date_range_days": 14,
        "row_count": 1,
        "rows": result.mappings.return_value.all.return_value,
    }
