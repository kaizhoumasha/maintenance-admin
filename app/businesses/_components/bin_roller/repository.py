"""Original component identities; every write uses the caller's transaction."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import EllipsisType

from sqlalchemy import Table, and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.identifiers import uuid7
from app.core.strict_json import canonical_digest

from .models import TABLES
from .ports import SourceFaceRef, SupplyChange, SupplyProgress


class RollerRepository:
    def __init__(
        self, owner_domain: str, work_line_id: int, *, tables: Mapping[str, Table] | None = None
    ) -> None:
        self.owner_domain = owner_domain
        self.work_line_id = work_line_id
        self.tables = TABLES if tables is None else tables

    @property
    def scope(self) -> dict[str, str | int]:
        return dict(owner_domain=self.owner_domain, work_line_id=self.work_line_id)

    def scoped(self, table: Table) -> tuple[ColumnElement[bool], ...]:
        return (
            table.c.owner_domain == self.owner_domain,
            table.c.work_line_id == self.work_line_id,
        )

    async def _one(
        self, session: AsyncSession, table: Table, *conditions: ColumnElement[bool]
    ) -> RowMapping | None:
        return (
            (await session.execute(select(table).where(*self.scoped(table), *conditions)))
            .mappings()
            .one_or_none()
        )

    async def _register(
        self,
        session: AsyncSession,
        table: Table,
        values: dict,
        keys: Sequence[str],
        compare: Sequence[str],
    ) -> RowMapping:
        insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
        await session.execute(insert(table).values(**values).on_conflict_do_nothing())
        row = await self._one(session, table, *(table.c[key] == values[key] for key in keys))
        if row is None:
            raise ValueError("original business identity is not visible in this transaction")
        if any(row[key] != values[key] for key in compare):
            raise ValueError("original business input cannot change")
        return row

    async def apply_supply_change(self, session: AsyncSession, change: SupplyChange) -> None:
        table = self.tables["supply_demand"]
        for source in change.added:
            await self._register(
                session,
                table,
                dict(
                    id=str(uuid7()),
                    **self.scope,
                    task_id=change.task_id,
                    change_id=change.change_id,
                    plan_revision=source.plan_revision,
                    rack_id=source.rack_id,
                    rack_face=source.rack_face,
                    withdrawn=False,
                ),
                ("task_id", "plan_revision", "rack_id", "rack_face"),
                (),
            )
        for source in change.withdrawn:
            await session.execute(
                table.update()
                .where(
                    *self.scoped(table),
                    table.c.task_id == change.task_id,
                    table.c.plan_revision == source.plan_revision,
                    table.c.rack_id == source.rack_id,
                    table.c.rack_face == source.rack_face,
                )
                .values(withdrawn=True)
            )

    async def demand(self, session: AsyncSession, demand_id: str) -> RowMapping | None:
        table = self.tables["supply_demand"]
        return await self._one(session, table, table.c.id == demand_id)

    async def supply_started(self, session: AsyncSession, demand: RowMapping) -> bool:
        if demand["service_id"]:
            table = self.tables["feed"]
            if await self._one(session, table, table.c.service_id == demand["service_id"]):
                return True
        if demand["cycle_id"]:
            table = self.tables["rack_action"]
            return (
                await self._one(
                    session, table, table.c.cycle_id == demand["cycle_id"], table.c.kind == "CTU01"
                )
                is not None
            )
        return False

    async def bind_supply(
        self, session: AsyncSession, *, demand_id: str, cycle_id: str, service_id: str | None = None
    ) -> None:
        demand = await self.demand(session, demand_id)
        cycle = await self.get_cycle(session, cycle_id)
        if (
            demand is None
            or cycle is None
            or (
                cycle["task_id"] != demand["task_id"]
                or cycle["rack_id"] != demand["rack_id"]
                or cycle["purpose"] != "ORDINARY"
                or (demand["cycle_id"] is not None and demand["cycle_id"] != cycle_id)
                or (
                    service_id is not None
                    and demand["service_id"] is not None
                    and demand["service_id"] != service_id
                )
            )
        ):
            raise ValueError("original supply cycle association mismatch")
        if service_id is not None:
            table = self.tables["face_service"]
            service = await self._one(session, table, table.c.id == service_id)
            if service is None or any(
                service[key] != value
                for key, value in dict(
                    cycle_id=cycle_id,
                    source_ref=demand_id,
                    task_id=demand["task_id"],
                    plan_revision=demand["plan_revision"],
                    rack_face=demand["rack_face"],
                ).items()
            ):
                raise ValueError("original supply face association mismatch")
        table = self.tables["supply_demand"]
        await session.execute(
            table.update()
            .where(*self.scoped(table), table.c.id == demand_id)
            .values(cycle_id=cycle_id, service_id=service_id or demand["service_id"])
        )

    async def supply_progress(
        self, session: AsyncSession, task_id: str
    ) -> tuple[SupplyProgress, ...]:
        table, feeds = self.tables["supply_demand"], self.tables["feed"]
        rows = (
            (
                await session.execute(
                    select(table)
                    .where(*self.scoped(table), table.c.task_id == task_id)
                    .order_by(table.c.id)
                )
            )
            .mappings()
            .all()
        )
        progress = []
        for demand in rows:
            feed = (
                await self._one(session, feeds, feeds.c.service_id == demand["service_id"])
                if demand["service_id"]
                else None
            )
            original = [demand["id"]]
            original.extend(demand[key] for key in ("cycle_id", "service_id") if demand[key])
            if feed is not None:
                original.extend((feed["id"], feed["operation_id"]))
                if feed["message_id"] is not None:
                    original.append(str(feed["message_id"]))
                segments = self.tables["feed_segment"]
                members = (
                    (
                        await session.execute(
                            select(segments)
                            .where(*self.scoped(segments), segments.c.feed_id == feed["id"])
                            .order_by(segments.c.segment_index)
                        )
                    )
                    .mappings()
                    .all()
                )
                for segment in members:
                    original.extend(
                        segment[key]
                        for key in ("id", "client_request_id", "transport_task_id", "operation_id")
                        if segment[key] is not None
                    )
            progress.append(
                SupplyProgress(
                    task_id,
                    SourceFaceRef(demand["plan_revision"], demand["rack_id"], demand["rack_face"]),
                    bool(feed and feed["feed_complete"]),
                    bool(feed["pending"])
                    if feed
                    else (not demand["withdrawn"] or await self.supply_started(session, demand)),
                    tuple(original),
                )
            )
        return tuple(progress)

    async def accept_scan_fact(
        self,
        session: AsyncSession,
        *,
        digest: str,
        receipt_id: int,
        point: str,
        device_code: str,
        raw_bin_code: str,
        bin_code: str | None,
        scanned_at: int,
        separator_command_code: str | None = None,
        binding_snapshot: dict | None = None,
        previous_arrival: RowMapping | None | EllipsisType = ...,
    ) -> RowMapping:
        if point not in {"SCAN1", "SCAN2", "SCAN3", "SCAN4"}:
            raise ValueError("invalid roller scan point")
        facts, arrivals = self.tables["scan_fact"], self.tables["scan_arrival"]
        original = await self._one(session, facts, facts.c.digest == digest)
        content = dict(
            point=point,
            device_code=device_code,
            raw_bin_code=raw_bin_code,
            bin_code=bin_code,
            scanned_at=scanned_at,
        )
        if original is not None:
            if any(original[key] != value for key, value in content.items()):
                raise ValueError("original scan message cannot change")
            return await self._one(session, arrivals, arrivals.c.id == original["arrival_id"])
        # The caller's verified Result and predecessor must use the same original read basis.
        previous = (
            await self.latest_scan(session, point) if previous_arrival is ... else previous_arrival
        )
        same = previous is not None and all(
            previous[key] == content[key] for key in ("device_code", "raw_bin_code", "bin_code")
        )
        if not same:
            separator_command_code = None
        # Only the owning point's already-verified original command Result separates equal content.
        separated = separator_command_code is not None and (
            previous is None or separator_command_code != previous["separator_command_code"]
        )
        if same and not separated:
            arrival = previous
        else:
            previous_id = previous["id"] if previous is not None else None
            basis = dict(
                point=point,
                previous_arrival_id=previous_id,
                separator_command_code=separator_command_code,
                device_code=device_code,
                raw_bin_code=raw_bin_code,
                bin_code=bin_code,
            )
            values = dict(
                **self.scope,
                id=str(uuid7()),
                received_at=datetime.now(UTC),
                business_key=canonical_digest(basis),
                first_digest=digest,
                **basis,
                scanned_at=scanned_at,
                return_round_id=None,
                delegated=False,
                pending=True,
                command_code=None,
            )
            arrival = await self._register(
                session,
                arrivals,
                values,
                ["owner_domain", "work_line_id", "business_key"],
                tuple(basis),
            )
            if arrival["id"] == values["id"] and bin_code is not None:
                await self.record_position(
                    session,
                    object_kind="BIN",
                    object_code=bin_code,
                    location_code=point,
                    rack_face=None,
                    source_kind="SCAN",
                    source_id=arrival["id"],
                    source_revision=0,
                )
        await self._register(
            session,
            facts,
            dict(
                **self.scope,
                id=str(uuid7()),
                digest=digest,
                receipt_id=receipt_id,
                arrival_id=arrival["id"],
                binding_snapshot=binding_snapshot,
                **content,
            ),
            ["digest"],
            (*content, "arrival_id"),
        )
        return arrival

    async def latest_scan(self, session: AsyncSession, point: str) -> RowMapping | None:
        arrivals = self.tables["scan_arrival"]
        return (
            (
                await session.execute(
                    select(arrivals)
                    .where(
                        *self.scoped(arrivals),
                        arrivals.c.point == point,
                    )
                    .order_by(arrivals.c.received_at.desc(), arrivals.c.id.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )

    async def update_scan(self, session: AsyncSession, arrival_id: str, **values) -> None:
        table = self.tables["scan_arrival"]
        await session.execute(
            table.update().where(*self.scoped(table), table.c.id == arrival_id).values(**values)
        )

    async def bind_scan_round(
        self, session: AsyncSession, arrival: RowMapping, *, valid: bool
    ) -> RowMapping | None:
        rounds = self.tables["return_round"]
        if arrival["return_round_id"] is not None:
            return await self._one(session, rounds, rounds.c.id == arrival["return_round_id"])
        if not valid:
            return None
        if arrival["point"] == "SCAN3":
            round_ = await self._register(
                session,
                rounds,
                dict(
                    **self.scope,
                    id=str(uuid7()),
                    scan3_arrival_id=arrival["id"],
                    scan4_arrival_id=None,
                    bin_code=arrival["bin_code"],
                    status="PENDING",
                    removed=False,
                ),
                ["owner_domain", "work_line_id", "scan3_arrival_id"],
                ["bin_code"],
            )
        else:
            round_ = await self._one(
                session,
                rounds,
                rounds.c.bin_code == arrival["bin_code"],
                self.current_round_condition(),
                rounds.c.removed.is_(False),
                rounds.c.status != "COMPLETED",
            )
            if round_ is None:
                return None
        await self.update_scan(session, arrival["id"], return_round_id=round_["id"])
        if arrival["point"] == "SCAN4" and round_["scan4_arrival_id"] is None:
            await session.execute(
                rounds.update()
                .where(*self.scoped(rounds), rounds.c.id == round_["id"])
                .values(scan4_arrival_id=arrival["id"])
            )
        return round_

    async def point_attempts(
        self, session: AsyncSession, round_id: str, point: str
    ) -> Sequence[RowMapping]:
        table = self.tables["scan_arrival"]
        return (
            (
                await session.execute(
                    select(table)
                    .where(
                        *self.scoped(table),
                        table.c.return_round_id == round_id,
                        table.c.point == point,
                        table.c.command_code.is_not(None),
                    )
                    .order_by(table.c.received_at, table.c.id)
                )
            )
            .mappings()
            .all()
        )

    async def current_position(
        self, session: AsyncSession, object_kind: str, object_code: str
    ) -> RowMapping | None:
        table = self.tables["position_fact"]
        return await self._one(
            session,
            table,
            table.c.object_kind == object_kind,
            table.c.object_code == object_code,
            table.c.is_current.is_(True),
        )

    async def position_at(
        self, session: AsyncSession, object_kind: str, location_code: str
    ) -> RowMapping | None:
        table = self.tables["position_fact"]
        return await self._one(
            session,
            table,
            table.c.object_kind == object_kind,
            table.c.location_code == location_code,
            table.c.is_current.is_(True),
        )

    async def record_position(
        self,
        session: AsyncSession,
        *,
        object_kind: str,
        object_code: str,
        location_code: str,
        rack_face: str | None,
        source_kind: str,
        source_id: str,
        source_revision: int,
        history_only: bool = False,
    ) -> str:
        table = self.tables["position_fact"]
        original = await self._one(
            session,
            table,
            table.c.source_kind == source_kind,
            table.c.source_id == source_id,
            table.c.source_revision == source_revision,
        )
        values = dict(
            **self.scope,
            id=str(uuid7()),
            object_kind=object_kind,
            object_code=object_code,
            location_code=location_code,
            rack_face=rack_face,
            source_kind=source_kind,
            source_id=source_id,
            source_revision=source_revision,
        )
        if original is not None:
            if any(
                original[key] != values[key]
                for key in ("object_kind", "object_code", "location_code", "rack_face")
            ):
                raise ValueError("original position fact cannot change")
            return "HISTORY_ONLY"
        newer = await session.scalar(
            select(table.c.id)
            .where(
                *self.scoped(table),
                table.c.source_kind == source_kind,
                table.c.source_id == source_id,
                table.c.source_revision > source_revision,
            )
            .limit(1)
        )
        history_only = history_only or newer is not None
        if not history_only:
            await session.execute(
                table.update()
                .where(
                    *self.scoped(table),
                    table.c.object_kind == object_kind,
                    table.c.is_current.is_(True),
                    or_(table.c.object_code == object_code, table.c.location_code == location_code),
                )
                .values(is_current=False)
            )
        await session.execute(table.insert().values(**values, is_current=not history_only))
        return "HISTORY_ONLY" if history_only else "UPDATED"

    async def create_cycle(
        self,
        session: AsyncSession,
        *,
        cycle_id: str,
        rack_id: str,
        purpose: str,
        task_id: str | None,
        source_ref: str,
        resources: dict | None = None,
    ) -> RowMapping:
        if purpose not in {"ORDINARY", "DRAIN"}:
            raise ValueError("invalid rack service purpose")
        table = self.tables["rack_cycle"]
        return await self._register(
            session,
            table,
            dict(
                **self.scope,
                id=cycle_id,
                rack_id=rack_id,
                purpose=purpose,
                task_id=task_id,
                source_ref=source_ref,
                resources=resources,
                status="ACTIVE",
                pending=True,
            ),
            ["owner_domain", "work_line_id", "source_ref", "rack_id"],
            ["purpose", "task_id"],
        )

    async def get_cycle(self, session: AsyncSession, cycle_id: str) -> RowMapping | None:
        table = self.tables["rack_cycle"]
        return await self._one(session, table, table.c.id == cycle_id)

    async def register_rack_action(
        self,
        session: AsyncSession,
        *,
        cycle_id: str,
        kind: str,
        source_ref: str,
        action_id: str,
        client_request_id: str,
        transport_task_id: str | None,
        operation_id: str | None,
        target: dict,
        caller_workline_id: str,
        target_face: str | None = None,
        station_id: str | None = None,
    ) -> RowMapping:
        if kind not in {"CTU01", "CTU02", "CTU03"}:
            raise ValueError("invalid rack action")
        if await self.get_cycle(session, cycle_id) is None:
            raise ValueError("rack action requires its original cycle")
        table = self.tables["rack_action"]
        return await self._register(
            session,
            table,
            dict(
                **self.scope,
                id=action_id,
                cycle_id=cycle_id,
                kind=kind,
                source_ref=source_ref,
                client_request_id=client_request_id,
                transport_task_id=transport_task_id,
                operation_id=operation_id,
                target=target,
                target_face=target_face,
                station_id=station_id,
                caller_workline_id=caller_workline_id,
                status="PENDING",
                pending=True,
            ),
            ["owner_domain", "work_line_id", "cycle_id", "kind"]
            + (["source_ref"] if kind == "CTU02" else []),
            ["source_ref", "target", "target_face", "station_id", "caller_workline_id"],
        )

    async def get_action(self, session: AsyncSession, action_id: str) -> RowMapping | None:
        table = self.tables["rack_action"]
        return await self._one(session, table, table.c.id == action_id)

    async def bind_transport(
        self, session: AsyncSession, *, action_id: str, transport_task_id: str, operation_id: str
    ) -> None:
        action = await self.get_action(session, action_id)
        if action is None:
            raise ValueError("original rack action missing")
        for key, value in dict(
            transport_task_id=transport_task_id, operation_id=operation_id
        ).items():
            if action[key] is not None and action[key] != value:
                raise ValueError("original Transport identity cannot change")
        table = self.tables["rack_action"]
        await session.execute(
            table.update()
            .where(*self.scoped(table), table.c.id == action_id)
            .values(transport_task_id=transport_task_id, operation_id=operation_id)
        )

    def current_round_condition(self) -> ColumnElement[bool]:
        rounds = self.tables["return_round"]
        arrivals = self.tables["scan_arrival"].alias("current_scan3")
        # A later accepted SCAN3 changes physical membership, never the old fulfillment facts.
        latest = (
            select(arrivals.c.return_round_id)
            .where(
                *self.scoped(arrivals),
                arrivals.c.point == "SCAN3",
                arrivals.c.bin_code == rounds.c.bin_code,
                arrivals.c.raw_bin_code.endswith("-B"),
            )
            .order_by(arrivals.c.received_at.desc(), arrivals.c.id.desc())
            .limit(1)
            .correlate(rounds)
        )
        return and_(
            rounds.c.id == latest.scalar_subquery(),
            rounds.c.scan3_arrival_id == latest.with_only_columns(arrivals.c.id).scalar_subquery(),
        )

    async def return_queue(self, session: AsyncSession) -> Sequence[RowMapping]:
        rounds, arrivals = self.tables["return_round"], self.tables["scan_arrival"]
        scan3 = arrivals.alias("later_scan3")
        later_visit = (
            select(scan3.c.id)
            .where(
                *self.scoped(scan3),
                scan3.c.point == "SCAN3",
                scan3.c.bin_code == arrivals.c.bin_code,
                scan3.c.raw_bin_code.endswith("-B"),
                or_(
                    scan3.c.received_at > arrivals.c.received_at,
                    and_(scan3.c.received_at == arrivals.c.received_at, scan3.c.id > arrivals.c.id),
                ),
            )
            .correlate(arrivals)
            .exists()
        )
        return (
            (
                await session.execute(
                    select(rounds)
                    .select_from(
                        arrivals.outerjoin(
                            rounds,
                            and_(
                                arrivals.c.id == rounds.c.scan4_arrival_id,
                                rounds.c.owner_domain == arrivals.c.owner_domain,
                                rounds.c.work_line_id == arrivals.c.work_line_id,
                            ),
                        )
                    )
                    .where(
                        *self.scoped(arrivals),
                        arrivals.c.point == "SCAN4",
                        or_(
                            and_(
                                rounds.c.removed.is_(False),
                                rounds.c.status != "COMPLETED",
                                self.current_round_condition(),
                            ),
                            and_(
                                arrivals.c.return_round_id.is_(None),
                                arrivals.c.pending.is_(True),
                                ~later_visit,
                            ),
                        ),
                    )
                    .order_by(arrivals.c.received_at, arrivals.c.id)
                )
            )
            .mappings()
            .all()
        )
