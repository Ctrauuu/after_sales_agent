"""串联 Prompt、SQL 校验、RBAC 和只读执行。"""

from collections.abc import Callable
from typing import Any, Mapping

from sqlalchemy.exc import SQLAlchemyError

from nl2sql.executor import ReadOnlyExecutor
from nl2sql.prompt_registry import default_registry
from nl2sql.schema_registry import build_schema_prompt
from nl2sql.sql_sandbox import SQLValidator


SQLGenerator = Callable[[str, str], str]


class NL2SQLPipeline:
    def __init__(
        self,
        generator: SQLGenerator,
        validator: SQLValidator,
        executor: ReadOnlyExecutor,
        max_attempts: int = 2,
        prompt_version: str = "v1.0",
    ) -> None:
        """输入：SQL 生成器、校验器、只读执行器、最大尝试次数和已部署 Prompt 版本。

        输出：初始化后的 NL2SQL 管道实例状态。
        功能：组合生成、校验和执行组件，校验重试次数，并固定运行时使用的已评估 Prompt 版本。
        """
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于或等于1")
        self.generator = generator
        self.validator = validator
        self.executor = executor
        self.max_attempts = max_attempts
        self.prompt_version = prompt_version.strip()
        if not self.prompt_version:
            raise ValueError("prompt_version 不能为空")

    def run(
        self,
        question: str,
        safe_filters: Mapping[str, Any],
    ) -> dict[str, Any]:
        """输入：自然语言问题 ``question`` 和授权后的 ``safe_filters``。

        输出：包含最终 SQL、EXPLAIN 和查询行的结果字典。
        功能：生成 SQL、校验修正、注入 RBAC、执行，并在校验或数据库失败时有限重试。
        """
        question = question.strip()
        if not question:
            raise ValueError("question 不能为空")

        system_prompt = build_schema_prompt(
            default_registry().load_prompt("nl2sql", self.prompt_version).content
        )
        previous_error = ""
        for _attempt in range(self.max_attempts):
            user_prompt = question
            if previous_error:
                user_prompt += f"\n\n上一条 SQL 失败，请修正后重新生成。错误：{previous_error}"
            sql = self.generator(system_prompt, user_prompt)
            passed, sql, error = self.validator.validate_and_fix(sql)
            if not passed:
                previous_error = error or "SQL校验失败"
                continue

            sql, params = self.validator.inject_rbac(sql, safe_filters)
            try:
                execution = self.executor.run(sql, params)
            except SQLAlchemyError as exc:
                previous_error = f"数据库校验失败：{getattr(exc, 'orig', exc)}"
                continue
            return {"sql": sql, **execution}

        raise ValueError(f"无法生成安全SQL：{previous_error}")
