import base64
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select

from app.core import AppError
from app.models import AuditLogArchive, User
from app.schemas.query import CursorParams
from app.services.audit_service import list_archived_logs
from app.services.pagination import _decode_cursor, _encode_cursor, paginate_cursor


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        1,
        {},
        {"id": None},
        {"id": True},
        {"id": 1.0},
        {"id": "1"},
        {"id": 0},
        {"id": -1},
        {"id": 2147483648},
    ],
)
def test_cursor_rejects_invalid_payload(value):
    cursor = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    with pytest.raises(AppError) as error:
        _decode_cursor(cursor)
    assert error.value.message == "cursor 无效"


@pytest.mark.parametrize("cursor", ["", "*", "a", "YWJj", "_w", "e30!", "x" * 65])
def test_cursor_rejects_bad_encoding(cursor):
    with pytest.raises(AppError):
        _decode_cursor(cursor)


def test_cursor_rejects_non_canonical_encoding():
    canonical = _encode_cursor(15)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    trailing_bits = canonical[:-1] + alphabet[(alphabet.index(canonical[-1]) + 1) % 64]
    # 守卫:变体确实解出同样的字节,否则这条用例没有意义
    assert base64.urlsafe_b64decode(trailing_bits + "==") == base64.urlsafe_b64decode(
        canonical + "=="
    )
    compact = base64.urlsafe_b64encode(b'{"id":15}').decode().rstrip("=")
    extra_key = base64.urlsafe_b64encode(json.dumps({"id": 15, "x": 1}).encode()).decode()
    for cursor in (trailing_bits, compact, extra_key.rstrip("=")):
        with pytest.raises(AppError):
            _decode_cursor(cursor)


def test_cursor_id_bounds_roundtrip():
    for value in (1, 2147483647):
        assert _decode_cursor(_encode_cursor(value)) == value


@pytest.mark.parametrize("count", [0, 1, 2, 3, 4, 5])
async def test_cursor_pages_no_count_offset_or_gaps(db, count):
    db.add_all(
        User(username=f"u{i}", email=f"{i}@e.test", hashed_password="x") for i in range(count)
    )
    await db.commit()
    sql = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        sql.append(statement)

    event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
    try:
        ids, cursor = [], None
        while True:
            result = await paginate_cursor(
                db, select(User), User.id, CursorParams(limit=2, cursor=cursor)
            )
            rows, cursor = result["items"], result["next_cursor"]
            ids.extend(u.id for u in rows)
            if cursor is None:
                break
            assert _decode_cursor(cursor) == rows[-1].id
        assert ids == list(range(count, 0, -1))
        assert not any("COUNT(" in s.upper() for s in sql)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", capture)


async def test_archive_cursor_traversal(db):
    now = datetime.now(UTC)
    db.add_all(
        AuditLogArchive(id=i, created_at=now, action="a", trace_id="t", archived_at=now)
        for i in range(1, 6)
    )
    await db.commit()
    ids, cursor = [], None
    while True:
        page = await list_archived_logs(db, CursorParams(limit=2, cursor=cursor))
        ids.extend(row.id for row in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert ids == [5, 4, 3, 2, 1]


async def test_cursor_and_filter_are_intersected(db):
    db.add_all(User(username=f"u{i}", email=f"{i}@e.test", hashed_password="x") for i in range(5))
    await db.commit()
    result = await paginate_cursor(
        db,
        select(User).where(User.id.in_([1, 3, 5])),
        User.id,
        CursorParams(limit=1, cursor=_encode_cursor(5)),
    )
    assert [u.id for u in result["items"]] == [3]
    assert _decode_cursor(result["next_cursor"]) == 3
