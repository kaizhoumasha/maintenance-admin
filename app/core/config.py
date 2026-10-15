from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ENVIRONMENT: str = "local"
    DATABASE_URL: str
    REDIS_URL: str

    JWT_SECRET_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    @property
    def REFRESH_TOKEN_EXPIRE_SECONDS(self) -> int:
        return self.REFRESH_TOKEN_EXPIRE_DAYS * 86400


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue] -- 必填项由环境变量注入
