from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.businesses import catalog
from app.businesses.coarse_sorting.service import read_scan
from app.businesses.runtime import BusinessRuntime, build_runtime
from app.controllers.audit import router as audit_log_router
from app.controllers.auth import router as auth_router
from app.controllers.businesses import router as businesses_router
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
from app.integrations.discovery import load_formal_modules, load_local_modules
from app.integrations.ecs_command.runtime import configure_ecs, ecs_runtime_config
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
    owns_business_runtime = False
    try:
        ecs_commands = getattr(application.state, "ecs_commands", None)
        if ecs_commands is None:
            if settings.ENVIRONMENT == "local":
                ecs_commands = configure_local_ecs(
                    application,
                    environment=settings.ENVIRONMENT,
                    routes=DeviceRoutes(command_bindings, annexes=command_annexes),
                    annexes=command_annexes,
                )
            elif command_bindings or command_annexes:
                ecs_commands = configure_ecs(
                    application,
                    routes=DeviceRoutes(command_bindings, annexes=command_annexes),
                    annexes=command_annexes,
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
            runtime = getattr(application.state, "business_runtime", None)
            if runtime is None:
                runtime = build_runtime(outbox=outbox, ecs_commands=ecs_commands)
                owns_business_runtime = True
            expected = {definition.code: definition for definition in catalog.definitions()}
            if (
                not isinstance(runtime, BusinessRuntime)
                or runtime.definitions != expected
                or runtime.services.keys() != expected.keys()
                or any(service is None for service in runtime.services.values())
            ):
                raise ValueError("Business runtime must contain complete definitions and services")
            application.state.business_runtime = runtime
            yield
    finally:
        if owns_business_runtime:
            application.state.business_runtime = None
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
app.include_router(businesses_router, prefix="/api/v1")
load_formal_modules()
command_bindings, command_annexes = catalog.validate_business_contracts()
load_local_modules(environment=settings.ENVIRONMENT)
from app.controllers.dev import include_dev_routers  # noqa: E402
from app.integrations.ecs_command.dev import configure_local_ecs  # noqa: E402

include_wms_events(app, environment=settings.ENVIRONMENT)
include_ecs_callback(app, environment=settings.ENVIRONMENT, resolve_binding=resolve_binding)
include_dev_routers(app, environment=settings.ENVIRONMENT, read_business=read_scan)

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
