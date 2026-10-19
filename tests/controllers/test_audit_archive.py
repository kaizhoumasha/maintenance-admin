"""SPEC-016 审计归档验收测试。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.main import app
from app.models import AuditLog, AuditLogArchive
from tests.helpers import bearer as _bearer
from tests.helpers import make_user_with_perms as _make_user_with_perms

BASE = "/api/v1"
TRIGGER = f"{BASE}/admin/maintenance/archive-audit"
ARCHIVE = f"{BASE}/audit-logs/archive"


async def _seed_logs(*ages_days: int) -> list[int]:
    """按"几天前"造审计日志,返回 id(顺序与入参一致)。"""
    now = datetime.now(UTC)
    async with app.state.audit_session_factory() as s:
        rows = [
            AuditLog(
                user_id=7,
                action=f"test:row:{age}",
                trace_id="t",
                detail={"age": age},
                created_at=now - timedelta(days=age),
            )
            for age in ages_days
        ]
        s.add_all(rows)
        await s.commit()
        return [r.id for r in rows]


async def _ids(model) -> set[int]:
    async with app.state.audit_session_factory() as s:
        return set(await s.scalars(select(model.id)))


async def test_trigger_moves_old_rows_and_returns_count(client, admin):
    old = await _seed_logs(100, 120, 91)
    recent = await _seed_logs(89, 1)

    r = await client.post(TRIGGER, headers=admin)
    assert r.status_code == 200 and r.json() == {"archived": 3}

    source = await _ids(AuditLog)
    assert not source & set(old)  # 源表里 90 天前的都没了
    assert set(recent) <= source  # 89 天前的还在(边界)
    assert await _ids(AuditLogArchive) == set(old)

    async with app.state.audit_session_factory() as s:  # 归档行内容、原 id、原时间都保留
        row = await s.get(AuditLogArchive, old[0])
        assert row.action == "test:row:100" and row.detail == {"age": 100}
        assert row.archived_at is not None
        assert row.created_at.replace(tzinfo=None) < datetime.now(UTC).replace(
            tzinfo=None
        ) - timedelta(days=99)


async def test_archive_is_idempotent(client, admin):
    await _seed_logs(100, 200)
    assert (await client.post(TRIGGER, headers=admin)).json() == {"archived": 2}
    assert (await client.post(TRIGGER, headers=admin)).json() == {"archived": 0}
    async with app.state.audit_session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(AuditLogArchive)) == 2


async def test_trigger_requires_maintenance_permission(client, admin):
    await _seed_logs(100)
    uid = await _make_user_with_perms(
        client, admin, ["admin:audit:read", "admin:audit:read_archive"]
    )
    r = await client.post(TRIGGER, headers=_bearer(uid))
    assert r.status_code == 403 and r.json()["detail"] == "admin:maintenance:archive_audit"
    assert (await client.post(TRIGGER)).status_code == 401
    assert await _ids(AuditLogArchive) == set()  # 没权限就一条也没搬


async def test_archive_list_pagination_and_permission(client, admin):
    old = await _seed_logs(100, 101, 102)
    await client.post(TRIGGER, headers=admin)

    r = await client.get(ARCHIVE, params={"limit": 2}, headers=admin)
    body = r.json()
    assert r.status_code == 200
    assert set(body) == {"items", "next_cursor"}
    assert body["next_cursor"] is not None
    assert [i["id"] for i in body["items"]] == sorted(old, reverse=True)[:2]  # id 倒序
    assert body["items"][0]["archived_at"]
    r = await client.get(ARCHIVE, params={"limit": 2, "cursor": body["next_cursor"]}, headers=admin)
    assert [i["id"] for i in r.json()["items"]] == [min(old)]
    assert r.json()["next_cursor"] is None

    uid = await _make_user_with_perms(client, admin, ["admin:audit:read"])  # 只有 read 不够
    r = await client.get(ARCHIVE, headers=_bearer(uid))
    assert r.status_code == 403 and r.json()["detail"] == "admin:audit:read_archive"
    assert (await client.get(ARCHIVE)).status_code == 401


async def test_archive_list_rejects_bad_params(client, admin):
    for params in (
        {"limit": 0},
        {"limit": 101},
        {"cursor": "x" * 65},
        {"cursor": "bad!"},
    ):
        assert (await client.get(ARCHIVE, params=params, headers=admin)).status_code == 400


async def test_archive_action_rejects_ilike(client, admin):
    group = '{"conditions":[{"field":"action","op":"ilike","value":"admin%"}]}'
    r = await client.get(ARCHIVE, params={"filter": group}, headers=admin)
    assert r.status_code == 400 and r.json()["message"] == "筛选算子不支持"


async def test_retention_days_comes_from_settings(client, admin, monkeypatch):
    from app.core import get_settings

    monkeypatch.setattr(get_settings(), "AUDIT_RETENTION_DAYS", 30)
    old, recent = await _seed_logs(40), await _seed_logs(10)
    assert (await client.post(TRIGGER, headers=admin)).json() == {"archived": 1}
    assert await _ids(AuditLogArchive) == set(old) and set(recent) <= await _ids(AuditLog)


async def test_trigger_logs_count(client, admin, caplog):
    import logging

    await _seed_logs(100, 101)
    with caplog.at_level(logging.INFO):
        await client.post(TRIGGER, headers=admin)
    assert any("搬运 2 条" in r.getMessage() for r in caplog.records)
