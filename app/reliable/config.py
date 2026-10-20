from collections.abc import Mapping
from dataclasses import dataclass, fields
from math import isfinite
from types import MappingProxyType
from typing import TYPE_CHECKING

from app.http_client import HttpClientConfig

if TYPE_CHECKING:
    from app.core.config import Settings


def positive(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive and finite")


@dataclass(frozen=True)
class TargetConfig:
    name: str
    base_url: str
    protocol: str
    timeout_ms: float = 10000
    alert_after_attempts: int = 8
    retry_base_s: float = 1
    retry_cap_s: float = 300
    max_retry_after_s: float = 3600
    max_body_bytes: int = 262144

    def __post_init__(self):
        if not isinstance(self.protocol, str) or not self.protocol:
            raise ValueError("protocol is required")
        for name in (
            "timeout_ms",
            "alert_after_attempts",
            "retry_base_s",
            "retry_cap_s",
            "max_retry_after_s",
            "max_body_bytes",
        ):
            positive(getattr(self, name), name)
        for name in ("alert_after_attempts", "max_body_bytes"):
            if not isinstance(getattr(self, name), int):
                raise TypeError(f"{name} must be an integer")
        self.http_config()
        if self.base_url.endswith("/"):
            raise ValueError("target base_url must not end with slash")

    def http_config(self) -> HttpClientConfig:
        timeout = self.timeout_ms / 1000
        return HttpClientConfig(
            self.base_url,
            self.name,
            total_timeout_s=timeout,
            connect_timeout_s=min(5, timeout),
            read_timeout_s=min(5, timeout),
            write_timeout_s=min(5, timeout),
            pool_timeout_s=min(5, timeout),
            max_request_body_bytes=self.max_body_bytes,
            max_response_body_bytes=self.max_body_bytes,
        )


@dataclass(frozen=True)
class ReliableConfig:
    targets: Mapping[str, TargetConfig]
    max_concurrency: int = 4
    claim_batch_size: int = 4
    db_pool_size: int = 2
    db_pool_timeout_s: float = 1
    poll_interval_s: float = 1
    lease_margin_s: float = 5
    shutdown_grace_s: float = 15
    retention_days: int = 90

    def __post_init__(self):
        for field in fields(self):
            if field.name != "targets":
                positive(getattr(self, field.name), field.name)
        for name in ("max_concurrency", "claim_batch_size", "db_pool_size", "retention_days"):
            if not isinstance(getattr(self, name), int):
                raise TypeError(f"{name} must be an integer")
        object.__setattr__(self, "targets", MappingProxyType(dict(self.targets)))


def parse_reliable_config(settings: "Settings") -> ReliableConfig:
    raw = settings.RELIABLE_TARGETS
    if set(raw) - {"defaults", "targets"}:
        raise ValueError("unknown reliable target configuration")
    defaults = {
        "timeout_ms": settings.RELIABLE_DEFAULT_TIMEOUT_MS,
        "max_body_bytes": settings.RELIABLE_MAX_BODY_BYTES,
        "max_retry_after_s": settings.RELIABLE_MAX_RETRY_AFTER_S,
    }
    for name, value in defaults.items():
        positive(value, name)
    if not isinstance(raw.get("defaults", {}), dict) or not isinstance(
        raw.get("targets", {}), dict
    ):
        raise ValueError("defaults and targets must be objects")
    defaults.update(raw.get("defaults", {}))
    TargetConfig(
        **(
            {"name": "validation", "base_url": "http://invalid.test", "protocol": "validation"}
            | defaults
        )
    )
    targets = {}
    for name, overrides in raw.get("targets", {}).items():
        if not isinstance(overrides, dict) or "name" in overrides:
            raise ValueError("target must be an object without name override")
        targets[name] = TargetConfig(name=name, **(defaults | overrides))
    # 轮询间隔、租约余量、停机宽限、连接池等待是内部常量(ReliableConfig 默认值),不暴露为环境变量。
    return ReliableConfig(
        targets,
        max_concurrency=settings.RELIABLE_MAX_CONCURRENCY,
        claim_batch_size=settings.RELIABLE_CLAIM_BATCH_SIZE,
        db_pool_size=settings.RELIABLE_DB_POOL_SIZE,
        retention_days=settings.RELIABLE_RETENTION_DAYS,
    )
