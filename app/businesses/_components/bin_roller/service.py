"""One business-owned component; every operation uses its caller's transaction."""

from collections.abc import Mapping
from time import time

from sqlalchemy import Table, func, or_, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

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


class BinRoller:
    def __init__(
        self,
        context: CommandOwner,
        capabilities: Capabilities,
        production: Production,
        *,
        tables: Mapping[str, Table] | None = None,
    ) -> None:
        self.context = context
        self.capabilities = capabilities
        self.production = production
        self.tables = TABLES if tables is None else tables

    def repository(self, work_line_id: int) -> RollerRepository:
        return RollerRepository(self.context.domain, work_line_id, tables=self.tables)

    async def original_arrival(self, session: AsyncSession, arrival_id: str) -> RowMapping:
        return await self.original_record(session, "scan_arrival", arrival_id)

    async def original_record(self, session: AsyncSession, key: str, record_id: str) -> RowMapping:
        table = self.tables[key]
        row = (
            (
                await session.execute(
                    select(table).where(
                        table.c.owner_domain == self.context.domain, table.c.id == record_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
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
        demand, feed, action = (
            self.tables[key] for key in ("supply_demand", "feed", "rack_action")
        )
        original_feed = select(feed.c.id).where(
            feed.c.owner_domain == demand.c.owner_domain,
            feed.c.work_line_id == demand.c.work_line_id,
            feed.c.service_id == demand.c.service_id,
        )
        original_entry = select(action.c.id).where(
            action.c.owner_domain == demand.c.owner_domain,
            action.c.work_line_id == demand.c.work_line_id,
            action.c.cycle_id == demand.c.cycle_id,
            action.c.kind == "CTU01",
        )
        return select(demand.c.id).where(
            demand.c.owner_domain == self.context.domain,
            ~original_feed.where(feed.c.feed_complete.is_(True)).exists(),
            or_(demand.c.withdrawn.is_(False), original_feed.exists(), original_entry.exists()),
        )

    def pending_query(self, kind: str):
        if kind == "FEED":
            return self.pending_feed_query(), self.tables["supply_demand"]
        if kind in {"RETURN_BATCH", "RACK_CYCLE", "DRAIN", "SCAN2_RELEASE"}:
            table = self.tables[kind.lower()]
            return select(table.c.id).where(
                table.c.owner_domain == self.context.domain, table.c.pending.is_(True)
            ), table
        if kind == "SCAN":
            table, rounds = self.tables["scan_arrival"], self.tables["return_round"]
            return select(table.c.id).where(
                table.c.owner_domain == self.context.domain,
                or_(
                    (table.c.point != "SCAN4") & table.c.pending.is_(True),
                    (table.c.point == "SCAN4")
                    & table.c.return_round_id.is_(None)
                    & table.c.pending.is_(True),
                    (table.c.point == "SCAN4")
                    & select(rounds.c.id)
                    .where(
                        rounds.c.id == table.c.return_round_id,
                        rounds.c.owner_domain == table.c.owner_domain,
                        rounds.c.work_line_id == table.c.work_line_id,
                        rounds.c.removed.is_(False),
                        rounds.c.status != "COMPLETED",
                    )
                    .exists(),
                ),
            ), table
        raise ValueError("unsupported roller work kind")

    async def get_pending_upper_id(self, session: AsyncSession, *, kind: str) -> str | None:
        query, _ = self.pending_query(kind)
        pending = query.subquery()
        return await session.scalar(select(func.max(pending.c.id)))

    async def list_pending(
        self, session: AsyncSession, *, kind: str, after_id: str | None, upper_id: str, limit: int
    ) -> tuple[RollerWorkRef, ...]:
        if limit < 1:
            raise ValueError("invalid roller work page")
        query, table = self.pending_query(kind)
        query = query.where(table.c.id <= upper_id)
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
        return await get_scan_result(self, session, arrival)

    async def get_scan_result(
        self, session: AsyncSession, *, arrival_id: str
    ) -> CommandView | None:
        return await get_scan_result(
            self, session, await self.original_arrival(session, arrival_id)
        )

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
            cycle = await self.original_record(session, "rack_cycle", cycle_id)
            line_id = cycle["work_line_id"]
            if cycle["purpose"] == "DRAIN":
                from .return_flow import summarize_drain

                await summarize_drain(self, session, cycle["source_ref"])
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
        table, cycles = repo.tables["drain"], repo.tables["rack_cycle"]
        current = (
            (
                await session.execute(
                    select(table)
                    .where(*repo.scoped(table), table.c.pending.is_(True))
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
        original_cycles = (
            (
                await session.execute(
                    select(cycles).where(*repo.scoped(cycles), cycles.c.status == "ACTIVE")
                )
            )
            .mappings()
            .all()
        )
        task = await self.production.read_current_task(session, work_line_id=work_line_id)
        if task.is_valid:
            demands = repo.tables["supply_demand"]
            demand = (
                (
                    await session.execute(
                        select(demands)
                        .where(
                            *repo.scoped(demands),
                            demands.c.task_id == task.task_id,
                            demands.c.withdrawn.is_(False),
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
        services = repo.tables["face_service"]
        if await session.scalar(
            select(services.c.id)
            .where(
                *repo.scoped(services),
                services.c.pending.is_(True),
                services.c.cycle_id.in_(
                    [c["id"] for c in original_cycles if c["purpose"] == "ORDINARY"]
                ),
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
                        .where(*repo.scoped(table))
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
            result = applied_result(await get_scan_result(self, session, arrival), arrival)
            if result is None or result.result != "SUCCESS":
                break
            candidates.append(round_)
            if limit is not None and len(candidates) == limit:
                break
        return tuple(candidates)

    async def return_channel_empty(self, session: AsyncSession, *, work_line_id: int) -> bool:
        return not await self.repository(work_line_id).return_queue(session)

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
