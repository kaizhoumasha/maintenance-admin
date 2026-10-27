"""Generic ECS Status query, independently usable without a device directory or storage."""

from .client import normalize_status_origin, query
from .contracts import (
    CAPABILITY_NAME,
    EcsDeviceInfo,
    EcsDeviceState,
    EcsDeviceStatus,
    EcsStatusResponse,
    StatusQueryError,
    StatusQueryIn,
    StatusQueryResult,
)

__all__ = [
    "CAPABILITY_NAME",
    "EcsDeviceInfo",
    "EcsDeviceState",
    "EcsDeviceStatus",
    "EcsStatusResponse",
    "StatusQueryError",
    "StatusQueryIn",
    "StatusQueryResult",
    "normalize_status_origin",
    "query",
]
