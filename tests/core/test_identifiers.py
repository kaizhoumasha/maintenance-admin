from datetime import UTC, datetime, timedelta, timezone
from uuid import RFC_4122, UUID

import pytest

from app.core.identifiers import uuid7, validate_uuid7


@pytest.mark.parametrize("bits", [0, 2**74 - 1])
def test_uuid7_encodes_milliseconds_version_and_variant(bits):
    now = datetime(2026, 10, 22, 1, 2, 3, 456789, tzinfo=UTC)
    identity = uuid7(now, randbits=lambda width: bits)
    assert identity.version == 7 and identity.variant == RFC_4122
    assert identity.int >> 80 == 1792630923456
    assert str(identity) == str(identity).lower()
    assert validate_uuid7(str(identity)) == str(identity)


def test_uuid7_uses_exact_utc_milliseconds_for_any_offset():
    offset = timezone(timedelta(hours=8))
    identity = uuid7(datetime(1970, 1, 1, 8, 0, 0, 123999, tzinfo=offset))
    assert identity.int >> 80 == 123


@pytest.mark.parametrize(
    "now", [datetime(2026, 10, 22), datetime(1969, 12, 31, tzinfo=UTC), "2026-10-22", 0]
)
def test_uuid7_rejects_naive_or_out_of_range_clock(now):
    with pytest.raises(ValueError):
        uuid7(now)


def test_validate_uuid7_accepts_only_lowercase_canonical_text():
    text = "01a13b00-cabd-7390-890a-0c093e7c07a1"
    assert validate_uuid7(text) == text
    for bad in (
        text.upper(),
        "{" + text + "}",
        "urn:uuid:" + text,
        text.replace("-", ""),
        "9af2751b-4d01-4203-88a7-649bbf69e810",  # UUIDv4
        text + "\x00",
        "",
        "not-a-uuid",
        UUID(text),
        None,
        7,
    ):
        with pytest.raises(ValueError):
            validate_uuid7(bad)


def test_uuid7_defaults_to_now_and_is_unique_and_hex_form_is_32_chars():
    before = datetime.now(UTC)
    first, second = uuid7(), uuid7()
    assert first != second and first.version == 7 and len(first.hex) == 32
    assert before - timedelta(seconds=1) < datetime.fromtimestamp((first.int >> 80) / 1000, UTC)
    # 时间在高位:同毫秒内的区别只在尾部随机位,截取唯一后缀必须取尾部。
    assert first.hex[-12:] != second.hex[-12:]
