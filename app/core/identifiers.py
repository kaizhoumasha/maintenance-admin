import secrets
from collections.abc import Callable
from datetime import datetime
from uuid import UUID
from uuid import uuid4 as _uuid4

from .clock import unix_ms


def uuid4() -> UUID:
    """项目内随机 UUID 的唯一入口;需要无连字符文本时取 `.hex`。"""
    return _uuid4()


def uuid7(now: datetime, *, randbits: Callable[[int], int] = secrets.randbits) -> UUID:
    """RFC 9562 UUIDv7:48 位 UTC 毫秒 + 版本 + 变体 + 74 位随机;标准库 3.13 尚无实现。"""
    milliseconds = unix_ms(now)
    if not 0 <= milliseconds < 2**48:
        raise ValueError("timestamp is outside the UUIDv7 range")
    random_bits = randbits(74)
    return UUID(
        int=(milliseconds << 80)
        | (7 << 76)
        | ((random_bits >> 62) << 64)
        | (2 << 62)
        | (random_bits & (2**62 - 1))
    )


def validate_uuid7(value: object) -> str:
    """只接受小写 canonical 文本形式的 UUIDv7,返回原值。"""
    if not isinstance(value, str):
        raise ValueError("expected a UUIDv7 string")
    try:
        identity = UUID(value)
    except ValueError:
        raise ValueError("expected a canonical UUIDv7") from None
    if identity.version != 7 or str(identity) != value:
        raise ValueError("expected a lowercase canonical UUIDv7")
    return value
