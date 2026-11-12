from functools import lru_cache
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PositiveProtocolInt = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
_ArmTimeout = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]


class AutoWorklineSettings(BaseModel):
    work_line_id: _PositiveProtocolInt
    work_line_version: _PositiveProtocolInt
    rack_arm_timeout_ms: _ArmTimeout = 0
    bin_arm_timeout_ms: _ArmTimeout = 0
    source_locations: tuple[tuple[str, Literal["STATION_A", "STATION_B"]], ...]
    transport_station_id: Annotated[str, Field(min_length=1, pattern=r"^[^\x00]+$")] | None = None


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
    CALLBACK_SNAPSHOT_RETENTION_DAYS: int = Field(default=7, gt=0)
    INTEGRATION_HISTORY_RETENTION_DAYS: int = Field(default=90, gt=0)

    # Transport 任务创建后超过该时长仍无确定终态就输出卡住告警(暂定 2 小时,按现场 SOP 调整)
    TRANSPORT_STUCK_ALERT_MINUTES: int = Field(default=120, gt=0)

    SMT_AUTO_FIRST_PLAN_WAIT_MS: int | None = Field(default=None, gt=0)
    SMT_AUTO_WORKLINES: tuple[AutoWorklineSettings, ...] = ()

    SMT_MANUAL_FIRST_PLAN_WAIT_MS: int | None = Field(default=None, gt=0)
    COARSE_SORTING_TAIL_ENABLED: bool = False
    COARSE_SORTING_OUTPUT_LOCATIONS: dict[str, str] = Field(default_factory=dict)
    COARSE_SORTING_NG_LOCATIONS: dict[str, str] = Field(default_factory=dict)
    COARSE_SORTING_WMS_MOCK: dict = Field(default_factory=dict)
    XRAY_TRANSIT_TARGETS: dict[str, dict[str, dict]] = Field(default_factory=dict)
    XRAY_RETURN_RACKS: dict[str, dict] = Field(default_factory=dict)
    XRAY_WMS_MOCK_TARGETS: dict[str, dict[str, dict]] = Field(default_factory=dict)

    @field_validator("SMT_MANUAL_FIRST_PLAN_WAIT_MS", "SMT_AUTO_FIRST_PLAN_WAIT_MS", mode="before")
    @classmethod
    def _first_plan_wait(cls, value):
        if value is not None and type(value) not in {int, str}:
            raise ValueError("first plan wait must be integer milliseconds")
        return value

    RELIABLE_TARGETS: dict = {}
    RELIABLE_MAX_BODY_BYTES: int = 262144
    RELIABLE_DEFAULT_TIMEOUT_MS: float = 10000
    RELIABLE_RETENTION_DAYS: int = 90
    RELIABLE_MAX_RETRY_AFTER_S: float = 3600
    RELIABLE_MAX_CONCURRENCY: int = 4
    RELIABLE_CLAIM_BATCH_SIZE: int = 4
    RELIABLE_DB_POOL_SIZE: int = 2

    @field_validator("*", mode="before")
    @classmethod
    def _reject_reliable_boolean(cls, value, info):
        if info.field_name.startswith(("RELIABLE_", "CALLBACK_")) and isinstance(value, bool):
            raise ValueError("numeric settings cannot be booleans")
        return value

    # 逗号分隔的前端源(如 http://localhost:5173);为空则不启用 CORS(同域部署,nginx 反代)
    CORS_ORIGINS: str = ""

    @model_validator(mode="after")
    def _check_jwt_secret(self) -> "Settings":
        if self.COARSE_SORTING_TAIL_ENABLED and self.ENVIRONMENT != "local":
            raise ValueError("Coarse sorting tail is available only in the local integration mode")
        if self.COARSE_SORTING_WMS_MOCK and not self.COARSE_SORTING_TAIL_ENABLED:
            raise ValueError("Coarse sorting WMS mock requires the local integration tail")
        if self.COARSE_SORTING_WMS_MOCK and len(self.JWT_SECRET_KEY) < 32:
            raise ValueError(
                "Coarse sorting WMS mock requires JWT_SECRET_KEY with at least 32 characters"
            )
        if self.XRAY_WMS_MOCK_TARGETS and self.ENVIRONMENT != "local":
            raise ValueError("X-Ray WMS mock is available only in the local integration mode")
        if self.XRAY_WMS_MOCK_TARGETS and len(self.JWT_SECRET_KEY) < 32:
            raise ValueError("X-Ray WMS mock requires JWT_SECRET_KEY with at least 32 characters")
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
