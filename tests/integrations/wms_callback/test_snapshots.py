from datetime import UTC, datetime

from sqlalchemy import select

from app.integrations.wms_callback import snapshots
from app.integrations.wms_callback.receiver import CallbackResponse
from app.models import CallbackSnapshot


def test_each_source_has_its_own_quota(callback_store, monkeypatch):
    monkeypatch.setattr(snapshots, "SNAPSHOTS_PER_MINUTE", 2)
    monkeypatch.setattr(snapshots, "monotonic", lambda: 100.0)
    assert [snapshots.allow_snapshot("ecs") for _ in range(3)] == [True, True, False]
    assert snapshots.allow_snapshot("wms") is True


def test_default_source_is_wms(callback_store, monkeypatch):
    monkeypatch.setattr(snapshots, "SNAPSHOTS_PER_MINUTE", 1)
    monkeypatch.setattr(snapshots, "monotonic", lambda: 100.0)
    assert snapshots.allow_snapshot() is True
    assert snapshots.allow_snapshot("wms") is False


async def test_record_snapshot_stores_source(callback_store):
    for index, source in enumerate(["ecs", None]):
        await snapshots.record_snapshot(
            callback_store,
            preview=b"{}",
            truncated=False,
            response=CallbackResponse(200, {}, "ok"),
            request_id=f"req-{index}",
            received_at=datetime.now(UTC),
            **({"source": source} if source else {}),
        )
    async with callback_store() as session:
        rows = list(await session.scalars(select(CallbackSnapshot).order_by(CallbackSnapshot.id)))
    assert [row.source for row in rows] == ["ecs", None]
