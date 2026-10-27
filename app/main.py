from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.controllers.audit import router as audit_log_router
from app.controllers.auth import router as auth_router
from app.controllers.dev import include_dev_routers
from app.controllers.devices import router as devices_router
from app.controllers.ecs_callback import include_ecs_callback
from app.controllers.ecs_result import router as ecs_result_router
from app.controllers.ecs_status import router as ecs_status_router
from app.controllers.ecs_status import status_capability
from app.controllers.health import router as health_router
from app.controllers.maintenance import router as maintenance_router
from app.controllers.menus import router as menus_router
from app.controllers.permissions import router as permissions_router
from app.controllers.roles import router as roles_router
from app.controllers.users import router as users_router
from app.controllers.wms_events import include_wms_events
from app.controllers.worklines import router as worklines_router
from app.core import error_responses, get_settings, register_exception_handlers, setup_logging
from app.core.database import engine, session_factory
from app.core.ratelimit import RateLimit
from app.core.redis import get_redis
from app.core.routing import drop_422
from app.integrations.ecs_command.dev import configure_local_ecs
from app.integrations.ecs_command.runtime import ecs_runtime_config
from app.integrations.wms_request import ADAPTERS as WMS_REQUEST_ADAPTERS
from app.integrations.wms_transport import ADAPTERS as WMS_TRANSPORT_ADAPTERS
from app.middlewares import AuditMiddleware, trace_id_middleware
from app.reliable_runtime import reliable_lifespan
from app.services.device_bindings import resolve_binding
from app.services.device_routes import DeviceRoutes
from app.services.device_status_service import DeviceStatusService

settings = get_settings()


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    try:
        ecs_commands = getattr(application.state, "ecs_commands", None)
        if ecs_commands is None:
            ecs_commands = configure_local_ecs(
                application,
                environment=settings.ENVIRONMENT,
                routes=DeviceRoutes() if settings.ENVIRONMENT == "local" else None,
            )
        application.state.device_status = DeviceStatusService(session_factory, get_redis())
        async with reliable_lifespan(
            ecs_runtime_config(settings, ecs_commands),
            getattr(application.state, "reliable_adapters", {}),
            lifecycles=getattr(application.state, "reliable_lifecycles", {}),
            client_provider=getattr(application.state, "reliable_client_provider", None),
        ) as outbox:
            application.state.outbox = outbox
            if ecs_commands is not None:
                ecs_commands.outbox = outbox
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
app.state.integration_capabilities = [status_capability()]
# WMS 目标由 RELIABLE_TARGETS 配置;local ECS 自动装配通用合同并加入同一发送器。
app.state.reliable_adapters = {**WMS_TRANSPORT_ADAPTERS, **WMS_REQUEST_ADAPTERS}
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
app.include_router(ecs_result_router, prefix="/api/v1")
app.include_router(ecs_status_router, prefix="/api/v1")
app.include_router(devices_router, prefix="/api/v1")
app.include_router(worklines_router, prefix="/api/v1")
include_wms_events(app, environment=settings.ENVIRONMENT)
include_ecs_callback(app, environment=settings.ENVIRONMENT, resolve_binding=resolve_binding)
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
