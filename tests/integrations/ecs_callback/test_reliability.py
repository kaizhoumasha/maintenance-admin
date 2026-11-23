"""接收机制的并发合同:同身份事件并发时靠收据唯一索引串行化(仅 PostgreSQL 有真并发)。

回滚、重放、提交失败的合同已由 test_receiver.py 与 controllers/test_ecs_callback.py 固化。
"""

import asyncio
import json

import pytest
from sqlalchemy import func, select

from app.integrations.ecs_callback import CallbackPayload, registry
from app.integrations.ecs_callback.receiver import ACK, receive_ecs_event
from app.integrations.ecs_callback.registry import ecs_event
from app.models import IntegrationExchange
from tests.helpers import ecs_binding

EVENT = json.dumps(
    {"device_code": "D1", "event_type": "SCAN_COMPLETED", "timestamp": 1, "data": {"b": 1}}
).encode()


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})


@pytest.fixture
def pg_factory(callback_store, callback_effects):
    if callback_store.kw["bind"].dialect.name != "postgresql":
        pytest.skip("并发串行化只在 PostgreSQL 上有意义")
    return callback_store


async def receive(factory, extra=None):
    async with factory() as session, session.begin():
        return await receive_ecs_event(
            session,
            json.dumps(json.loads(EVENT) | (extra or {})).encode(),
            resolve_binding=ecs_binding,
        )


async def race(factory, wait_for_lock, handler_after_release, effects, *, second_extra=None):
    """A 的处理器写入业务行后停住,B 此时进入并阻塞在收据唯一索引上,再放行 A。"""
    inserted, release, calls = asyncio.Event(), asyncio.Event(), []

    async def handler(ctx, data):
        calls.append(len(calls))
        if len(calls) > 1:  # B 接管时的执行
            await ctx.session.execute(effects.insert().values(marker="B"))
            return
        await ctx.session.execute(effects.insert().values(marker="A"))
        await ctx.session.flush()
        inserted.set()
        await release.wait()
        handler_after_release()

    ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=CallbackPayload)(handler)
    task_a = asyncio.create_task(receive(factory))
    task_b = None
    try:
        await asyncio.wait_for(inserted.wait(), 3)
        task_b = asyncio.create_task(receive(factory, second_extra))
        await wait_for_lock(task_b)  # 观察到 B 确实卡在 A 未提交的唯一索引项上
        assert not task_b.done()
        release.set()
        async with asyncio.timeout(10):
            results = await asyncio.gather(task_a, task_b, return_exceptions=True)
    finally:
        release.set()
        pending = [t for t in (task_a, task_b) if t is not None]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    return calls, results


async def rows(factory, effects):
    async with factory() as session:
        receipts = await session.scalar(
            select(func.count())
            .select_from(IntegrationExchange)
            .where(
                IntegrationExchange.role == "RECEIPT", IntegrationExchange.receipt_active.is_(True)
            )
        )
        ids = (await session.scalars(select(effects.c.marker))).all()
    return receipts, sorted(ids)


async def test_concurrent_identical_events_run_handler_once(
    pg_factory, wait_for_lock, callback_effects
):
    calls, responses = await race(pg_factory, wait_for_lock, lambda: None, callback_effects)
    assert [(r.status_code, r.body) for r in responses] == [(200, ACK)] * 2
    assert sorted(r.outcome for r in responses) == ["duplicate", "received"]
    assert len(calls) == 1
    assert await rows(pg_factory, callback_effects) == (1, ["A"])


async def test_waiter_takes_over_when_first_request_rolls_back(
    pg_factory, wait_for_lock, callback_effects
):
    def fail():
        raise RuntimeError("boom")

    calls, (error_a, resp_b) = await race(pg_factory, wait_for_lock, fail, callback_effects)
    assert isinstance(error_a, RuntimeError) and str(error_a) == "boom"
    assert (resp_b.status_code, resp_b.body, resp_b.outcome) == (200, ACK, "received")
    assert len(calls) == 2  # A 失败一次,B 成功一次
    assert await rows(pg_factory, callback_effects) == (1, ["B"])


async def test_same_core_event_concurrently_executes_once(
    pg_factory, wait_for_lock, callback_effects
):
    calls, responses = await race(
        pg_factory,
        wait_for_lock,
        lambda: None,
        callback_effects,
        second_extra={"is_debug": True, "trace": "B"},
    )
    assert [(r.status_code, r.body) for r in responses] == [(200, ACK)] * 2
    assert sorted(r.outcome for r in responses) == ["duplicate", "received"]
    assert len(calls) == 1
    assert await rows(pg_factory, callback_effects) == (1, ["A"])
