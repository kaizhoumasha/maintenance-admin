from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
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
    base_url: str | None
    protocol: str
    timeout_ms: float = 10000
    alert_after_attempts: int = 8
    retry_base_s: float = 1
    retry_cap_s: float = 300
    max_retry_after_s: float = 3600
    max_body_bytes: int = 262144
    max_not_received_attempts: int | None = None
    fixed_retry_interval_s: float | None = None
    address_mode: str = "STATIC"

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
        if self.max_not_received_attempts is not None:
            positive(self.max_not_received_attempts, "max_not_received_attempts")
            if not isinstance(self.max_not_received_attempts, int):
                raise TypeError("max_not_received_attempts must be an integer")
        if self.fixed_retry_interval_s is not None:
            positive(self.fixed_retry_interval_s, "fixed_retry_interval_s")
        if self.address_mode not in {"STATIC", "SNAPSHOT"}:
            raise ValueError("invalid address mode")
        if self.address_mode == "SNAPSHOT":
            if self.base_url is not None:
                raise ValueError("SNAPSHOT has no static base_url")
            return
        self.http_config()
        if self.base_url.endswith("/"):
            raise ValueError("target base_url must not end with slash")

    def effective(self, snapshot):
        if snapshot is None:
            if self.address_mode != "STATIC":
                raise ValueError("snapshot required")
            return self
        if self.address_mode != "SNAPSHOT" or self.protocol != snapshot.protocol:
            raise ValueError("delivery configuration drift")
        return replace(self, base_url=snapshot.origin, address_mode="STATIC")

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
    maintenance_batch_size: int = 100

    def __post_init__(self):
        for field in fields(self):
            if field.name != "targets":
                positive(getattr(self, field.name), field.name)
        for name in (
            "max_concurrency",
            "claim_batch_size",
            "db_pool_size",
            "retention_days",
            "maintenance_batch_size",
        ):
            if not isinstance(getattr(self, name), int):
                raise TypeError(f"{name} must be an integer")
        object.__setattr__(self, "targets", MappingProxyType(dict(self.targets)))


@contextmanager
def _config_error(label: str) -> Iterator[None]:
    """保持原异常类型,消息里补上配置名和出错位置,避免只看到 TargetConfig 的内部参数错误。"""
    try:
        yield
    except (TypeError, ValueError) as error:
        raise type(error)(f"RELIABLE_TARGETS {label}: {error}") from error


def parse_reliable_config(settings: "Settings") -> ReliableConfig:
    raw = settings.RELIABLE_TARGETS
    if unknown := set(raw) - {"defaults", "targets"}:
        raise ValueError(
            f"RELIABLE_TARGETS: unknown reliable target configuration {sorted(unknown)}"
            "(只允许 defaults、targets)"
        )
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
        raise ValueError("RELIABLE_TARGETS: defaults and targets must be objects")
    defaults.update(raw.get("defaults", {}))
    with _config_error("defaults"):
        TargetConfig(
            **(
                {"name": "validation", "base_url": "http://invalid.test", "protocol": "validation"}
                | defaults
            )
        )
    targets = {}
    for name, overrides in raw.get("targets", {}).items():
        if not isinstance(overrides, dict) or "name" in overrides:
            raise ValueError(
                f"RELIABLE_TARGETS target {name!r}: must be an object without name override"
            )
        with _config_error(f"target {name!r}"):
            targets[name] = TargetConfig(name=name, **({"base_url": None} | defaults | overrides))
    # 轮询间隔、租约余量、停机宽限、连接池等待是内部常量(ReliableConfig 默认值),不暴露为环境变量。
    return ReliableConfig(
        targets,
        max_concurrency=settings.RELIABLE_MAX_CONCURRENCY,
        claim_batch_size=settings.RELIABLE_CLAIM_BATCH_SIZE,
        db_pool_size=settings.RELIABLE_DB_POOL_SIZE,
        retention_days=settings.RELIABLE_RETENTION_DAYS,
    )


def targets_for_protocol(targets: Mapping[str, TargetConfig], protocol: str) -> list[str]:
    return [name for name, item in targets.items() if item.protocol == protocol]


def target_config_hint(targets: Mapping[str, TargetConfig], protocol: str) -> str:
    """缺少某协议的目标时附在错误后面:列出现有目标和一个可复制的配置示例。"""
    existing = ", ".join(f"{n}[{t.protocol}]" for n, t in targets.items())
    example = (
        f'{{"targets":{{"{protocol}":{{"base_url":"http://host:port","protocol":"{protocol}"}}}}}}'
    )
    return f"(现有:{existing or '无'})。示例:RELIABLE_TARGETS='{example}'"
