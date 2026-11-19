"""Bounded storage for a fixed pending-candidate snapshot."""

import json
from collections.abc import Awaitable, Callable
from tempfile import TemporaryFile

PENDING_SNAPSHOT_BATCH_SIZE = 1000


class PendingSnapshot:
    def __init__(self):
        self._files = {}

    def append(self, kind, ids):
        if not ids:
            return
        file = self._files.setdefault(kind, TemporaryFile(mode="w+b"))
        for identity in ids:
            file.write(json.dumps(identity).encode("utf-8") + b"\n")

    def rewind(self):
        for file in self._files.values():
            file.seek(0)

    def kinds(self):
        return tuple(self._files)

    def read_batch(self, kind, limit):
        file = self._files[kind]
        ids = []
        for _ in range(limit):
            line = file.readline()
            if not line:
                break
            ids.append(json.loads(line))
        return tuple(ids)

    def close(self):
        for file in self._files.values():
            file.close()
        self._files.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


async def capture_query(session, query, identity, snapshot, kind):
    after_id = None
    while True:
        page = query
        if after_id is not None:
            page = page.where(identity > after_id)
        ids = tuple(
            await session.scalars(page.order_by(identity).limit(PENDING_SNAPSHOT_BATCH_SIZE))
        )
        if not ids:
            return
        snapshot.append(kind, ids)
        after_id = ids[-1]


async def capture_pages(
    snapshot,
    kind,
    fetch: Callable[[object, int], Awaitable[tuple]],
):
    after_id = None
    while True:
        ids = await fetch(after_id, PENDING_SNAPSHOT_BATCH_SIZE)
        if not ids:
            return
        snapshot.append(kind, ids)
        after_id = ids[-1]
