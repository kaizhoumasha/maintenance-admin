from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 项目名:OpenAPI 标题、Celery 应用名、日志器名都取它;新项目改这里(和 pyproject.toml)
    APP_NAME: str = "enterprise-backend"
    ENVIRONMENT: str = "local"
    DATABASE_URL: str
    REDIS_URL: str

    JWT_SECRET_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    AUDIT_RETENTION_DAYS: int = 90

    # 逗号分隔的前端源(如 http://localhost:5173);为空则不启用 CORS(同域部署,nginx 反代)
    CORS_ORIGINS: str = ""

    @model_validator(mode="after")
    def _check_jwt_secret(self) -> "Settings":
        if self.ENVIRONMENT != "local" and len(self.JWT_SECRET_KEY) < 32:
            raise ValueError("JWT_SECRET_KEY 在非 local 环境至少 32 个字符(openssl rand -hex 32)")
        return self

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def REFRESH_TOKEN_EXPIRE_SECONDS(self) -> int:
        return self.REFRESH_TOKEN_EXPIRE_DAYS * 86400


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue] -- 必填项由环境变量注入
