"""Prompt YAML 注册、版本排序与 NL2SQL A/B 评估测试。"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import nl2sql.pipeline as nl2sql_pipeline_module
import nl2sql.prompt_evaluation as prompt_evaluation
from nl2sql.pipeline import NL2SQLPipeline
from nl2sql.prompt_registry import (
    GoldenCase,
    NL2SQLEvaluator,
    PromptRegistry,
    PromptVersion,
    default_registry,
    evaluation_passed,
    load_golden_dataset,
)
from nl2sql.sql_sandbox import SQLValidator


VALID_SQL = """
SELECT
    c.category_name,
    COUNT(DISTINCT r.return_id) AS return_count
FROM t_aftersale_return AS r
JOIN t_order_main AS o ON o.order_id = r.order_id
JOIN t_product_sku AS s ON s.sku_code = r.sku_code
JOIN t_product_category AS c ON c.category_code = s.category_l3_code
WHERE 1 = 1
GROUP BY c.category_name
"""


class FakeExecutor:
    """记录 EXPLAIN 加真实查询都通过的最小只读执行器替身。"""

    def __init__(self) -> None:
        """输入：参数 ``self``。

        输出：无显式返回值；初始化空调用记录。
        功能：创建可供评估器验证 SQL 执行分支的内存替身。
        """
        self.sql: list[str] = []

    def run(
        self,
        sql: str,
        _params: dict[str, Any] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """输入：已经通过 SQL 沙箱的候选 ``sql`` 和可选绑定参数 ``_params``。

        输出：固定 EXPLAIN 和查询行字典。
        功能：记录 SQL 并模拟生产只读执行器的成功返回结构，兼容评估和 Pipeline 调用。
        """
        self.sql.append(sql)
        return {
            "explain": [{"table": "r"}],
            "rows": [{"category_name": "空调", "return_count": 3}],
        }


def make_prompt(version: str, content: str) -> PromptVersion:
    """输入：版本号 ``version`` 和完整模板 ``content``。

    输出：测试用 ``PromptVersion``。
    功能：集中构造 A/B 测试所需的最小合法 Prompt 元数据。
    """
    return PromptVersion(
        prompt_id="nl2sql",
        version=version,
        content=content,
        description="测试版本",
        author="tester",
        created_at="2026-08-16T00:00:00+08:00",
    )


def test_registry_round_trips_and_uses_numeric_version_sorting(tmp_path: Path) -> None:
    """输入：pytest 提供的临时目录 ``tmp_path``。

    输出：无；断言 YAML 往返保存和数字版本排序结果。
    功能：验证 Git 注册表按单文件保存 Prompt，并正确选择 v1.10 而非字典序版本。
    """
    registry = PromptRegistry(tmp_path)
    registry.save_prompt(make_prompt("v1.2", "旧模板"))
    registry.save_prompt(make_prompt("v1.10", "新模板"))

    assert registry.list_versions("nl2sql") == ["v1.2", "v1.10"]
    assert registry.load_latest("nl2sql").content == "新模板"


def test_load_golden_dataset_preserves_traceability(tmp_path: Path) -> None:
    """输入：pytest 临时目录 ``tmp_path`` 和写入其中的脱敏 Golden JSON。

    输出：无；断言载入后的用例保留来源会话和预期结果签名。
    功能：验证 A/B 管道读取真实对话采样所需的可追溯输入结构。
    """
    dataset_path = tmp_path / "golden_nl2sql.json"
    dataset_path.write_text(
        json.dumps(
            [
                {
                    "case_id": "case-1",
                    "natural_language": "近7天空调退单原因分布",
                    "source_conversation_id": "sanitized-conversation-1",
                    "expected_result_signature": {
                        "row_count_min": 1,
                        "key_columns": ["return_reason_desc", "return_count"],
                    },
                    "difficulty": "medium",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    cases = load_golden_dataset(dataset_path)

    assert cases[0].source_conversation_id == "sanitized-conversation-1"
    assert cases[0].expected_result_signature == {
        "row_count_min": 1,
        "key_columns": ["return_reason_desc", "return_count"],
    }


def test_load_golden_dataset_rejects_empty_or_unverifiable_cases(tmp_path: Path) -> None:
    """输入：pytest 临时目录 ``tmp_path`` 和两个不具评估条件的 JSON 数据集。

    输出：无；断言空数据集和缺少期望值的用例都抛出 ``ValueError``。
    功能：防止 CI 因空数据或不可验证的生产采样而错误放行 Prompt 变更。
    """
    empty_path = tmp_path / "empty.json"
    unverifiable_path = tmp_path / "unverifiable.json"
    empty_path.write_text("[]", encoding="utf-8")
    unverifiable_path.write_text(
        json.dumps(
            [
                {
                    "case_id": "case-1",
                    "natural_language": "统计退单量",
                    "source_conversation_id": "sanitized-conversation-1",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="不能为空"):
        load_golden_dataset(empty_path)
    with pytest.raises(ValueError, match="expected_sql"):
        load_golden_dataset(unverifiable_path)


def test_evaluator_votes_then_reports_regression() -> None:
    """输入：无显式参数；构造一条结果签名 Golden 用例和确定性生成器。

    输出：无；断言新版危险 SQL 让 A/B 报告判定旧版获胜。
    功能：验证三次运行的多数投票、SQL 沙箱与 EXPLAIN 执行、结果匹配率和退化判定链路。
    """
    old_runs = iter(["DROP TABLE t_aftersale_return", VALID_SQL, VALID_SQL])

    def generator(system_prompt: str, _question: str) -> str:
        """输入：版本化 ``system_prompt`` 和忽略内容的用户问题。

        输出：旧版两次合法 SQL 和一次异常 SQL，新版始终返回危险 SQL。
        功能：让评估测试稳定覆盖多数投票、SQL 沙箱与新旧 Prompt 的质量差异。
        """
        return next(old_runs) if system_prompt == "old" else "DROP TABLE t_aftersale_return"

    executor = FakeExecutor()
    report = NL2SQLEvaluator(
        generator,
        SQLValidator(),
        executor,
        repeat_per_case=3,
    ).evaluate(
        make_prompt("v1.0", "old"),
        make_prompt("v1.1", "new"),
        [
            GoldenCase(
                case_id="case-1",
                natural_language="最近 7 天空调退单量",
                source_conversation_id="sanitized-conversation-1",
                expected_result_signature={
                    "row_count_min": 1,
                    "key_columns": ["category_name", "return_count"],
                },
            )
        ],
    )

    assert report.winner == "old"
    assert report.sql_syntax_pass_rate_old == 1.0
    assert report.sql_syntax_pass_rate == 0.0
    assert report.result_match_rate_old == 1.0
    assert report.result_match_rate == 0.0
    assert report.results[0]["winner"] == "old"
    assert len(executor.sql) == 1


def test_evaluation_passed_requires_accuracy_improvement_without_regression() -> None:
    """输入：无显式参数；构造相同综合胜者但不同准确指标的评估报告。

    输出：无；断言只有 SQL 准确率提升且两个指标均不退化的报告可通过。
    功能：验证 CI 不会把单纯延迟改善或结果匹配下降当成可自动上线的 Prompt 改进。
    """
    def generator(_prompt: str, _query: str) -> str:
        """输入：忽略内容的 Prompt 和用户问题。

        输出：固定合法聚合 SQL。
        功能：构造可手工调整汇总指标的确定性 A/B 报告。
        """
        return VALID_SQL

    improved = NL2SQLEvaluator(
        generator,
        SQLValidator(),
        FakeExecutor(),
    ).evaluate(
        make_prompt("v1.0", "old"),
        make_prompt("v1.1", "new"),
        [
            GoldenCase(
                case_id="case-1",
                natural_language="统计空调退单数量",
                source_conversation_id="sanitized-conversation-3",
                expected_sql=VALID_SQL,
            )
        ],
    )
    improved.winner = "new"
    improved.sql_syntax_pass_rate_old = 0.5

    assert evaluation_passed(improved) is True
    improved.result_match_rate = 0.5
    assert evaluation_passed(improved) is False


def test_ci_entry_blocks_tied_candidate_for_manual_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：临时注册表和 Golden Dataset，以及替换后的 CI 运行时依赖。

    输出：无；断言指标持平的候选 Prompt 返回进程码 ``2``。
    功能：端到端验证 CI 不会把没有 SQL 准确率提升的候选版本自动放行。
    """
    registry = PromptRegistry(tmp_path / "prompts")
    registry.save_prompt(make_prompt("v1.0", "old"))
    registry.save_prompt(make_prompt("v1.1", "new"))
    dataset_path = tmp_path / "golden.json"
    dataset_path.write_text(
        json.dumps(
            [
                {
                    "case_id": "case-1",
                    "natural_language": "统计空调退单数量",
                    "source_conversation_id": "sanitized-conversation-4",
                    "expected_sql": VALID_SQL,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def default_test_registry() -> PromptRegistry:
        """输入：无显式参数；使用本测试创建的临时注册表。

        输出：含新旧两个版本的 ``PromptRegistry``。
        功能：隔离 CI 入口，避免读取项目真实 Prompt 文件。
        """
        return registry

    def generator(_prompt: str, _query: str) -> str:
        """输入：忽略内容的 Prompt 和用户问题。

        输出：固定合法聚合 SQL。
        功能：构造两版质量相同的候选，触发人工审核返回码。
        """
        return VALID_SQL

    monkeypatch.setattr(prompt_evaluation, "default_registry", default_test_registry)
    monkeypatch.setattr(
        prompt_evaluation,
        "nl2sql_pipeline",
        SimpleNamespace(
            generator=generator,
            validator=SQLValidator(),
            executor=FakeExecutor(),
            prompt_version="v1.0",
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["prompt_evaluation", "--dataset", str(dataset_path)],
    )

    assert prompt_evaluation.main() == 2


def test_pipeline_uses_explicitly_deployed_prompt_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：临时 Prompt 注册表、固定版本号和替换后的默认注册表函数。

    输出：无；断言 Pipeline 只向生成器传入已部署版本的模板。
    功能：防止新增候选 YAML 因数字版本更高而在未通过 A/B 前影响线上 NL2SQL。
    """
    registry = PromptRegistry(tmp_path / "prompts")
    registry.save_prompt(make_prompt("v1.0", "已部署 {{schema_json}}"))
    registry.save_prompt(make_prompt("v1.1", "候选 {{schema_json}}"))
    prompts: list[str] = []

    def default_test_registry() -> PromptRegistry:
        """输入：无显式参数；使用本测试创建的临时注册表。

        输出：同时含已部署版与候选版的 ``PromptRegistry``。
        功能：隔离 Pipeline 的 Prompt 查找路径。
        """
        return registry

    def generator(system_prompt: str, _question: str) -> str:
        """输入：Pipeline 生成的 System Prompt 和忽略内容的用户问题。

        输出：固定合法聚合 SQL。
        功能：记录实际使用的模板，验证 Pipeline 的版本固定行为。
        """
        prompts.append(system_prompt)
        return VALID_SQL

    monkeypatch.setattr(nl2sql_pipeline_module, "default_registry", default_test_registry)
    NL2SQLPipeline(
        generator,
        SQLValidator(),
        FakeExecutor(),  # type: ignore[arg-type]
        prompt_version="v1.0",
    ).run("统计空调退单数量", {})

    assert "已部署" in prompts[0]
    assert "候选" not in prompts[0]


def test_evaluator_compares_expected_sql_after_safety_normalization() -> None:
    """输入：无显式参数；构造包含未补全安全条件的期望 SQL Golden 用例。

    输出：无；断言新旧版本均能匹配经同一 SQL 沙箱规范化的期望 SQL。
    功能：验证 SQL 期望值可复用生产默认时间和 LIMIT 规则，不要求数据集重复这些服务端条件。
    """
    def generator(_system_prompt: str, _question: str) -> str:
        """输入：忽略内容的 System Prompt 和用户问题。

        输出：固定合法聚合 SQL。
        功能：为期望 SQL 评估分支提供没有随机性的模型替身。
        """
        return VALID_SQL

    report = NL2SQLEvaluator(
        generator,
        SQLValidator(),
        FakeExecutor(),
    ).evaluate(
        make_prompt("v1.0", "old"),
        make_prompt("v1.1", "new"),
        [
            GoldenCase(
                case_id="case-sql",
                natural_language="统计空调退单数量",
                source_conversation_id="sanitized-conversation-2",
                expected_sql=VALID_SQL,
            )
        ],
    )

    assert report.result_match_rate_old == 1.0
    assert report.result_match_rate == 1.0


def test_runtime_registry_contains_renderable_nl2sql_prompt() -> None:
    """输入：无显式参数；读取项目内默认 Prompt 注册表。

    输出：无；断言运行时 Prompt 模板含有 Schema 注入占位符。
    功能：防止 NL2SQL Pipeline 依赖的 Git 版本化初始 Prompt 被遗漏或失去动态 Schema 位置。
    """
    prompt = default_registry().load_latest("nl2sql")

    assert prompt.version == "v1.0"
    assert "{{schema_json}}" in prompt.content
