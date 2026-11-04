"""One host-owned component using four current checkpoints and the original public ports."""

from collections.abc import Mapping

from sqlalchemy import Table, func, or_, select

from app.businesses._components.rack_dispatch.service import RackDispatch
from app.businesses.runtime import Capabilities
from app.core.errors import AppError, ErrorCode
from app.integrations.ecs_command.service import CommandOwner
from app.reliable import ScopeRef
from app.services.workline_service import read_workline

from .models import TABLES
from .ports import Production, RollerWorkRef
from .repository import RollerRepository
from .scan_flow import accept_scan, consume_scan_result, get_scan_result, point_event, release_scan2

ROLLER_SOURCES = ("ROLLER_ORDINARY", "ROLLER_DRAIN")


class RackLockSetChanged(RuntimeError):
    """Original rack set changed while the host acquired the ordered lock set."""


class BinRoller:
    def __init__(
        self,
        context: CommandOwner,
        capabilities: Capabilities,
        production: Production,
        *,
        rack_dispatch: RackDispatch,
        tables: Mapping[str, Table] | None = None,
    ):
        self.context, self.capabilities, self.production = context, capabilities, production
        self.rack_dispatch = rack_dispatch
        self.tables = TABLES if tables is None else tables

    def repository(self, work_line_id):
        return RollerRepository(
            self.context.domain, work_line_id, rack_dispatch=self.rack_dispatch, tables=self.tables
        )

    async def lock_jobs_on_line(self, session, work_line_id):
        jobs = await self.rack_dispatch.jobs(
            session, work_line_id=work_line_id, source_kinds=ROLLER_SOURCES, pending=True
        )
        await self.rack_dispatch.lock_jobs(session, job_ids=tuple(j.id for j in jobs))
        return jobs

    async def lock_production_subjects(self, session, work_line_id, jobs):
        repo = self.repository(work_line_id)
        for key in ("bin_passage", "bin_return", "face_work", "workline_state"):
            table = self.tables[key]
            condition = table.c.closed_at.is_(None) if key != "workline_state" else True
            await session.execute(
                select(*table.primary_key)
                .where(*repo.scoped(table), condition)
                .order_by(*table.primary_key)
                .with_for_update()
            )

    async def original_record(self, session, key, record_id, *, for_update=False):
        table = self.tables[key]
        identity = table.c.line_run_id if key == "workline_state" else table.c.id
        query = select(table).where(
            table.c.owner_domain == self.context.domain, identity == record_id
        )
        if for_update:
            query = query.with_for_update()
        row = (await session.execute(query)).mappings().one_or_none()
        if row is None:
            raise ValueError("original roller checkpoint is missing")
        return row

    async def original_arrival(self, session, arrival_id):
        row = await self.original_record(session, "bin_passage", arrival_id)
        event = await point_event(session, row, "SCAN2")
        if event is None:
            raise ValueError("original SCAN2 event is unavailable")
        return dict(
            row,
            point="SCAN2",
            task_id=row["scope_id"] if row["scope_kind"] == "TASK" else None,
            device_code=event.envelope["device_code"],
            raw_bin_code=event.envelope["data"]["bin_code"],
            scanned_at=event.envelope["timestamp"],
        )

    async def apply_supply_change(self, session, change):
        await self.repository(change.work_line_id).apply_supply_change(session, change)

    async def get_supply_progress(self, session, *, work_line_id, task_id):
        return await self.repository(work_line_id).supply_progress(session, task_id)

    def pending_query(self, kind):
        if kind in ("PASSAGE", "SCAN2_RELEASE"):
            t = self.tables["bin_passage"]
            needed = (
                (t.c.scan1_command_code.is_not(None) & t.c.scan1_consumed_result_id.is_(None))
                if kind == "PASSAGE"
                else (
                    t.c.release_command_code.is_not(None) & t.c.release_consumed_result_id.is_(None)
                )
            )
        elif kind == "RETURN":
            t = self.tables["bin_return"]
            needed = (
                (t.c.return_state == "WAIT_FACT")
                | (t.c.is_current.is_(True) & (t.c.return_state != "OBSERVED"))
                | (t.c.scan3_command_code.is_not(None) & t.c.scan3_consumed_result_id.is_(None))
                | (t.c.scan4_command_code.is_not(None) & t.c.scan4_consumed_result_id.is_(None))
            )
        elif kind == "FACE":
            t = self.tables["face_work"]
            needed = True
        elif kind == "DRAIN":
            t = self.tables["workline_state"]
            returns = self.tables["bin_return"]
            ready_return = select(returns.c.id).where(
                returns.c.owner_domain == t.c.owner_domain,
                returns.c.work_line_id == t.c.work_line_id,
                returns.c.closed_at.is_(None),
                returns.c.return_state == "READY",
            ).exists()
            return select(t.c.line_run_id.label("id")).where(
                t.c.owner_domain == self.context.domain,
                t.c.line_run_id.is_not(None),
                or_(t.c.drain_phase != "IDLE", ready_return),
            )
        else:
            raise ValueError("unsupported roller pending kind")
        return select(t.c.id).where(
            t.c.owner_domain == self.context.domain, t.c.closed_at.is_(None), needed
        )

    async def get_pending_upper_id(self, session, *, kind):
        if kind == "RACK_CYCLE":
            return await self.rack_dispatch.pending_upper_id(session, source_kinds=ROLLER_SOURCES)
        pending = self.pending_query(kind).subquery()
        return await session.scalar(select(func.max(pending.c.id)))

    async def list_pending(self, session, *, kind, after_id, upper_id, limit):
        if not 1 <= limit <= 1000:
            raise ValueError("invalid pending page size")
        if kind == "RACK_CYCLE":
            ids = await self.rack_dispatch.pending_ids(
                session,
                source_kinds=ROLLER_SOURCES,
                after_id=after_id,
                upper_id=upper_id,
                limit=limit,
            )
        else:
            t = self.pending_query(kind).subquery()
            query = select(t.c.id).where(t.c.id <= upper_id)
            if after_id is not None:
                query = query.where(t.c.id > after_id)
            ids = await session.scalars(query.order_by(t.c.id).limit(limit))
        return tuple(RollerWorkRef(kind, record_id) for record_id in ids)

    async def accept_scan(self, ctx, payload):
        await accept_scan(self, ctx, payload)

    async def release_scan2(self, session, *, arrival_id):
        return await release_scan2(self, session, arrival_id=arrival_id)

    async def get_scan2_release_result(self, session, *, arrival_id):
        row = await self.original_record(session, "bin_passage", arrival_id)
        return await get_scan_result(self, session, row["release_command_code"])

    async def get_scan_result(self, session, *, arrival_id):
        row = await self.original_record(session, "bin_passage", arrival_id)
        return await get_scan_result(self, session, row["scan1_command_code"])

    async def advance(self, session, work):
        from .feed_flow import advance_face
        from .rack_flow import advance_cycle, select_supply_cycle
        from .return_flow import advance_drain

        if work.kind in ("PASSAGE", "SCAN2_RELEASE", "RETURN"):
            progress = await consume_scan_result(self, session, work)
            if work.kind == "RETURN":
                row = await self.original_record(session, "bin_return", work.record_id)
                if row["return_state"] == "WAIT_FACT" and row["closed_at"] is None:
                    await self.consume_return_fact(session, return_id=row["id"])
                    return progress
                await self.check_drain(session, row["work_line_id"])
            return progress
        if work.kind == "FACE":
            face = await self.original_record(session, "face_work", work.record_id)
            if face["closed_at"]:
                return "CLOSED"
            job_id = face["rack_job_id"] or await select_supply_cycle(self, session, face["id"])
            if job_id:
                await advance_cycle(self, session, job_id)
            progress = await advance_face(self, session, face["id"])
            updated = await self.original_record(session, "face_work", face["id"])
            if job_id and updated["close_reason"] == "COMPLETED":
                await advance_cycle(self, session, job_id)
            return progress
        if work.kind == "RACK_CYCLE":
            return await advance_cycle(self, session, work.record_id)
        if work.kind == "DRAIN":
            table = self.tables["workline_state"]
            line = (
                (
                    await session.execute(
                        select(table).where(
                            table.c.owner_domain == self.context.domain,
                            table.c.line_run_id == work.record_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if line is None:
                return "CLOSED"
            return await advance_drain(self, session, line["work_line_id"])
        raise ValueError("unsupported roller work kind")

    async def read_resources(self, session, work_line_id):
        try:
            line = await read_workline(session, work_line_id)
        except AppError as error:
            if error.code != ErrorCode.NOT_FOUND:
                raise
            return None
        if (
            line.business_code != self.context.domain
            or not line.is_enabled
            or any(not line.position_bindings.get(k) for k in ("FIVE_RACK", "INLET", "OUTLET"))
        ):
            return None
        return dict(
            workline_code=line.line_code,
            caller_workline_id=str(line.id),
            station_id=None,
            positions={k: line.position_bindings[k] for k in ("FIVE_RACK", "INLET", "OUTLET")},
        )

    async def return_candidates(self, session, *, work_line_id, limit=None):
        from .scan_flow import applied_result, point_view

        candidates = []
        for row in await self.repository(work_line_id).return_queue(session):
            if row["return_state"] != "READY":
                break
            event, view = await point_view(self, session, row, "SCAN4")
            result = (
                applied_result(view, row["scan4_command_code"], event.envelope["device_code"])
                if event
                else None
            )
            if not result or result.result != "SUCCESS":
                break
            candidates.append(row)
            if limit and len(candidates) >= limit:
                break
        return tuple(candidates)

    async def get_return_candidates(self, session, *, work_line_id):
        return await self.return_candidates(session, work_line_id=work_line_id, limit=4)

    async def return_channel_empty(self, session, *, work_line_id):
        repo = self.repository(work_line_id)
        table = self.tables["bin_return"]
        return (
            await session.scalar(
                select(table.c.id)
                .where(
                    *repo.scoped(table),
                    table.c.closed_at.is_(None),
                    or_(
                        table.c.is_current.is_(True)
                        & (
                            (table.c.return_state != "OBSERVED")
                            | table.c.scan4_event_id.is_not(None)
                        ),
                        table.c.selected_face_work_id.is_not(None),
                        table.c.return_state == "WAIT_FACT",
                    ),
                )
                .limit(1)
            )
            is None
        )

    async def check_drain(self, session, work_line_id):
        from .return_flow import advance_drain

        return await advance_drain(self, session, work_line_id)

    async def close_scope(self, session, scope: ScopeRef, *, reason):
        from .return_flow import close_scope

        await close_scope(self, session, scope, reason=reason)

    async def consume_return_fact(self, session, *, return_id):
        from .return_flow import consume_return_fact

        await consume_return_fact(self, session, return_id=return_id)

    async def cleanup_closed(self, session, *, cutoff, limit=500):
        from .retention import cleanup_closed

        return await cleanup_closed(
            session,
            cutoff=cutoff,
            limit=limit,
            owner_domain=self.context.domain,
            tables=self.tables,
        )
