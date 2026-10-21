from datetime import UTC, datetime, timedelta

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def unix_ms(now: datetime) -> int:
    """带时区时间的 UTC Unix 毫秒数(向下取整)。"""
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be an aware datetime")
    return (now.astimezone(UTC) - _EPOCH) // timedelta(milliseconds=1)
