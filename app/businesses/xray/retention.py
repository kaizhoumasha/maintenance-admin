"""Protect original messages and callbacks while a reel has unfinished work."""

from sqlalchemy import inspect, select

from app.database import SCHEMA

from .models import Reel, ReelEvent, ReelOperation


async def _has_reels(session):
    # History maintenance also runs against the earlier history-authority schema.
    key = "xray_retention_has_reels"
    if key not in session.info:
        connection = await session.connection()
        schema = None
        if connection.dialect.name == "postgresql":
            options = connection.sync_connection.get_execution_options()
            schema = options.get("schema_translate_map", {}).get(SCHEMA, SCHEMA)
        session.info[key] = await connection.run_sync(
            lambda sync: inspect(sync).has_table(Reel.__tablename__, schema=schema)
        )
    return session.info[key]


async def message_references(session, *, candidate_ids, line_ids=None):
    candidates = set(candidate_ids)
    if not candidates or not await _has_reels(session):
        return ()
    pending = Reel.pending
    if line_ids is not None:
        pending &= Reel.work_line_id.in_(line_ids)
    operations = await session.execute(
        select(Reel.work_line_id, ReelOperation.message_id)
        .join(ReelOperation, ReelOperation.reel_key == Reel.id)
        .where(pending, ReelOperation.message_id.in_(candidates))
    )
    commands = await session.execute(
        select(Reel.work_line_id, Reel.actions).where(pending, Reel.actions.is_not(None))
    )
    return (
        *operations,
        *(
            (line, action["message_id"])
            for line, actions in commands
            for action in actions.values()
            if action["message_id"] in candidates
        ),
    )


async def event_references(session, *, candidate_ids):
    if not candidate_ids or not await _has_reels(session):
        return ()
    return tuple(
        await session.execute(
            select(Reel.work_line_id, ReelEvent.receipt_id)
            .join(ReelEvent, ReelEvent.reel_key == Reel.id)
            .where(Reel.pending, ReelEvent.receipt_id.in_(candidate_ids))
        )
    )
