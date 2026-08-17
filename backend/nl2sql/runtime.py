"""创建所有动态分析 MCP Tool 共用的 NL2SQL Pipeline。"""

from mcp_suning.config import settings
from mcp_suning.database import engine
from nl2sql.executor import ReadOnlyExecutor
from nl2sql.generator import DeepSeekSQLGenerator
from nl2sql.pipeline import NL2SQLPipeline
from nl2sql.sql_sandbox import SQLValidator


nl2sql_pipeline = NL2SQLPipeline(
    generator=DeepSeekSQLGenerator(
        api_key=settings.deepseek_api,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout_seconds=settings.nl2sql_timeout_seconds,
    ),
    validator=SQLValidator(),
    executor=ReadOnlyExecutor(engine),
    max_attempts=2,
    prompt_version=settings.nl2sql_prompt_version,
)

nl2sql_lite_pipeline = NL2SQLPipeline(
    generator=DeepSeekSQLGenerator(
        api_key=settings.deepseek_api,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_lite_model,
        timeout_seconds=settings.nl2sql_timeout_seconds,
    ),
    validator=SQLValidator(),
    executor=ReadOnlyExecutor(engine),
    max_attempts=2,
    prompt_version=settings.nl2sql_prompt_version,
)


__all__ = ["nl2sql_lite_pipeline", "nl2sql_pipeline"]
