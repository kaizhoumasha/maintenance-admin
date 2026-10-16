from fastapi import Depends, FastAPI

from app.controllers.audit import router as audit_log_router
from app.controllers.auth import router as auth_router
from app.controllers.maintenance import router as maintenance_router
from app.controllers.permissions import router as permissions_router
from app.controllers.roles import router as roles_router
from app.controllers.users import router as users_router
from app.core import get_settings, register_exception_handlers, setup_logging
from app.core.database import session_factory
from app.core.ratelimit import RateLimit
from app.middlewares import AuditMiddleware, trace_id_middleware

settings = get_settings()

# 非本地环境隐藏 API 文档
app = FastAPI(
    title="maintenance-admin",
    description="管理系统后端",
    version="0.1.0",
    # 默认 100 次/分钟/IP/路由,路由可用同 name 的 RateLimit 覆盖
    dependencies=[Depends(RateLimit(100))],
    # 刷新页面后保留已填的 token
    swagger_ui_parameters={"persistAuthorization": True},
    docs_url="/docs" if settings.ENVIRONMENT == "local" else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.ENVIRONMENT == "local" else None,
)
app.middleware("http")(trace_id_middleware)
app.add_middleware(AuditMiddleware)  # 后添加 = 最外层,能看到最终状态码
app.state.audit_session_factory = session_factory
register_exception_handlers(app)
setup_logging()
app.include_router(users_router, prefix="/api/v1")
app.include_router(audit_log_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(roles_router, prefix="/api/v1")
app.include_router(permissions_router, prefix="/api/v1")
app.include_router(maintenance_router, prefix="/api/v1")


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
