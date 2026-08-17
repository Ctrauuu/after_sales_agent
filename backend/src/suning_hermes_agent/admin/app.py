"""组装管理后台各业务域路由和统一认证。"""

from fastapi import FastAPI

from mcp_suning.config import settings

from .common import AdminAuthMiddleware, ApiError, api_error_handler
from .conversations import router as conversations_router
from .dashboard import router as dashboard_router
from .mcp import router as mcp_router
from .skills import router as skills_router
from .users import router as users_router


def create_app(admin_token: str | None = None) -> FastAPI:
    """输入：可选管理 Bearer Token ``admin_token``；默认读取环境配置。

    输出：挂载全部业务域路由和统一认证的 FastAPI 应用。
    功能：组装管理服务，同时保留测试和 Uvicorn 启动入口。
    """

    token = settings.suning_admin_api_token if admin_token is None else admin_token
    admin_app = FastAPI(
        title="Suning Hermes 管理后台 API",
        docs_url="/api/admin/docs",
        openapi_url="/api/admin/openapi.json",
    )
    admin_app.add_middleware(AdminAuthMiddleware, token=token)
    admin_app.add_exception_handler(ApiError, api_error_handler)
    for router in (
        users_router,
        mcp_router,
        conversations_router,
        skills_router,
        dashboard_router,
    ):
        admin_app.include_router(router)
    return admin_app


app = create_app()
