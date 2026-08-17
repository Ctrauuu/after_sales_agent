"""集中管理管理 API 使用的可替换数据源和运行时路径。"""

from pathlib import Path
from zoneinfo import ZoneInfo

from mcp_suning.config import settings
from mcp_suning.database import engine


PROJECT_ROOT = Path(__file__).resolve().parents[4]
HERMES_HOME = Path(settings.hermes_home).expanduser()
SKILLS_DIR = Path(
    settings.skill_evolution_dir.strip()
    or PROJECT_ROOT / ".hermes" / "skills" / "evolved"
).expanduser()
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
