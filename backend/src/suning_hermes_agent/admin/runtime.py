"""集中管理管理 API 使用的可替换数据源和运行时路径。"""

from pathlib import Path
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from mcp_suning.config import settings


PROJECT_ROOT = Path(__file__).resolve().parents[4]
engine = sa.create_engine(
    sa.URL.create(
        drivername="mysql+pymysql",
        username=settings.suning_admin_mysql_user or settings.mysql_user,
        password=settings.suning_admin_mysql_password or settings.mysql_password,
        host=settings.mysql_host,
        port=settings.mysql_port,
        database=settings.mysql_database,
        query={"charset": "utf8mb4"},
    ),
    pool_pre_ping=True,
    pool_recycle=3600,
)
HERMES_HOME = Path(settings.hermes_home).expanduser()
SKILLS_DIR = Path(
    settings.skill_evolution_dir.strip()
    or PROJECT_ROOT / ".hermes" / "skills" / "evolved"
).expanduser()
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
