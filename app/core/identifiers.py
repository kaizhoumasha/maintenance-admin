import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from .clock import unix_ms


def uuid7(
    now: datetime | None = None, *, randbits: Callable[[int], int] = secrets.randbits
) -> UUID:
    """项目内 UUID 的唯一入口(RFC 9562 UUIDv7:48 位 UTC 毫秒 + 版本 + 变体 + 74 位随机)。

    需要无连字符文本时取 `.hex`;前 12 位十六进制是时间,截取唯一后缀应取尾部。
    """
    milliseconds = unix_ms(datetime.now(UTC) if now is None else now)
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
