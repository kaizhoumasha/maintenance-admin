import base64
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select

from app.core import AppError
from app.models import AuditLogArchive, User
from app.schemas.query import CursorParams
from app.services.audit_service import list_archived_logs
from app.services.pagination import paginate_cursor
from tests.helpers import raw_cursor


async def _use_cursor(db, cursor):
    return await paginate_cursor(db, select(User), User.id, CursorParams(cursor=cursor))


@pytest.mark.parametrize(
    "value",
    [None, [], 1, {}, {"id": None}, {"id": True}, {"id": 1.0}, {"id": "1"}, {"id": 0}, {"id": -1}]
    + [{"id": 2147483648}, {"id": 15, "x": 1}],
)
async def test_cursor_rejects_invalid_payload(db, value):
    with pytest.raises(AppError) as error:
        await _use_cursor(db, raw_cursor(value))
    assert error.value.message == "cursor 无效"


@pytest.mark.parametrize("cursor", ["", "*", "a", "YWJj", "_w", "e30!"])
async def test_cursor_rejects_bad_encoding(db, cursor):
    with pytest.raises(AppError):
        await _use_cursor(db, cursor)


async def test_cursor_rejects_non_canonical_json(db):
    # 同一个 id 的紧凑写法不是服务端发出的形式,不接受
    compact = base64.urlsafe_b64encode(b'{"id":15}').decode().rstrip("=")
    with pytest.raises(AppError):
        await _use_cursor(db, compact)


async def test_cursor_accepts_max_id(db):
    assert (await _use_cursor(db, raw_cursor({"id": 2147483647})))["items"] == []


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
        CursorParams(limit=1, cursor=raw_cursor({"id": 5})),
    )
    assert [u.id for u in result["items"]] == [3]
    assert result["next_cursor"] is not None  # 过滤后 id=1 还在后面
