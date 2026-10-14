from fastapi import FastAPI

from app.controllers.users import router as users_router
from app.core import get_settings, register_exception_handlers, setup_logging
from app.middlewares import trace_id_middleware

settings = get_settings()

# 非本地环境隐藏 API 文档
app = FastAPI(
    title="maintenance-admin",
    docs_url="/docs" if settings.ENVIRONMENT == "local" else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.ENVIRONMENT == "local" else None,
)
app.middleware("http")(trace_id_middleware)
register_exception_handlers(app)
setup_logging()
app.include_router(users_router, prefix="/api/v1")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
