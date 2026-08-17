"""保留管理后台原有 Uvicorn 导入路径的兼容入口。"""

from .admin import app, create_app

__all__ = ["app", "create_app"]
