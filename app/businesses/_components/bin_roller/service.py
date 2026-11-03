"""One business-owned component; every operation uses its caller's transaction."""

from collections.abc import Mapping
from time import time

from sqlalchemy import Table, func, or_, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.businesses._components.rack_dispatch.service import RackDispatch
from app.businesses.runtime import Capabilities
from app.core.errors import AppError, ErrorCode
from app.integrations.ecs_callback import EcsEventContext
from app.integrations.ecs_command.contracts import CommandView
from app.integrations.ecs_command.service import CommandOwner
from app.integrations.wms_transport import HandoffPosition, TransportCaller, TransportTaskResult
from app.services.workline_service import read_workline

from .contracts import ScanCompletedData
from .models import TABLES
from .ports import Production, RollerWorkRef, SupplyChange, SupplyProgress
from .repository import RollerRepository
from .scan_flow import accept_scan, consume_scan_result, get_scan_result, release_scan2

ROLLER_SOURCES = ("ROLLER_ORDINARY", "ROLLER_DRAIN")


class RackLockSetChanged(RuntimeError):
    """An original job entered or left the located set while acquiring host locks."""


class BinRoller:
    def __init__(
        self,
        context: CommandOwner,
        capabilities: Capabilities,
        production: Production,
        *,
        rack_dispatch: RackDispatch,
        tables: Mapping[str, Table] | None = None,
    ) -> None:
        self.rack_dispatch = rack_dispatch
        self.context = context
        self.capabilities = capabilities
        self.production = production
        self.tables = TABLES if tables is None else tables

    def repository(self, work_line_id: int) -> RollerRepository:
        return RollerRepository(
            self.context.domain, work_line_id, tables=self.tables, rack_dispatch=self.rack_dispatch
        )

    async def lock_jobs_on_line(self, session, work_line_id):
        jobs = await self.rack_dispatch.jobs(
            session, work_line_id=work_line_id, source_kinds=ROLLER_SOURCES, pending=True
        )
        await self.rack_dispatch.lock_jobs(session, job_ids=tuple(j.id for j in jobs))
        return jobs

    async def lock_production_subjects(self, session, work_line_id, jobs):
        repo = self.repository(work_line_id)
        arrivals, faces, segments, opportunities, batches, drains = (
            repo.tables[k]
            for k in (
                "scan_arrival",
                "face_work",
                "feed_segment",
                "return_opportunity",
                "return_batch",
                "decision",
            )
        )
        active_faces = (
            faces.c.cycle_id.in_([j.id for j in jobs])
            | (faces.c.demand_state == "ACTIVE") & faces.c.cycle_id.is_(None)
            | (faces.c.feed_id.is_not(None) & (faces.c.feed_state != "COMPLETED"))
        )
        active_feeds = select(faces.c.feed_id).where(*repo.scoped(faces), active_faces)
        active_services = select(faces.c.service_id).where(*repo.scoped(faces), active_faces)
        active_opportunities = ~opportunities.c.ended | opportunities.c.service_id.in_(
            active_services
        )
        selected_opportunities = select(opportunities.c.id).where(
            *repo.scoped(opportunities), active_opportunities
        )
        # A retired FIFO visit can remain part of a frozen unresolved return.
        relevant_rounds = {
            m["round_id"]
            for members in await session.scalars(
                select(batches.c.members).where(*repo.scoped(batches), batches.c.pending)
            )
            for m in members
        }
        live_rounds = select(arrivals.c.round_id).where(
            *repo.scoped(arrivals),
            arrivals.c.removed.is_(False),
            arrivals.c.return_state != "COMPLETED",
        )
        subjects = (
            (
                arrivals,
                arrivals.c.pending
                | (arrivals.c.release_state == "PENDING")
                | (arrivals.c.removed.is_(False) & (arrivals.c.return_state != "COMPLETED"))
                | arrivals.c.round_id.in_(live_rounds)
                | arrivals.c.round_id.in_(relevant_rounds),
            ),
            (faces, active_faces),
            (segments, segments.c.pending | segments.c.feed_id.in_(active_feeds)),
            (opportunities, active_opportunities),
            (batches, batches.c.pending | batches.c.opportunity_id.in_(selected_opportunities)),
            (
                drains,
                drains.c.pending
                | drains.c.id.in_(
                    [j.origin.source_ref for j in jobs if j.origin.source_kind == "ROLLER_DRAIN"]
                ),
            ),
        )
        for table, condition in subjects:
            await session.execute(
                select(*table.primary_key)
                .where(*repo.scoped(table), condition)
                .order_by(*table.primary_key)
                .with_for_update()
            )

    async def original_arrival(self, session: AsyncSession, arrival_id: str) -> RowMapping:
        return await self.original_record(session, "scan_arrival", arrival_id)

    async def original_record(
        self, session: AsyncSession, key: str, record_id: str, *, for_update: bool = False
    ) -> RowMapping:
        if key in {"supply_demand", "face_service", "feed"}:
            table = self.tables["face_work"]
            identity = {
                "supply_demand": table.c.demand_id,
                "face_service": table.c.service_id,
                "feed": table.c.feed_id,
            }[key]
            conditions = (identity == record_id,)
        elif key == "drain":
            table = self.tables["decision"]
            conditions = (table.c.kind == key.upper(), table.c.id == record_id)
        elif key == "scan2_release":
            table = self.tables["scan_arrival"]
            conditions = (table.c.release_record_id == record_id,)
        elif key == "return_round":
            table = self.tables["scan_arrival"]
            conditions = (table.c.point == "SCAN3", table.c.round_id == record_id)
        elif key in {
            "scan_arrival",
            "scan_fact",
            "position_fact",
            "feed_segment",
            "return_opportunity",
            "return_batch",
        }:
            table = self.tables[key]
            conditions = (table.c.id == record_id,)
        else:
            raise ValueError("a typed original roller identity is required")
        query = select(table).where(table.c.owner_domain == self.context.domain, *conditions)
        if for_update:
            query = query.with_for_update()
        row = (await session.execute(query)).mappings().one_or_none()
        if row is None:
            raise ValueError("original roller record is missing")
        return row

    async def apply_supply_change(self, session: AsyncSession, change: SupplyChange) -> None:
        await self.repository(change.work_line_id).apply_supply_change(session, change)

    async def get_supply_progress(
        self, session: AsyncSession, *, work_line_id: int, task_id: str
    ) -> tuple[SupplyProgress, ...]:
        return await self.repository(work_line_id).supply_progress(session, task_id)

    def pending_feed_query(self):
        demand = self.tables["face_work"]
        return select(demand.c.demand_id.label("id")).where(
            demand.c.owner_domain == self.context.domain,
            demand.c.purpose == "SUPPLY",
            demand.c.service_state.is_distinct_from("WITHDRAWN"),
            demand.c.feed_state.is_distinct_from("COMPLETED"),
            or_(
                demand.c.demand_state == "ACTIVE",
                demand.c.feed_id.is_not(None),
                demand.c.preparation_action_id.is_not(None),
            ),
        )

    def pending_query(self, kind: str):
        if kind == "FEED":
            return self.pending_feed_query()
        if kind in {"RETURN_BATCH", "DRAIN"}:
            table = self.tables["decision" if kind == "DRAIN" else kind.lower()]
            query = select(table.c.id).where(
                table.c.owner_domain == self.context.domain, table.c.pending.is_(True)
            )
            return query.where(table.c.kind == "DRAIN") if kind == "DRAIN" else query
        table = self.tables["scan_arrival"]
        if kind == "SCAN2_RELEASE":
            return select(table.c.release_record_id.label("id")).where(
                table.c.owner_domain == self.context.domain, table.c.release_state == "PENDING"
            )
        if kind == "SCAN":
            root = table.alias("original_round")
            return select(table.c.id).where(
                table.c.owner_domain == self.context.domain,
                or_(
                    table.c.pending.is_(True)
                    & ((table.c.point != "SCAN4") | table.c.round_id.is_(None)),
                    (table.c.point == "SCAN4")
                    & select(root.c.id)
                    .where(
                        root.c.point == "SCAN3",
                        root.c.round_id == table.c.round_id,
                        root.c.owner_domain == table.c.owner_domain,
                        root.c.work_line_id == table.c.work_line_id,
                        root.c.removed.is_(False),
                        root.c.return_state != "COMPLETED",
                    )
                    .exists(),
                ),
            )
        raise ValueError("unsupported roller work kind")

    async def get_pending_upper_id(self, session: AsyncSession, *, kind: str) -> str | None:
        if kind == "RACK_CYCLE":
            return await self.rack_dispatch.pending_upper_id(session, source_kinds=ROLLER_SOURCES)
        pending = self.pending_query(kind).subquery()
        return await session.scalar(select(func.max(pending.c.id)))

    async def list_pending(
        self, session: AsyncSession, *, kind: str, after_id: str | None, upper_id: str, limit: int
    ) -> tuple[RollerWorkRef, ...]:
        if limit < 1:
            raise ValueError("invalid roller work page")
        if kind == "RACK_CYCLE":
            ids = await self.rack_dispatch.pending_ids(
                session,
                source_kinds=ROLLER_SOURCES,
                after_id=after_id,
                upper_id=upper_id,
                limit=limit,
            )
            return tuple(RollerWorkRef(kind, id_) for id_ in ids)
        table = self.pending_query(kind).subquery()
        query = select(table.c.id).where(table.c.id <= upper_id)
        if after_id is not None:
            query = query.where(table.c.id > after_id)
        ids = await session.scalars(query.order_by(table.c.id).limit(limit))
        return tuple(RollerWorkRef(kind, record_id) for record_id in ids)

    async def record_feed_success(
        self, session: AsyncSession, *, feed_id: str, segment_id: str, result: TransportTaskResult
    ) -> str:
        from .feed_flow import record_feed_success

        return await record_feed_success(
            self, session, feed_id=feed_id, segment_id=segment_id, result=result
        )

    async def accept_scan(self, ctx: EcsEventContext, payload: ScanCompletedData) -> None:
        await accept_scan(self, ctx, payload)

    async def release_scan2(self, session: AsyncSession, *, arrival_id: str) -> str:
        return await release_scan2(self, session, arrival_id=arrival_id)

    async def get_scan2_release_result(
        self, session: AsyncSession, *, arrival_id: str
    ) -> CommandView | None:
        arrival = await self.original_arrival(session, arrival_id)
        if arrival["point"] != "SCAN2":
            raise ValueError("original SCAN2 arrival required")
        return await get_scan_result(self, session, arrival["release_command_code"])

    async def get_scan_result(
        self, session: AsyncSession, *, arrival_id: str
    ) -> CommandView | None:
        arrival = await self.original_arrival(session, arrival_id)
        return await get_scan_result(self, session, arrival["command_code"])

    async def advance(self, session: AsyncSession, work: RollerWorkRef) -> str:
        from .rack_flow import advance_cycle, select_supply_cycle

        cycle_id = None
        line_id = None
        if work.kind == "RETURN_BATCH":
            from .return_flow import advance_return_batch

            progress = await advance_return_batch(self, session, work.record_id)
            batch = await self.original_record(session, "return_batch", work.record_id)
            opportunity = await self.original_record(
                session, "return_opportunity", batch["opportunity_id"]
            )
            service = await self.original_record(session, "face_service", opportunity["service_id"])
            cycle_id = service["cycle_id"]
        elif work.kind == "FEED":
            from .feed_flow import advance_feed

            progress = await advance_feed(self, session, work.record_id)
            cycle_id = await select_supply_cycle(self, session, work.record_id)
            demand = await self.original_record(session, "supply_demand", work.record_id)
            line_id = demand["work_line_id"]
        elif work.kind == "RACK_CYCLE":
            cycle_id, progress = work.record_id, "WAITING_FOR_RACK"
        elif work.kind == "DRAIN":
            from .return_flow import advance_drain

            cycle_id = await advance_drain(self, session, work.record_id)
            drain = await self.original_record(session, "drain", work.record_id)
            line_id, progress = drain["work_line_id"], drain["status"]
        else:
            progress = await consume_scan_result(self, session, work)
            if work.kind == "SCAN":
                arrival = await self.original_arrival(session, work.record_id)
                line_id = arrival["work_line_id"] if arrival["point"] == "SCAN4" else None
        if cycle_id is not None:
            progress = await advance_cycle(self, session, cycle_id)
            cycle = await self.rack_dispatch.progress(session, job_id=cycle_id)
            line_id = cycle.origin.work_line_id
            if cycle.origin.source_kind == "ROLLER_DRAIN":
                from .return_flow import summarize_drain

                await summarize_drain(self, session, cycle.origin.source_ref)
        if line_id is not None:
            selected = await self.check_drain(session, line_id)
            if selected is not None and selected != cycle_id:
                await advance_cycle(self, session, selected)
        return progress

    async def read_resources(self, session, work_line_id) -> dict | None:
        """Check new-work configuration; original steps use their saved resources."""
        try:
            line = await read_workline(session, work_line_id)
        except AppError as error:
            if error.code != ErrorCode.NOT_FOUND:
                raise
            return None
        if line.business_code != self.context.domain or any(
            not line.position_bindings.get(key) for key in ("FIVE_RACK", "INLET", "OUTLET")
        ):
            return None
        return dict(
            workline_code=line.line_code,
            caller_workline_id=str(line.id),
            station_id=None,
            positions={
                key: line.position_bindings[key] for key in ("FIVE_RACK", "INLET", "OUTLET")
            },
        )

    async def check_drain(self, session, work_line_id):
        """Read the actual supply execution/evaluations and FIFO in this advance transaction."""
        from .rack_flow import select_supply_cycle
        from .return_flow import advance_drain, register_drain

        repo = self.repository(work_line_id)
        table = repo.tables["decision"]
        current = (
            (
                await session.execute(
                    select(table)
                    .where(*repo.scoped(table), table.c.kind == "DRAIN", table.c.pending.is_(True))
                    .order_by(table.c.id.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        if current is not None:
            selected = await advance_drain(self, session, current["id"])
            if selected is not None:
                return selected
            current = await self.original_record(session, "drain", current["id"])
            if current["status"] == "PENDING" or current["status"] == "READY":
                return None
            if current["status"] == "WAIT" and current["due_at"] > int(time() * 1000):
                return None
        original_cycles = await self.rack_dispatch.jobs(
            session, work_line_id=work_line_id, source_kinds=("ROLLER_ORDINARY",), pending=True
        )
        task = await self.production.read_current_task(session, work_line_id=work_line_id)
        if task.is_valid:
            demands = repo.tables["face_work"]
            demand = (
                (
                    await session.execute(
                        select(demands)
                        .where(
                            *repo.scoped(demands),
                            demands.c.task_id == task.task_id,
                            demands.c.demand_state == "ACTIVE",
                            demands.c.service_id.is_(None),
                        )
                        .order_by(demands.c.id)
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if demand is not None:
                selected = await select_supply_cycle(self, session, demand["id"])
                if selected is not None:
                    return selected
        services = repo.tables["face_work"]
        if await session.scalar(
            select(services.c.id)
            .where(
                *repo.scoped(services),
                services.c.service_state.in_(("PENDING", "READY")),
                services.c.cycle_id.in_([c.id for c in original_cycles]),
            )
            .limit(1)
        ):
            return None
        candidates = await self.return_candidates(session, work_line_id=work_line_id)
        successor = await self.production.read_successor(session, work_line_id=work_line_id)
        if successor.state != "NONE" or not candidates:
            return None
        if current is None:
            current = (
                (
                    await session.execute(
                        select(table)
                        .where(*repo.scoped(table), table.c.kind == "DRAIN")
                        .order_by(table.c.id.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
        await register_drain(
            self,
            session,
            repo,
            current,
            candidates,
            successor,
        )
        return None

    async def get_return_candidates(
        self, session: AsyncSession, *, work_line_id: int
    ) -> tuple[RowMapping, ...]:
        return await self.return_candidates(session, work_line_id=work_line_id, limit=4)

    async def return_candidates(self, session, *, work_line_id, limit=None):
        from .scan_flow import applied_result

        repo = self.repository(work_line_id)
        candidates = []
        for round_ in await repo.return_queue(session):
            if round_["status"] != "PENDING":
                break
            attempts = await repo.point_attempts(session, round_["id"], "SCAN4")
            if not attempts:
                break
            arrival = attempts[-1]
            code = arrival["command_code"]
            result = applied_result(
                await get_scan_result(self, session, code), code, arrival["device_code"]
            )
            if result is None or result.result != "SUCCESS":
                break
            candidates.append(round_)
            if limit is not None and len(candidates) == limit:
                break
        return tuple(candidates)

    async def return_channel_empty(self, session: AsyncSession, *, work_line_id: int) -> bool:
        repo = self.repository(work_line_id)
        if await repo.return_queue(session):
            return False
        arrivals = repo.tables["scan_arrival"]
        if await session.scalar(
            select(arrivals.c.id)
            .where(
                *repo.scoped(arrivals),
                repo.current_round_condition(),
                arrivals.c.first_scan4_arrival_id.is_(None),
                arrivals.c.removed.is_(False),
                arrivals.c.return_state != "COMPLETED",
            )
            .limit(1)
        ):
            return False
        # A new SCAN3 visit can retire an older FIFO entry before its frozen return is resolved.
        batches = repo.tables["return_batch"]
        return not await session.scalar(
            select(batches.c.id).where(*repo.scoped(batches), batches.c.pending.is_(True)).limit(1)
        )

    async def start_return_batch(
        self,
        session: AsyncSession,
        *,
        opportunity_id: str,
        source_ref: str,
        workline_code: str,
        caller: TransportCaller,
        source: HandoffPosition,
    ) -> str | None:
        from .return_flow import start_return_batch

        return await start_return_batch(
            self,
            session,
            opportunity_id=opportunity_id,
            source_ref=source_ref,
            workline_code=workline_code,
            caller=caller,
            source=source,
        )

    async def record_return_outcome(
        self,
        session: AsyncSession,
        *,
        service_id: str,
        opportunity_id: str,
        batch_id: str | None,
        reason: str,
        evidence: dict,
    ) -> str:
        from .return_flow import record_return_outcome

        return await record_return_outcome(
            self,
            session,
            service_id=service_id,
            opportunity_id=opportunity_id,
            batch_id=batch_id,
            reason=reason,
            evidence=evidence,
        )
