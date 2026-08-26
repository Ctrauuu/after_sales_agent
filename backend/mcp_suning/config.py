"""集中读取 MCP 数据库、缓存和桥接鉴权环境变量。"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str
    mysql_database: str

    # Redis 只用于一次性身份凭证防重放，不缓存角色策略。
    redis_url: str = ""

    # 管理后台只读 Hermes 状态并写现有 Skill Hub；令牌为空时仅允许环回访问。
    suning_admin_api_token: str = ""
    suning_admin_mysql_user: str = ""
    suning_admin_mysql_password: str = ""
    hermes_home: str = "~/.hermes"
    skill_evolution_dir: str = ""

    # Hermes 插件与 MCP 之间的身份凭证配置
    suning_mcp_bridge_secret: str = ""
    suning_identity_issuer: str = ""
    suning_authn_require_redis: bool = True
    # 仅 Hermes 调度器可使用的内部服务主体；为空时 Cron 调用默认拒绝。
    suning_cron_service_subject: str = ""

    # 订单全链路聚合服务通过私有 MCP 并发调用下游服务；与 Hermes 插件的地址可独立部署。
    suning_mcp_order_url: str = "http://127.0.0.1:8101/mcp"
    suning_mcp_aftersale_url: str = "http://127.0.0.1:8102/mcp"
    suning_mcp_logistics_url: str = "http://127.0.0.1:8104/mcp"
    suning_mcp_payment_url: str = "http://127.0.0.1:8105/mcp"
    order_timeline_timeout_seconds: float = 10.0

    # OTel Collector 地址。留空时仅保留 Hermes 结构化日志，不阻断业务 MCP 调用。
    otel_exporter_otlp_traces_endpoint: str = ""

    # NL2SQL 通过公共 LangChain 工厂创建 DeepSeek 模型。API Key 为空时服务仍可
    # 启动，但调用自然语言分析工具会返回明确的配置错误。
    deepseek_api: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_lite_model: str = "deepseek-v4-flash"
    nl2sql_timeout_seconds: float = 30.0
    nl2sql_prompt_version: str = "v1.0"

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


# 字段由 BaseSettings 从 .env 读取，Pylance 无法静态识别
settings = Settings()  # pyright: ignore[reportCallIssue]
