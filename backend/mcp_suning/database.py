"""创建供全部业务 MCP 共用的 SQLAlchemy MySQL 连接池。"""

import sqlalchemy as sa

from mcp_suning.config import settings


database_url = sa.URL.create(
    drivername="mysql+pymysql",
    username=settings.mysql_user,
    password=settings.mysql_password,
    host=settings.mysql_host,
    port=settings.mysql_port,
    database=settings.mysql_database,
    query={"charset": "utf8mb4"},
)

engine = sa.create_engine(
    database_url,
    pool_pre_ping=True,
    pool_recycle=3600,
)
