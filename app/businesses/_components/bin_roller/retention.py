"""Fixed BIN reference collection for one bounded cleanup batch in its transaction."""

from sqlalchemy import delete, literal, or_, select, tuple_, union_all

from app.businesses._components.rack_dispatch.service import get_job, pending_message_references
from app.integrations.ecs_callback import get_event, list_point_events
from app.integrations.ecs_command.results import get_message_ids
from app.integrations.wms_transport import TransportCaller
from app.integrations.wms_transport.results import get_result
from app.models import WorkLine

from .models import TABLES


async def _lock_lines(session, line_ids):
    if line_ids:
        await session.execute(
            select(WorkLine.id)
            .where(WorkLine.id.in_(line_ids))
            .order_by(WorkLine.id)
            .with_for_update()
        )


async def _retained_rounds(session, line_ids=None):
    faces = TABLES["face_work"]
    query = select(faces.c.return_members).where(faces.c.closed_at.is_(None))
    if line_ids is not None:
        query = query.where(faces.c.work_line_id.in_(line_ids))
    return {m["round_id"] for members in await session.scalars(query) for m in members or ()}


async def protected_event_ids(session, *, candidate_ids: tuple[int, ...]) -> frozenset[int]:
    if not candidate_ids:
        return frozenset()
    candidates, lines = set(candidate_ids), set()
    for identity in candidate_ids:
        event = await get_event(session, event_id=identity)
        if event.record:
            lines.add(event.record.context["work_line_id"])
    for key, columns in (
        ("bin_passage", ("scan1_event_id", "scan2_event_id")),
        ("bin_return", ("scan3_event_id", "scan4_event_id", "first_scan4_event_id")),
    ):
        table = TABLES[key]
        lines.update(
            await session.scalars(
                select(table.c.work_line_id)
                .where(or_(*(table.c[column].in_(candidate_ids) for column in columns)))
                .distinct()
            )
        )
    await _lock_lines(session, lines)
    retained_rounds = await _retained_rounds(session, lines)
    protected, contexts = set(), set()
    for key, columns in (
        ("bin_passage", ("scan1_event_id", "scan2_event_id")),
        ("bin_return", ("scan3_event_id", "scan4_event_id", "first_scan4_event_id")),
    ):
        table = TABLES[key]
        live = table.c.closed_at.is_(None)
        if key == "bin_return":
            live |= table.c.id.in_(retained_rounds)
        rows = (
            await session.execute(select(table).where(live, table.c.work_line_id.in_(lines)))
        ).mappings()
        for row in rows:
            contexts.add((row["owner_domain"], row["work_line_id"]))
            for column in columns:
                identity = row[column]
                if identity is None:
                    continue
                protected.add(identity)
                event = await get_event(session, event_id=identity)
                if event.record and event.record.application_ref:
                    protected.update(
                        (
                            event.record.application_ref.canonical_event_id,
                            event.record.application_ref.predecessor_event_id,
                        )
                    )
    for key in ("face_work", "workline_state"):
        table = TABLES[key]
        live = (
            table.c.closed_at.is_(None) if key == "face_work" else table.c.line_run_id.is_not(None)
        )
        contexts.update(
            await session.execute(
                select(table.c.owner_domain, table.c.work_line_id)
                .where(live, table.c.work_line_id.in_(lines))
                .distinct()
            )
        )
    for owner, line in contexts:
        for point in ("SCAN1", "SCAN2", "SCAN3", "SCAN4"):
            events = await list_point_events(
                session, owner_domain=owner, work_line_id=line, point=point, limit=1
            )
            if events:
                protected.add(events[0].event_id)
                if events[0].application_ref:
                    protected.add(events[0].application_ref.predecessor_event_id)
    return frozenset(protected & candidates)


async def _message_references(session, candidates, line_ids=None):
    references = []
    table = TABLES["face_work"]
    query = select(table).where(table.c.closed_at.is_(None))
    if line_ids is not None:
        query = query.where(table.c.work_line_id.in_(line_ids))
    for row in (await session.execute(query)).mappings():
        for identity in (row["feed_message_id"], row["return_message_id"]):
            if identity in candidates:
                references.append((row["work_line_id"], identity))
        if row["current_transport_task_id"]:
            job = await get_job(
                session, owner_domain=row["owner_domain"], job_id=row["rack_job_id"]
            )
            if job and job.origin.caller:
                view = await get_result(
                    session,
                    transport_task_id=row["current_transport_task_id"],
                    client_request_id=row["current_client_request_id"],
                    caller=job.origin.caller,
                )
                if view.submission_message_id in candidates:
                    references.append((row["work_line_id"], view.submission_message_id))
    table = TABLES["workline_state"]
    references.extend(
        await session.execute(
            select(table.c.work_line_id, table.c.drain_message_id).where(
                table.c.drain_phase != "IDLE", table.c.drain_message_id.in_(candidates)
            )
        )
    )
    retained_rounds = await _retained_rounds(session, line_ids)
    command_lines = {}
    for key in ("bin_passage", "bin_return"):
        table = TABLES[key]
        live = table.c.closed_at.is_(None)
        if key == "bin_return":
            live |= table.c.id.in_(retained_rounds)
        query = select(table).where(live)
        if line_ids is not None:
            query = query.where(table.c.work_line_id.in_(line_ids))
        for row in (await session.execute(query)).mappings():
            if key == "bin_return" and row["transport_task_id"] and row["caller"]:
                view = await get_result(
                    session,
                    transport_task_id=row["transport_task_id"],
                    client_request_id=row["client_request_id"],
                    caller=TransportCaller(**row["caller"]),
                )
                if view.submission_message_id in candidates:
                    references.append((row["work_line_id"], view.submission_message_id))
            names = (
                ("scan1_command_code", "release_command_code")
                if key == "bin_passage"
                else ("scan3_command_code", "scan4_command_code")
            )
            for name in names:
                if row[name]:
                    command_lines.setdefault(row["owner_domain"], {}).setdefault(
                        row[name], set()
                    ).add(row["work_line_id"])
    for owner, commands in command_lines.items():
        codes = tuple(commands)
        for start in range(0, len(codes), 500):
            ids = await get_message_ids(
                session,
                owner_domain=owner,
                command_codes=codes[start : start + 500],
                candidate_ids=tuple(candidates),
            )
            references.extend(
                (line, identity) for code, identity in ids.items() for line in commands[code]
            )
    references.extend(await pending_message_references(session, candidate_ids=tuple(candidates)))
    return references


async def protected_message_ids(session, *, candidate_ids: tuple[int, ...]) -> frozenset[int]:
    if not candidate_ids:
        return frozenset()
    candidates = set(candidate_ids)
    hints = await _message_references(session, candidates)
    lines = {line for line, _ in hints}
    await _lock_lines(session, lines)
    # Candidate discovery is only a hint; final protection is reread under the same line locks.
    return frozenset(
        identity for _, identity in await _message_references(session, candidates, lines)
    )


async def cleanup_closed(session, *, cutoff, limit=500, owner_domain=None, tables=None):
    tables = TABLES if tables is None else tables
    queries = []
    for key in ("bin_passage", "bin_return", "face_work"):
        table = tables[key]
        query = select(literal(key).label("kind"), table.c.id, table.c.work_line_id).where(
            table.c.closed_at < cutoff
        )
        if owner_domain is not None:
            query = query.where(table.c.owner_domain == owner_domain)
        queries.append(query)
    candidates = union_all(*queries).subquery()
    cursor = None
    total = 0
    while total < limit:
        query = select(candidates)
        if cursor is not None:
            query = query.where(
                tuple_(candidates.c.work_line_id, candidates.c.kind, candidates.c.id) > cursor
            )
        page = tuple(
            await session.execute(
                query.order_by(candidates.c.work_line_id, candidates.c.kind, candidates.c.id).limit(
                    limit - total
                )
            )
        )
        if not page:
            break
        lines = {line for _, _, line in page}
        # Every subsequent page moves forward in WorkLine order, including protected-only pages.
        await _lock_lines(session, lines)
        face = tables["face_work"]
        members = {
            m["round_id"]
            for values in await session.scalars(
                select(face.c.return_members).where(
                    face.c.closed_at.is_(None), face.c.work_line_id.in_(lines)
                )
            )
            for m in values or ()
        }
        for key, identity, _line in page:
            table = tables[key]
            row = (
                (
                    await session.execute(
                        select(table).where(table.c.id == identity, table.c.closed_at < cutoff)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if not row:
                continue
            if key == "bin_return" and (row["selected_face_work_id"] or identity in members):
                continue
            if key == "face_work" and row["rack_job_id"]:
                job = await get_job(
                    session, owner_domain=row["owner_domain"], job_id=row["rack_job_id"]
                )
                if job and job.pending:
                    continue
            total += (await session.execute(delete(table).where(table.c.id == identity))).rowcount
        key, identity, line = page[-1]
        cursor = (line, key, identity)
    return total
