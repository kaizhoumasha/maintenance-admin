from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.core.clock import unix_ms


def test_unix_ms_is_exact_floor_in_utc_for_any_offset():
    offset = timezone(timedelta(hours=8))
    assert unix_ms(datetime(1970, 1, 1, 8, 0, 0, 123999, tzinfo=offset)) == 123
    assert unix_ms(datetime(2026, 10, 22, 1, 2, 3, 456789, tzinfo=UTC)) == 1792630923456


@pytest.mark.parametrize("now", [datetime(2026, 10, 22), "2026-10-22", None, 1792630923])
def test_unix_ms_requires_an_aware_datetime(now):
    with pytest.raises(ValueError):
        unix_ms(now)
