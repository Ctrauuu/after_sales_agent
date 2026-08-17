"""通过 Git 跟踪的 YAML 管理 NL2SQL Prompt 与离线 A/B 评估。"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml


SQLGenerator = Callable[[str, str], str]


@dataclass
class PromptVersion:
    """一条由 Git 跟踪的完整 Prompt 模板。"""

    prompt_id: str
    version: str
    content: str
    description: str
    author: str
    created_at: str
    parent_version: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class GoldenCase:
    """一条从已采纳真实对话脱敏后导出的 NL2SQL 基准样本。"""

    case_id: str
    natural_language: str
    source_conversation_id: str
    expected_sql: str | None = None
    expected_result_signature: dict[str, Any] | None = None
    difficulty: str = "easy"


@dataclass
class EvalReport:
    """一次新旧 Prompt 对比产生的可序列化结果。"""

    prompt_id: str
    old_version: str
    new_version: str
    total_cases: int
    results: list[dict[str, Any]]
    sql_syntax_pass_rate: float
    result_match_rate: float
    avg_tokens_old: int
    avg_tokens_new: int
    avg_latency_ms_old: float
    avg_latency_ms_new: float
    winner: str
    sql_syntax_pass_rate_old: float = 0.0
    result_match_rate_old: float = 0.0


class PromptRegistry:
    """读写单 Prompt 单 YAML 文件的本地注册中心。"""

    def __init__(self, registry_dir: Path) -> None:
        """输入：Git 跟踪的 YAML 目录 ``registry_dir``。

        输出：初始化后的注册中心；目录不存在时创建。
        功能：保存 Prompt 文件的统一位置，供运行时和 CI 使用相同版本来源。
        """
        self.registry_dir = registry_dir
        self.registry_dir.mkdir(parents=True, exist_ok=True)

    def save_prompt(self, prompt: PromptVersion) -> None:
        """输入：待保存的 ``prompt`` 版本记录。

        输出：写入或更新对应 YAML 文件；文件系统写入失败时抛出 ``OSError``。
        功能：以稳定、可审查的 UTF-8 YAML 格式持久化版本元数据和完整模板。
        """
        self._validate_identity(prompt.prompt_id, prompt.version)
        self._path(prompt.prompt_id, prompt.version).write_text(
            yaml.safe_dump(
                asdict(prompt),
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            ),
            encoding="utf-8",
        )

    def load_prompt(self, prompt_id: str, version: str) -> PromptVersion:
        """输入：Prompt 标识 ``prompt_id`` 和版本号 ``version``。

        输出：解析后的 ``PromptVersion``；文件缺失或 YAML 字段无效时抛出异常。
        功能：从唯一版本文件恢复可用于运行时或评估的完整 Prompt。
        """
        self._validate_identity(prompt_id, version)
        path = self._path(prompt_id, version)
        if not path.is_file():
            raise FileNotFoundError(f"Prompt {prompt_id} {version} 不存在")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Prompt {path} 必须是对象")
        try:
            prompt = PromptVersion(**data)
        except TypeError as exc:
            raise ValueError(f"Prompt {path} 字段无效") from exc
        if prompt.prompt_id != prompt_id or prompt.version != version:
            raise ValueError(f"Prompt {path} 的文件名与内容不一致")
        return prompt

    def list_versions(self, prompt_id: str) -> list[str]:
        """输入：Prompt 标识 ``prompt_id``。

        输出：按数字版本顺序排列的版本号列表。
        功能：发现可比较的 Git 版本，避免字符串排序把 ``v1.10`` 排在 ``v1.2`` 前面。
        """
        if not prompt_id or "/" in prompt_id or "\\" in prompt_id:
            raise ValueError("prompt_id 不能为空且不能包含路径分隔符")
        versions = [
            path.stem.removeprefix(f"{prompt_id}_")
            for path in self.registry_dir.glob(f"{prompt_id}_v*.yaml")
        ]
        return sorted(versions, key=self._version_key)

    def load_latest(self, prompt_id: str) -> PromptVersion:
        """输入：Prompt 标识 ``prompt_id``。

        输出：数字版本最高的 ``PromptVersion``；没有版本时抛出 ``FileNotFoundError``。
        功能：让运行时和 CI 统一选择 Git 中最新的已登记 Prompt。
        """
        versions = self.list_versions(prompt_id)
        if not versions:
            raise FileNotFoundError(f"Prompt {prompt_id} 没有已登记版本")
        return self.load_prompt(prompt_id, versions[-1])

    @staticmethod
    def _validate_identity(prompt_id: str, version: str) -> None:
        """输入：Prompt 标识 ``prompt_id`` 和版本号 ``version``。

        输出：无显式返回值；非法标识时抛出 ``ValueError``。
        功能：阻止版本文件路径穿越，并约束比较所需的 ``v数字`` 版本格式。
        """
        if not prompt_id or "/" in prompt_id or "\\" in prompt_id:
            raise ValueError("prompt_id 不能为空且不能包含路径分隔符")
        if not re.fullmatch(r"v\d+(?:\.\d+)*", version):
            raise ValueError("version 必须为 v1 或 v1.2 格式")

    def _path(self, prompt_id: str, version: str) -> Path:
        """输入：已校验的 Prompt 标识 ``prompt_id`` 和版本号 ``version``。

        输出：对应 YAML 文件的绝对或相对 ``Path``。
        功能：集中生成单版本单文件的稳定命名规则。
        """
        return self.registry_dir / f"{prompt_id}_{version}.yaml"

    @staticmethod
    def _version_key(version: str) -> tuple[int, ...]:
        """输入：格式为 ``v数字(.数字)*`` 的 ``version``。

        输出：可用于 Python 排序的整数元组。
        功能：按语义数字而非字典序排列 Prompt 版本。
        """
        PromptRegistry._validate_identity("prompt", version)
        return tuple(int(part) for part in version[1:].split("."))


def default_registry() -> PromptRegistry:
    """输入：无显式参数；读取 NL2SQL 模块内的 ``prompts`` 目录。

    输出：项目内 NL2SQL Prompt 的 ``PromptRegistry``。
    功能：提供运行时和 CI 共用的默认 Git 注册表位置。
    """
    return PromptRegistry(Path(__file__).resolve().parent / "prompts")


def load_golden_dataset(path: Path) -> list[GoldenCase]:
    """输入：脱敏生产对话导出的 JSON 文件 ``path``。

    输出：按文件顺序解析的 ``GoldenCase`` 列表；格式非法时抛出 ``ValueError``。
    功能：加载评估所需的自然语言、期望 SQL 或结果签名和可追溯会话标识。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Golden Dataset {path} 不是合法 JSON") from exc
    if not isinstance(data, list):
        raise ValueError("Golden Dataset 顶层必须是数组")
    try:
        cases = [GoldenCase(**case) for case in data]
    except (TypeError, ValueError) as exc:
        raise ValueError("Golden Dataset 用例字段无效") from exc
    if not cases:
        raise ValueError("Golden Dataset 不能为空")
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("Golden Dataset 的 case_id 必须唯一")
    for case in cases:
        if not case.case_id.strip() or not case.natural_language.strip():
            raise ValueError("Golden Dataset 必须包含 case_id 和 natural_language")
        if not case.source_conversation_id.strip():
            raise ValueError("Golden Dataset 必须保留 source_conversation_id")
        if case.expected_sql is None and case.expected_result_signature is None:
            raise ValueError("Golden Dataset 必须包含 expected_sql 或 expected_result_signature")
        if case.difficulty not in {"easy", "medium", "hard"}:
            raise ValueError("Golden Dataset difficulty 必须为 easy、medium 或 hard")
    return cases


class NL2SQLEvaluator:
    """复用生产 SQL 生成、校验和执行链路的同步 A/B 评估器。"""

    def __init__(
        self,
        generator: SQLGenerator,
        validator: Any,
        executor: Any,
        repeat_per_case: int = 3,
    ) -> None:
        """输入：生产生成器、SQL 校验器、只读执行器和每用例重复次数。

        输出：初始化后的评估器；重复次数小于一时抛出 ``ValueError``。
        功能：保存与线上一致的 SQL 链路，以重复调用和多数投票降低模型随机性。
        """
        if repeat_per_case < 1:
            raise ValueError("repeat_per_case 必须大于或等于1")
        self.generator = generator
        self.validator = validator
        self.executor = executor
        self.repeat_per_case = repeat_per_case

    def evaluate(
        self,
        old_prompt: PromptVersion,
        new_prompt: PromptVersion,
        test_cases: list[GoldenCase],
    ) -> EvalReport:
        """输入：同一 Prompt 的新旧版本和 Golden Dataset 用例列表。

        输出：含逐用例得分、成本、延迟和胜者的 ``EvalReport``。
        功能：对每条用例分别重复生成、投票、经线上 SQL 链路评分，并按 3% 阈值判断版本优劣。
        """
        if old_prompt.prompt_id != new_prompt.prompt_id:
            raise ValueError("只能比较同一 prompt_id 的版本")
        results: list[dict[str, Any]] = []
        old_runs_all: list[dict[str, Any]] = []
        new_runs_all: list[dict[str, Any]] = []
        for case in test_cases:
            old_runs = self._run_multiple(old_prompt.content, case.natural_language)
            new_runs = self._run_multiple(new_prompt.content, case.natural_language)
            old_score = self._score_sql(self._majority_vote(old_runs), case, old_runs)
            new_score = self._score_sql(self._majority_vote(new_runs), case, new_runs)
            results.append(
                {
                    "case_id": case.case_id,
                    "query": case.natural_language,
                    "difficulty": case.difficulty,
                    "old_sql": old_score["sql"],
                    "new_sql": new_score["sql"],
                    "old_score": old_score,
                    "new_score": new_score,
                    "winner": (
                        "new" if new_score["total"] > old_score["total"] else
                        "old" if old_score["total"] > new_score["total"] else "tie"
                    ),
                }
            )
            old_runs_all.extend(old_runs)
            new_runs_all.extend(new_runs)

        old_average = self._average([item["old_score"]["total"] for item in results])
        new_average = self._average([item["new_score"]["total"] for item in results])
        return EvalReport(
            prompt_id=old_prompt.prompt_id,
            old_version=old_prompt.version,
            new_version=new_prompt.version,
            total_cases=len(test_cases),
            results=results,
            sql_syntax_pass_rate=self._rate(results, "new_score", "syntax_pass"),
            result_match_rate=self._rate(results, "new_score", "result_score"),
            avg_tokens_old=int(self._average([run["tokens"] for run in old_runs_all])),
            avg_tokens_new=int(self._average([run["tokens"] for run in new_runs_all])),
            avg_latency_ms_old=self._average([run["latency_ms"] for run in old_runs_all]),
            avg_latency_ms_new=self._average([run["latency_ms"] for run in new_runs_all]),
            winner=self._case_winner(old_average, new_average),
            sql_syntax_pass_rate_old=self._rate(results, "old_score", "syntax_pass"),
            result_match_rate_old=self._rate(results, "old_score", "result_score"),
        )

    def _run_multiple(self, prompt: str, query: str) -> list[dict[str, Any]]:
        """输入：完整 System Prompt ``prompt`` 和用户问题 ``query``。

        输出：重复生成的 SQL、估算 Token 数和延迟字典列表。
        功能：以相同输入调用生成器多次，为多数投票收集降低随机噪声的样本。
        """
        runs = []
        for _ in range(self.repeat_per_case):
            started_at = time.perf_counter()
            response = self.generator(prompt, query)
            runs.append(
                {
                    "sql": response.strip(),
                    "latency_ms": (time.perf_counter() - started_at) * 1000,
                    "tokens": (len(prompt) + len(query) + len(response)) // 4,
                }
            )
        return runs

    def _majority_vote(self, runs: list[dict[str, Any]]) -> str:
        """输入：同一 Prompt 和问题的重复生成 ``runs``。

        输出：出现次数最多且已规范化的 SQL 字符串。
        功能：消除格式差异后选择多数结果，降低非零 temperature 的偶发输出影响。
        """
        if not runs:
            raise ValueError("runs 不能为空")
        return Counter(self._normalize_sql(run["sql"]) for run in runs).most_common(1)[0][0]

    def _score_sql(
        self,
        sql: str,
        case: GoldenCase,
        runs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """输入：投票后的 ``sql``、Golden 用例 ``case`` 和该 SQL 的重复运行数据。

        输出：语法、结果、延迟和综合得分字典。
        功能：复用 SQL 沙箱和 EXPLAIN 执行结果评估硬指标，再以期望 SQL 或结果签名衡量正确性。
        """
        passed, fixed_sql, _error = self.validator.validate_and_fix(sql)
        execution: dict[str, Any] | None = None
        if passed:
            try:
                execution = self.executor.run(fixed_sql)
            except Exception:
                passed = False
        result_match = False
        if passed and execution is not None:
            if case.expected_result_signature is not None:
                result_score = self._signature_similarity(
                    self._extract_signature(execution.get("rows", [])),
                    case.expected_result_signature,
                )
                result_match = result_score == 1.0
            elif case.expected_sql is not None:
                expected_passed, expected_sql, _error = self.validator.validate_and_fix(
                    case.expected_sql
                )
                result_match = expected_passed and (
                    self._normalize_sql(fixed_sql) == self._normalize_sql(expected_sql)
                )
                result_score = float(result_match)
            else:
                result_score = 0.0
        else:
            result_score = 0.0
        latency_score = max(0.0, 1.0 - self._average([run["latency_ms"] for run in runs]) / 5000)
        return {
            "sql": fixed_sql,
            "syntax_pass": passed,
            "syntax_score": float(passed),
            "result_match": result_match,
            "result_score": result_score,
            "latency_score": latency_score,
            "total": float(passed) * 0.4 + result_score * 0.5 + latency_score * 0.1,
        }

    @staticmethod
    def _normalize_sql(sql: str) -> str:
        """输入：候选或期望 SQL 文本 ``sql``。

        输出：折叠空白、统一大小写并忽略 LIMIT 数字的 SQL。
        功能：让多数投票和期望 SQL 对比聚焦查询语义，排除展示格式和行数上限差异。
        """
        normalized = re.sub(r"\s+", " ", sql).strip().upper().rstrip(";")
        return re.sub(r"\bLIMIT\s+\d+", "LIMIT N", normalized)

    @staticmethod
    def _extract_signature(rows: list[dict[str, Any]]) -> dict[str, Any]:
        """输入：SQL 查询得到的字典行列表 ``rows``。

        输出：包含行数、列名和首行样本的结果特征字典。
        功能：将查询结果压缩为不含完整业务数据的可比较签名。
        """
        if not rows:
            return {"row_count": 0, "columns": [], "first_row_sample": {}}
        return {
            "row_count": len(rows),
            "columns": list(rows[0]),
            "first_row_sample": {key: str(value)[:50] for key, value in rows[0].items()},
        }

    @staticmethod
    def _signature_similarity(actual: dict[str, Any], expected: dict[str, Any]) -> float:
        """输入：实际查询结果签名 ``actual`` 和 Golden 期望签名 ``expected``。

        输出：范围为 0 至 1 的匹配分数。
        功能：分别评估最小行数与关键列，作为没有固定期望 SQL 时的结果正确性指标。
        """
        score = 0.0
        if actual["row_count"] >= expected.get("row_count_min", 0):
            score += 0.4
        key_columns = expected.get("key_columns", [])
        if key_columns and all(column in actual["columns"] for column in key_columns):
            score += 0.6
        return score

    @staticmethod
    def _average(values: list[float | int]) -> float:
        """输入：数值列表 ``values``。

        输出：平均值；空列表返回 ``0.0``。
        功能：统一处理空 Golden Dataset 与运行指标的安全聚合。
        """
        return sum(values) / len(values) if values else 0.0

    @staticmethod
    def _rate(
        results: list[dict[str, Any]],
        score_key: str,
        metric: str,
    ) -> float:
        """输入：逐用例结果、得分字段名和布尔指标名。

        输出：指定指标的平均值；空结果返回 ``0.0``。
        功能：从新旧版本明细计算 SQL 语法通过率和结果匹配率。
        """
        return NL2SQLEvaluator._average(
            [float(result[score_key][metric]) for result in results]
        )

    @staticmethod
    def _case_winner(old_score: float, new_score: float) -> str:
        """输入：旧版 ``old_score`` 和新版 ``new_score`` 综合分。

        输出：``old``、``new`` 或 ``tie``。
        功能：以超过 3% 的相对优势判定胜者，避免将模型随机波动当成 Prompt 改进。
        """
        if new_score > old_score * 1.03:
            return "new"
        if new_score < old_score * 0.97:
            return "old"
        return "tie"


def evaluation_passed(report: EvalReport) -> bool:
    """输入：新旧 Prompt 的 A/B 评估 ``report``。

    输出：新版可自动通过 CI 时返回 ``True``。
    功能：要求新版综合分胜出、至少一项 SQL 准确指标提升，并禁止语法或结果匹配率退化。
    """
    return (
        report.winner == "new"
        and report.sql_syntax_pass_rate >= report.sql_syntax_pass_rate_old
        and report.result_match_rate >= report.result_match_rate_old
        and (
            report.sql_syntax_pass_rate > report.sql_syntax_pass_rate_old
            or report.result_match_rate > report.result_match_rate_old
        )
    )
