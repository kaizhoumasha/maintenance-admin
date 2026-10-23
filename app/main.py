from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.controllers.audit import router as audit_log_router
from app.controllers.auth import router as auth_router
from app.controllers.dev import include_dev_routers
from app.controllers.health import router as health_router
from app.controllers.maintenance import router as maintenance_router
from app.controllers.menus import router as menus_router
from app.controllers.permissions import router as permissions_router
from app.controllers.roles import router as roles_router
from app.controllers.users import router as users_router
from app.controllers.wms_events import include_wms_events
from app.core import error_responses, get_settings, register_exception_handlers, setup_logging
from app.core.database import engine, session_factory
from app.core.ratelimit import RateLimit
from app.core.redis import get_redis
from app.core.routing import drop_422
from app.integrations.wms_request import ADAPTERS as WMS_REQUEST_ADAPTERS
from app.integrations.wms_transport import ADAPTERS as WMS_TRANSPORT_ADAPTERS
from app.middlewares import AuditMiddleware, trace_id_middleware
from app.reliable.config import parse_reliable_config
from app.reliable_runtime import reliable_lifespan

settings = get_settings()


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    try:
        async with reliable_lifespan(
            parse_reliable_config(settings), getattr(application.state, "reliable_adapters", {})
        ) as outbox:
            application.state.outbox = outbox
            yield
    finally:
        application.state.outbox = None
        await engine.dispose()
        await get_redis().aclose()
        get_redis.cache_clear()


# 非本地环境隐藏 API 文档
app = FastAPI(
    lifespan=lifespan,
    title=settings.APP_NAME,
    description=(
        "通用企业级后端管理平台模板，适用于 WMS、MES、WES 等业务开发，"
        "提供第三方与内部 API 对接基础能力"
    ),
    version="0.2.0",
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
# 协议适配器按协议名注册给可靠发送器;是否真正发送由 RELIABLE_TARGETS 是否配置对应目标决定。
app.state.reliable_adapters = {
    **getattr(app.state, "reliable_adapters", {}),
    **WMS_TRANSPORT_ADAPTERS,
    **WMS_REQUEST_ADAPTERS,
}
register_exception_handlers(app)
setup_logging()
app.include_router(users_router, prefix="/api/v1")
app.include_router(audit_log_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(roles_router, prefix="/api/v1")
app.include_router(permissions_router, prefix="/api/v1")
app.include_router(maintenance_router, prefix="/api/v1")
app.include_router(menus_router, prefix="/api/v1")
app.include_router(health_router)
include_wms_events(app, environment=settings.ENVIRONMENT)
include_dev_routers(app, environment=settings.ENVIRONMENT)

_openapi = app.openapi
app.openapi = lambda: drop_422(_openapi())  # type: ignore[method-assign]


@app.get("/health", tags=["health"], responses=error_responses(429))
async def health() -> dict[str, str]:
    return {"status": "ok"}


if settings.cors_origins:
    # 包住包含 ServerErrorMiddleware 的完整栈,让兜底 500 也带 CORS 头。
    app.middleware_stack = CORSMiddleware(
        app.build_middleware_stack(),
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Trace-Id", "Retry-After"],
    )
