from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    MetaData,
    Table,
    delete,
    exists,
    func,
    inspect,
    or_,
    select,
    tuple_,
    union_all,
    update,
)

from app.core import get_settings
from app.models import (
    IntegrationExchange,
    IntegrationRecord,
    IntegrationReference,
    ReliableMessage,
    TransportTask,
)
from app.reliable.config import positive
from app.tasks._db import run_with_factory
from app.tasks.celery_app import celery_app


async def history_cleanup_ready(factory):
    from app.database import SCHEMA

    async with factory() as session:
        connection = await session.connection()
        schema = (
            connection.sync_connection.get_execution_options()
            .get("schema_translate_map", {})
            .get(SCHEMA, SCHEMA)
        )
        if connection.dialect.name == "sqlite":
            schema = None
        columns = await connection.run_sync(
            lambda c: inspect(c).get_columns("reliable_message", schema=schema)
        )
        return any(c["name"] == "exchange_id" and not c["nullable"] for c in columns)


async def _legacy_sources(session, root_id, *, deleting=False):
    """Unchanged retired sources are removed only with their verified canonical evidence."""
    import base64
    import binascii
    from hashlib import sha256

    from app.database import SCHEMA
    from migrations.integration_history_v1 import SOURCES, snapshot_version

    references = (
        await session.execute(
            select(IntegrationReference.namespace, IntegrationReference.reference_value).where(
                IntegrationReference.exchange_id == root_id,
                IntegrationReference.reference_kind == "LEGACY_SOURCE",
            )
        )
    ).all()
    removals = []
    connection = await session.connection()
    schema = (
        connection.sync_connection.get_execution_options()
        .get("schema_translate_map", {})
        .get(SCHEMA, SCHEMA)
    )
    if connection.dialect.name == "sqlite":
        schema = None
    for source, key in references:
        if source not in SOURCES:
            return False, []
        if source in {"reliable_message", "transport_task", "coarse_sorting_action"}:
            continue
        if not await connection.run_sync(
            lambda c, source=source: inspect(c).has_table(source, schema=schema)
        ):
            continue
        table = await connection.run_sync(
            lambda c, source=source: Table(
                source, MetaData(), schema=schema, autoload_with=c, resolve_fks=False
            )
        )
        if source != "ecs_command" and (not key.isascii() or not key.isdecimal() or len(key) > 19):
            return False, []
        pk = table.c.command_code if source == "ecs_command" else table.c.id
        value = key if source == "ecs_command" else int(key)
        row = (
            (
                await session.execute(
                    select(*(table.c[name] for name in SOURCES[source].split()))
                    .where(pk == value)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            purged = (
                await session.scalar(
                    select(IntegrationRecord.id)
                    .where(
                        IntegrationRecord.exchange_id == root_id,
                        IntegrationRecord.source_kind == "callback_snapshot:request",
                        IntegrationRecord.source_key == key,
                        IntegrationRecord.body_state == "PURGED",
                    )
                    .limit(1)
                )
                if source == "callback_snapshot"
                else None
            )
            if purged is None:
                return False, []
            continue
        version = snapshot_version(row)
        marker = await session.scalar(
            select(IntegrationRecord.id).where(
                IntegrationRecord.exchange_id == root_id,
                IntegrationRecord.source_kind == source,
                IntegrationRecord.source_key == key,
                IntegrationRecord.source_version == version,
            )
        )
        if marker is None:
            return False, []
        if source == "callback_snapshot":
            try:
                decoded = base64.b64decode(row["body_b64"], validate=True)
            except (ValueError, binascii.Error):
                return False, []
            original = (
                await session.execute(
                    select(
                        IntegrationRecord.body_state,
                        IntegrationRecord.body_sha256,
                        IntegrationRecord.stored_bytes,
                    ).where(
                        IntegrationRecord.exchange_id == root_id,
                        IntegrationRecord.source_kind == "callback_snapshot:request",
                        IntegrationRecord.source_key == key,
                        IntegrationRecord.source_version == version,
                    )
                )
            ).one_or_none()
            if (
                original is None
                or original.body_state not in {"PREVIEW", "PURGED"}
                or original.body_sha256 != sha256(decoded).hexdigest()
                or original.stored_bytes != len(decoded)
            ):
                return False, []
        if source == "callback_snapshot" or (deleting and source == "callback_receipt"):
            removals.append((table, pk, value))
    return True, removals


async def _remove_legacy_sources(session, removals):
    await session.flush()
    for table, pk, value in removals:
        if table.name == "callback_snapshot":
            root_id = await session.scalar(
                select(IntegrationReference.exchange_id).where(
                    IntegrationReference.reference_kind == "LEGACY_SOURCE",
                    IntegrationReference.namespace == "callback_snapshot",
                    IntegrationReference.reference_value == str(value),
                )
            )
            if root_id is not None and await session.scalar(
                select(IntegrationRecord.id)
                .where(
                    IntegrationRecord.exchange_id == root_id,
                    IntegrationRecord.evidence_class == "DIAGNOSTIC",
                    IntegrationRecord.body_state.in_(("FULL", "PREVIEW", "LEGACY_REENCODED")),
                )
                .limit(1)
            ):
                continue
        await session.execute(delete(table).where(pk == value))


async def _protected_roots(session, roots):
    """Original business locks precede history locks for purge and summary deletion."""
    from app.businesses._components.bin_roller.retention import (
        _lock_lines,
        _message_references,
        event_reference_lines,
        protected_event_ids,
        protected_message_ids,
    )
    from app.businesses.coarse_sorting.models import (
        CoarseSortingAction,
        CoarseSortingJob,
        RackPreparation,
    )
    from app.businesses.coarse_sorting.retention import wms_message_references as coarse_references
    from app.businesses.smt_auto.retention import wms_message_references as auto_references

    ids = tuple(row.id for row in roots)
    receipts = {row.receipt_id: row.id for row in roots if row.receipt_id is not None}
    protected = set()
    messages = dict(
        (
            await session.execute(
                select(ReliableMessage.id, ReliableMessage.exchange_id).where(
                    ReliableMessage.exchange_id.in_(ids)
                )
            )
        ).all()
    )
    reference_rows = (
        await session.execute(
            select(
                IntegrationReference.exchange_id,
                IntegrationReference.reference_kind,
                IntegrationReference.namespace,
                IntegrationReference.reference_value,
                IntegrationReference.id,
            ).where(IntegrationReference.exchange_id.in_(ids))
        )
    ).all()
    references = [tuple(row[:4]) for row in reference_rows]
    observed = {identity: set() for identity in ids}
    for row in reference_rows:
        observed[row.exchange_id].add(row.id)
    for root, kind, namespace, value in references:
        if kind == "RELIABLE_MESSAGE" and namespace == "id":
            if (
                value.isascii()
                and value.isdecimal()
                and len(value) <= 19
                and 0 < int(value) < 2**63
                and str(int(value)) == value
            ):
                messages[int(value)] = root
            else:
                protected.add(root)

    async def business_references(session, **scope):
        return (
            *await coarse_references(session, **scope),
            *await auto_references(session, **scope),
        )

    actions = [
        (root, value)
        for root, kind, namespace, value in references
        if kind == "BUSINESS_ACTION" and namespace == "coarse_sorting"
    ]
    lines = await event_reference_lines(session, candidate_ids=tuple(receipts))
    hints = (
        await _message_references(session, set(messages), business_references=business_references)
        if messages
        else []
    )
    lines.update(line for line, _ in hints)
    if actions:
        action = CoarseSortingAction.__table__
        job, prep = CoarseSortingJob.__table__, RackPreparation.__table__
        query = (
            select(
                action.c.id,
                action.c.job_id,
                action.c.preparation_id,
                job.c.state.label("job_state"),
                job.c.work_line_id,
                prep.c.work_line_id.label("prep_line"),
                job.c.pending,
                prep.c.state,
            )
            .select_from(
                action.outerjoin(job, action.c.job_id == job.c.id).outerjoin(
                    prep, action.c.preparation_id == prep.c.id
                )
            )
            .where(action.c.id.in_([value for _, value in actions]))
        )
        hints = (await session.execute(query)).mappings().all()
        lines.update(row["work_line_id"] or row["prep_line"] for row in hints)
    owner_queries = {}
    for kind, table in (
        ("BUSINESS_TASK", CoarseSortingJob.__table__),
        ("BUSINESS_PREPARATION", RackPreparation.__table__),
    ):
        values = [
            value
            for _, reference_kind, namespace, value in references
            if reference_kind == kind and namespace == "coarse_sorting"
        ]
        if values:
            columns = [table.c.id, table.c.work_line_id, table.c.state]
            if kind == "BUSINESS_TASK":
                columns.append(table.c.pending)
            owner_query = select(*columns).where(table.c.id.in_(values))
            owner_queries[kind] = owner_query
            lines.update(row.work_line_id for row in await session.execute(owner_query))
    await _lock_lines(session, lines)
    protected.update(
        receipts[value]
        for value in await protected_event_ids(session, candidate_ids=tuple(receipts))
    )
    protected.update(
        messages[value]
        for value in await protected_message_ids(
            session,
            candidate_ids=tuple(messages),
            business_references=business_references,
        )
    )
    known = set()
    if actions:
        current = {row["id"]: row for row in (await session.execute(query)).mappings()}
        for root, value in actions:
            row = current.get(value)
            if row is None:
                protected.add(root)
                continue
            known.add((root, "BUSINESS_ACTION", "coarse_sorting", value))
            for kind, column in (
                ("BUSINESS_TASK", "job_id"),
                ("BUSINESS_PREPARATION", "preparation_id"),
            ):
                if row[column]:
                    known.add((root, kind, "coarse_sorting", row[column]))
            if (
                row["job_id"] is not None
                and (row["pending"] or row["job_state"] not in {"COMPLETED", "NG_COMPLETED"})
            ) or (
                row["preparation_id"] is not None
                and row["state"] not in {"READY", "DEPART_ACCEPTED"}
            ):
                protected.add(root)
    for kind, owner_query in owner_queries.items():
        current = {row["id"]: row for row in (await session.execute(owner_query)).mappings()}
        for root, reference_kind, namespace, value in references:
            if reference_kind != kind or namespace != "coarse_sorting":
                continue
            row = current.get(value)
            if row is None:
                protected.add(root)
                continue
            known.add((root, kind, namespace, value))
            closed = row["state"] in (
                {"COMPLETED", "NG_COMPLETED"}
                if kind == "BUSINESS_TASK"
                else {"READY", "DEPART_ACCEPTED"}
            )
            if not closed or (kind == "BUSINESS_TASK" and row["pending"]):
                protected.add(root)
    for reference in references:
        if reference[1].startswith("BUSINESS_") and tuple(reference) not in known:
            protected.add(reference[0])
    return protected, observed


async def _references_unchanged(session, root_id, observed):
    current = set(
        await session.scalars(
            select(IntegrationReference.id).where(
                IntegrationReference.exchange_id == root_id,
            )
        )
    )
    return current == observed[root_id]


def _root_columns():
    return (IntegrationExchange.id, IntegrationExchange.role, IntegrationExchange.receipt_id)


async def _used_records(session, record_ids, *, outside_root=None, for_body=False):
    probes = (
        (
            IntegrationExchange,
            ("request_record_id", "ack_record_id", "current_response_record_id"),
            IntegrationExchange.id,
        ),
        (
            IntegrationRecord,
            ("body_record_id", "response_record_id")
            if for_body
            else ("body_record_id", "response_record_id", "parent_record_id"),
            IntegrationRecord.exchange_id,
        ),
        (IntegrationReference, ("source_record_id",), IntegrationReference.exchange_id),
        (
            ReliableMessage,
            ("request_record_id", "terminal_response_record_id", "last_response_record_id"),
            None,
        ),
        (TransportTask, ("acceptance_request_record_id", "acceptance_response_record_id"), None),
    )
    for model, columns, owner in probes:
        branches = []
        for column in columns:
            query = select(model.id).where(getattr(model, column).in_(record_ids))
            if outside_root is not None and owner is not None:
                query = query.where(owner != outside_root)
            branches.append(query)
        # Separate branches let PostgreSQL join owned IDs through each reverse index.
        if await session.scalar(union_all(*branches).limit(1)) is not None:
            return True
    return False


async def run_history_cleanup(factory, days=90, *, now=None, batch=500) -> int:
    positive(days, "INTEGRATION_HISTORY_RETENTION_DAYS")
    positive(batch, "batch")
    if not await history_cleanup_ready(factory):
        return 0
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    total, cursor = 0, None
    while True:
        async with factory() as session, session.begin():
            query = select(*_root_columns(), IntegrationExchange.created_at).where(
                IntegrationExchange.role != "ECS_COMMAND",
                IntegrationExchange.created_at < cutoff,
            )
            if cursor:
                query = query.where(
                    tuple_(IntegrationExchange.created_at, IntegrationExchange.id) > cursor
                )
            rows = (
                await session.execute(
                    query.order_by(IntegrationExchange.created_at, IntegrationExchange.id).limit(
                        batch
                    )
                )
            ).all()
            if not rows:
                return total
            protected, observed = await _protected_roots(session, rows)
            for candidate in rows:
                if candidate.id in protected:
                    continue
                root = await session.scalar(
                    select(IntegrationExchange)
                    .where(IntegrationExchange.id == candidate.id)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
                if (
                    root is None
                    or root.receipt_active
                    or await session.scalar(
                        select(exists().where(ReliableMessage.exchange_id == root.id))
                    )
                    or await session.scalar(
                        select(exists().where(TransportTask.submission_exchange_id == root.id))
                    )
                ):
                    continue
                if await session.scalar(
                    select(IntegrationRecord.id)
                    .where(
                        IntegrationRecord.exchange_id == root.id,
                        or_(
                            IntegrationRecord.occurred_at >= cutoff,
                            (IntegrationRecord.occurred_at.is_(None))
                            & (IntegrationRecord.capture_origin == "LIVE")
                            & (IntegrationRecord.recorded_at >= cutoff),
                        ),
                    )
                    .limit(1)
                ):
                    continue
                if not await _references_unchanged(session, root.id, observed):
                    continue
                source_ready, old_sources = await _legacy_sources(session, root.id, deleting=True)
                if not source_ready:
                    continue
                if await session.scalar(
                    select(IntegrationReference.id)
                    .where(
                        IntegrationReference.related_exchange_id == root.id,
                        IntegrationReference.exchange_id != root.id,
                    )
                    .limit(1)
                ):
                    continue
                owned_records = select(IntegrationRecord.id).where(
                    IntegrationRecord.exchange_id == root.id
                )
                locked = owned_records.with_for_update(skip_locked=True).cte("locked_history")
                locked_count = await session.scalar(select(func.count()).select_from(locked))
                total_count = await session.scalar(
                    select(func.count()).select_from(owned_records.subquery())
                )
                if locked_count != total_count or await _used_records(
                    session, owned_records, outside_root=root.id
                ):
                    continue
                await session.execute(
                    delete(IntegrationReference).where(IntegrationReference.exchange_id == root.id)
                )
                await session.execute(
                    update(IntegrationExchange)
                    .where(IntegrationExchange.id == root.id)
                    .values(
                        request_record_id=None, ack_record_id=None, current_response_record_id=None
                    )
                )
                await session.execute(
                    delete(IntegrationRecord).where(IntegrationRecord.exchange_id == root.id)
                )
                await session.delete(root)
                await _remove_legacy_sources(session, old_sources)
                total += 1
            cursor = rows[-1].created_at, rows[-1].id


async def _purge_records(session, records, at):
    import json

    for record in records:
        if record.body_record_id:
            descriptor = (
                await session.execute(
                    select(IntegrationRecord.body_sha256, IntegrationRecord.stored_bytes).where(
                        IntegrationRecord.id == record.body_record_id
                    )
                )
            ).one()
            record.body_sha256, record.stored_bytes = descriptor
        details = {
            **(record.details or {}),
            "purged_at": at.astimezone(UTC).isoformat(),
        }
        if len(json.dumps(details, ensure_ascii=False).encode()) > 4096:
            details = {
                "projection_state": "UNAVAILABLE",
                "purged_at": details["purged_at"],
            }
        record.raw_body = record.body_record_id = None
        record.body_state, record.details = "PURGED", details


async def run_cleanup(factory, days, *, now=None, batch=500) -> int:
    """Purge eligible diagnostic bytes on their original source clock."""
    positive(days, "CALLBACK_SNAPSHOT_RETENTION_DAYS")
    positive(batch, "batch")
    if not await history_cleanup_ready(factory):
        return 0
    at = now or datetime.now(UTC)
    cutoff = at - timedelta(days=days)
    total, cursor = 0, None
    while True:
        async with factory() as session, session.begin():
            query = select(
                IntegrationRecord.id, IntegrationRecord.exchange_id, IntegrationRecord.attempt_id
            ).where(
                IntegrationRecord.evidence_class == "DIAGNOSTIC",
                IntegrationRecord.record_kind.in_(("CALLBACK_RECEIVED", "RESPONSE_PREPARED")),
                IntegrationRecord.occurred_at < cutoff,
                IntegrationRecord.body_state.in_(("FULL", "PREVIEW", "LEGACY_REENCODED")),
            )
            if cursor is not None:
                query = query.where(IntegrationRecord.id > cursor)
            rows = (await session.execute(query.order_by(IntegrationRecord.id).limit(batch))).all()
            if not rows:
                return total
            roots = (
                await session.execute(
                    select(*_root_columns()).where(
                        IntegrationExchange.id.in_([row.exchange_id for row in rows])
                    )
                )
            ).all()
            protected, observed = await _protected_roots(session, roots)
            for row in rows:
                if row.exchange_id in protected:
                    continue
                locked = await session.scalar(
                    select(IntegrationExchange.id)
                    .where(IntegrationExchange.id == row.exchange_id)
                    .with_for_update(skip_locked=True)
                )
                if locked is None:
                    continue
                if not await _references_unchanged(session, row.exchange_id, observed):
                    continue
                source_ready, old_sources = await _legacy_sources(session, row.exchange_id)
                if not source_ready:
                    continue
                records = list(
                    await session.scalars(
                        select(IntegrationRecord)
                        .where(
                            IntegrationRecord.exchange_id == row.exchange_id,
                            IntegrationRecord.evidence_class == "DIAGNOSTIC",
                            IntegrationRecord.attempt_id == row.attempt_id,
                            IntegrationRecord.occurred_at < cutoff,
                            IntegrationRecord.body_state.in_(
                                ("FULL", "PREVIEW", "LEGACY_REENCODED")
                            ),
                        )
                        .with_for_update(skip_locked=True)
                        .execution_options(populate_existing=True)
                    )
                )
                if not records or row.id not in {record.id for record in records}:
                    continue
                identities = tuple(record.id for record in records)
                if await _used_records(session, identities, for_body=True):
                    continue
                await _purge_records(session, records, at)
                total += 1
                await _remove_legacy_sources(session, old_sources)
            cursor = rows[-1].id


async def run_receipt_cleanup(factory, days, *, now=None, batch=500) -> int:
    """Release original slots; bodies and searchable facts have separate clocks."""
    positive(days, "RELIABLE_RETENTION_DAYS")
    positive(batch, "batch")
    if not await history_cleanup_ready(factory):
        return 0
    at = now or datetime.now(UTC)
    cutoff = at - timedelta(days=days)
    total, cursor = 0, 0
    while True:
        async with factory() as session, session.begin():
            if session.get_bind().dialect.name == "postgresql":
                await session.connection(execution_options={"isolation_level": "READ COMMITTED"})
            roots = (
                await session.execute(
                    select(*_root_columns())
                    .where(
                        IntegrationExchange.role == "RECEIPT",
                        IntegrationExchange.receipt_id > cursor,
                        IntegrationExchange.created_at < cutoff,
                    )
                    .order_by(IntegrationExchange.receipt_id)
                    .limit(batch)
                )
            ).all()
            if not roots:
                return total
            protected, observed = await _protected_roots(session, roots)
            for candidate in roots:
                if candidate.id in protected:
                    continue
                root = await session.scalar(
                    select(IntegrationExchange)
                    .where(IntegrationExchange.id == candidate.id)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
                if root is None:
                    continue
                if not await _references_unchanged(session, root.id, observed):
                    continue
                ready, _ = await _legacy_sources(session, root.id)
                if not ready:
                    continue
                if root.receipt_active:
                    root.receipt_active = False
                    total += 1
                records = list(
                    await session.scalars(
                        select(IntegrationRecord)
                        .where(
                            IntegrationRecord.exchange_id == root.id,
                            IntegrationRecord.evidence_class == "CORE",
                            IntegrationRecord.body_state.in_(
                                ("FULL", "PREVIEW", "LEGACY_REENCODED")
                            ),
                        )
                        .with_for_update(skip_locked=True)
                    )
                )
                for record in records:
                    if not await _used_records(
                        session, (record.id,), outside_root=root.id, for_body=True
                    ):
                        await _purge_records(session, [record], at)
            cursor = roots[-1].receipt_id


def _run(cleanup, days_setting: str) -> int:
    settings = get_settings()
    return run_with_factory(
        settings.DATABASE_URL, lambda f: cleanup(f, getattr(settings, days_setting))
    )


@celery_app.task(name="callback.cleanup_snapshots")
def cleanup_snapshots() -> int:
    return _run(run_cleanup, "CALLBACK_SNAPSHOT_RETENTION_DAYS")


@celery_app.task(name="callback.cleanup_receipts")
def cleanup_receipts() -> int:
    settings = get_settings()

    async def cleanup(factory):
        released = await run_receipt_cleanup(factory, settings.RELIABLE_RETENTION_DAYS)
        await run_history_cleanup(factory, settings.INTEGRATION_HISTORY_RETENTION_DAYS)
        return released

    return run_with_factory(settings.DATABASE_URL, cleanup)
