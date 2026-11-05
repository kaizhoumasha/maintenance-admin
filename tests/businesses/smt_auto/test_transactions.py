"""Bounded S1 entry must own fresh whole transactions, including failed commits."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import DBAPIError

from app.businesses.runtime import Capabilities


def failure(code):
    return DBAPIError(None, None, SimpleNamespace(sqlstate=code))


@pytest.mark.parametrize("kind", ["PREPARE_NEXT", "PREPARE_RESULT"])
@pytest.mark.parametrize("phase", ["body", "commit"])
async def test_serialization_restarts_the_whole_transaction(kind, phase):
    from app.businesses.smt_auto.transactions import AutoS1Transactions

    events, sessions = [], []

    class Session:
        async def __aenter__(self):
            sessions.append(self)
            events.append("open")
            return self

        async def __aexit__(self, *args):
            events.append("close")

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        @asynccontextmanager
        async def begin(self):
            events.append("begin")
            try:
                yield
                events.append("commit")
                if phase == "commit" and len(sessions) < 3:
                    raise failure("40001")
            except BaseException:
                events.append("rollback")
                raise

        async def connection(self, *, execution_options):
            assert execution_options == {"isolation_level": "SERIALIZABLE"}
            assert events[-1] == "begin"
            events.append("isolation")

    class Host:
        async def run(self, session, **kwargs):
            assert events[-1] == "isolation"
            assert kwargs == {
                ("work_line_id" if kind == "PREPARE_NEXT" else "task_id"): "id",
                "now_ms": 9,
            }
            events.append("body")
            if phase == "body" and len(sessions) < 3:
                raise failure("40001")
            return "done"

        prepare_next = run
        advance_prepare = run

    entry = AutoS1Transactions(Capabilities(None, None, None, Session))
    entry.host = Host()
    assert await entry.advance(kind, "id", now_ms=9) == "done"
    assert len({id(session) for session in sessions}) == 3
    assert events.count("rollback") == 2 and events.count("body") == 3
    assert events[-2:] == ["commit", "close"]


@pytest.mark.parametrize("code,attempts", [("40001", 3), ("23505", 1)])
async def test_failure_propagates_without_unbounded_retry(code, attempts):
    from app.businesses.smt_auto.transactions import AutoS1Transactions

    opened = []

    class Session:
        async def __aenter__(self):
            opened.append(self)
            return self

        async def __aexit__(self, *args):
            pass

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        @asynccontextmanager
        async def begin(self):
            yield
            raise failure(code)

    class Host:
        async def prepare_next(self, session, **kwargs):
            return "uncommitted"

    entry = AutoS1Transactions(Capabilities(None, None, None, Session))
    entry.host = Host()
    with pytest.raises(DBAPIError) as caught:
        await entry.advance("PREPARE_NEXT", 1, now_ms=9)
    assert caught.value.orig.sqlstate == code and len(opened) == attempts
    with pytest.raises(ValueError):
        await entry.advance("PRODUCTION", 1, now_ms=9)
    assert len(opened) == attempts


@pytest.mark.parametrize("phase", ["body", "commit"])
async def test_changed_rack_lock_set_restarts_fresh_whole_transaction(phase):
    from app.businesses._components.bin_roller.service import RackLockSetChanged
    from app.businesses.smt_auto.transactions import AutoS1Transactions

    sessions = []

    class Session:
        async def __aenter__(self):
            sessions.append(self)
            return self

        async def __aexit__(self, *args):
            pass

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        @asynccontextmanager
        async def begin(self):
            yield
            if phase == "commit" and len(sessions) < 3:
                raise RackLockSetChanged("changed")

    class Host:
        async def advance(self, session, **kwargs):
            assert kwargs == {"task_id": "t", "now_ms": 9}
            if phase == "body" and len(sessions) < 3:
                raise RackLockSetChanged("changed")
            return "confirmed"

    entry = AutoS1Transactions(Capabilities(None, None, None, Session), task_flow=Host())
    assert await entry.advance("TASK", "t", now_ms=9) == "confirmed"
    assert len({id(s) for s in sessions}) == 3
