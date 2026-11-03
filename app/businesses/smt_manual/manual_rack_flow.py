"""Original outbound rack work; all writes use the caller's transaction."""

from datetime import UTC, datetime, timedelta
from time import time

from sqlalchemy import func, or_, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.businesses.runtime import Capabilities
from app.core.identifiers import uuid7, validate_uuid7
from app.integrations.wms_callback import Accepted, CallbackContext, Outcome, Rejected, Unavailable
from app.integrations.wms_transport import (
    Rack,
    RackPosition,
    TransportCaller,
    TransportTaskResult,
    Zone,
)
from app.models import WorkLine

from .contracts import DirectPickCompleted, ReturnRackCompleted
from .models import RACK_COMPLETION_OPERATIONS
from .repository import TaskRepository


class ManualRackFlow:
    def __init__(self, capabilities: Capabilities, *, repository: TaskRepository | None = None):
        self.capabilities = capabilities
        self.repository = repository or TaskRepository()
        self.tables = self.repository.tables

    async def reconcile_task(self, session: AsyncSession, *, task_id: str) -> None:
        """Apply saved completion scopes and register eligible original production entries."""
        line, task = await self.repository.original(session, task_id)
        if task is None:
            return
        await self.apply_completions(session, task=task)
        if (
            task["business_type"] != "outbound"
            or task["status"] != "EXECUTING"
            or line is None
            or not line.is_enabled
            or line.business_code != "smt_manual"
            or line.line_code != task["workline_code"]
        ):
            return
        racks, members = self.tables["rack"], self.tables["member"]
        for role in ("TRANSFER", "RETURN"):
            location = line.position_bindings.get(role + "_RACK")
            if not location:
                continue
            occupied = await self._one(
                session,
                "rack",
                racks.c.work_line_id == line.id,
                racks.c.role == role,
                racks.c.state.not_in(("DEPARTED", "ENTRY_CANCELLED")),
            )
            if occupied is not None and occupied["task_id"] != task_id:
                continue
            if role == "TRANSFER":
                target = task["target_rack"]
                if target is None or await self._one(
                    session, "rack", racks.c.task_id == task_id, racks.c.role == role
                ):
                    continue
                selected = []
                rack_id, face = target["rack_id"], target["rack_face"]
            else:
                query = (
                    select(members)
                    .where(
                        members.c.task_id == task_id,
                        members.c.kind == "DIRECT",
                        members.c.status == "PENDING",
                        members.c.rack_work_id.is_(None),
                    )
                    .order_by(members.c.plan_revision, members.c.member_order, members.c.id)
                )
                unbound = (await session.execute(query)).mappings().all()
                cancelled_racks = set(
                    await session.scalars(
                        select(racks.c.rack_id).where(
                            racks.c.task_id == task_id,
                            racks.c.role == role,
                            racks.c.state == "ENTRY_CANCELLED",
                        )
                    )
                )
                unbound = [m for m in unbound if m["rack_id"] not in cancelled_racks]
                if not unbound:
                    continue
                rack_id = occupied["rack_id"] if occupied else unbound[0]["rack_id"]
                selected = [m for m in unbound if m["rack_id"] == rack_id]
                if not selected:
                    continue
                face = selected[0]["rack_face"]
                if occupied and occupied["departure_operation_id"]:
                    continue
            if occupied is None:
                actions = self.tables["rack_action"]
                # Other lines' committed conflicts reserve only their known destination.
                conflict = await session.scalar(
                    select(actions.c.id)
                    .join(racks, racks.c.id == actions.c.rack_work_id)
                    .where(
                        racks.c.state != "DEPARTED",
                        actions.c.state == "POSITION_CONFLICT",
                        actions.c.actual_position["kind"].as_string() == "RACK_POSITION",
                        actions.c.actual_position["location_code"].as_string() == location,
                    )
                    .limit(1)
                )
                if conflict:
                    continue
                work_id = str(uuid7())
                await session.execute(
                    racks.insert().values(
                        id=work_id,
                        task_id=task_id,
                        work_line_id=line.id,
                        workline_code=task["workline_code"],
                        role=role,
                        rack_id=rack_id,
                        location_code=location,
                        initial_face=face,
                        caller_workline_id=str(line.id),
                        station_id=None,
                    )
                )
                occupied = await self._one(session, "rack", racks.c.id == work_id)
                await self._action(
                    session,
                    occupied,
                    kind="ENTER",
                    source_ref=work_id,
                    target=dict(type="RACK_POSITION", location_code=location),
                    face=face,
                )
            if selected:
                await session.execute(
                    members.update()
                    .where(members.c.id.in_([m["id"] for m in selected]))
                    .values(rack_work_id=occupied["id"])
                )

    async def _one(
        self, session: AsyncSession, key: str, *where, request_kind: str | None = None
    ) -> RowMapping | None:
        table = self.tables[key]
        if request_kind is not None:
            where = (*where, table.c.kind == request_kind)
        return (await session.execute(select(table).where(*where))).mappings().one_or_none()

    async def _save(
        self,
        session: AsyncSession,
        key: str,
        identity: str,
        *,
        request_kind: str | None = None,
        **values,
    ) -> None:
        table = self.tables[key]
        column = table.c.id if "id" in table.c else table.c.operation_id
        conditions = [column == identity]
        if request_kind is not None:
            conditions.append(table.c.kind == request_kind)
        await session.execute(table.update().where(*conditions).values(**values))

    async def get_pending_upper_id(
        self, session: AsyncSession, *, kind: str = "MANUAL_RACK"
    ) -> str | None:
        if kind != "MANUAL_RACK":
            raise ValueError("invalid manual rack pending kind")
        table = self.tables["rack"]
        return await session.scalar(select(func.max(table.c.id)).where(table.c.pending))

    async def list_pending(
        self,
        session: AsyncSession,
        *,
        upper_id: str,
        after_id: str | None = None,
        limit: int = 100,
        kind: str = "MANUAL_RACK",
    ) -> tuple[str, ...]:
        if kind != "MANUAL_RACK":
            raise ValueError("invalid manual rack pending kind")
        table = self.tables["rack"]
        query = select(table.c.id).where(table.c.pending, table.c.id <= upper_id)
        if after_id is not None:
            query = query.where(table.c.id > after_id)
        return tuple(await session.scalars(query.order_by(table.c.id).limit(limit)))

    async def has_report_obligations(self, session: AsyncSession, *, task_id: str) -> bool:
        """A registered RETURN entry owns its report before result consumption.

        Unstarted members and failed entries without arrival do not invent reports.
        This predicate never gates eligible rotation or departure.
        """
        racks, actions, reports = (self.tables[k] for k in ("rack", "rack_action", "request"))
        return bool(
            await session.scalar(
                select(actions.c.id)
                .join(racks, racks.c.id == actions.c.rack_work_id)
                .outerjoin(
                    reports,
                    (reports.c.kind == "RACK_ARRIVAL_REPORT")
                    & (reports.c.action_id == actions.c.id)
                    & (reports.c.rack_work_id == racks.c.id)
                    & (reports.c.task_id == racks.c.task_id)
                    & (reports.c.work_line_id == racks.c.work_line_id),
                )
                .where(
                    racks.c.task_id == task_id,
                    racks.c.role == "RETURN",
                    actions.c.kind == "ENTER",
                    or_(
                        actions.c.state.not_in(("FAILED", "CANCELLED")),
                        reports.c.operation_id.is_not(None),
                    ),
                    reports.c.state.is_distinct_from("CONSUMED"),
                )
                .limit(1)
            )
        )

    @staticmethod
    def _caller(work: RowMapping) -> TransportCaller:
        return TransportCaller(work["caller_workline_id"], work["station_id"])

    async def _action(
        self,
        session: AsyncSession,
        work: RowMapping,
        *,
        kind: str,
        source_ref: str,
        target: dict,
        face: str | None,
    ) -> RowMapping:
        actions = self.tables["rack_action"]
        original = await self._one(
            session,
            "rack_action",
            actions.c.rack_work_id == work["id"],
            actions.c.kind == kind,
            actions.c.source_ref == source_ref,
        )
        if original is not None:
            return original
        identity = str(uuid7())
        args = dict(
            client_request_id=str(uuid7()),
            caller=self._caller(work),
            rack_id=work["rack_id"],
            target_face=face,
            cycle_id=work["id"],
        )
        if kind == "ROTATE":
            handle = await self.capabilities.transport.rotate_rack(
                session,
                **args,
                position=RackPosition(target["location_code"]),
                rcs_template_id="CK04",
            )
        else:
            handle = await self.capabilities.transport.move_rack(
                session,
                **args,
                source=Rack(work["rack_id"])
                if kind == "ENTER"
                else RackPosition(work["location_code"]),
                target=(Zone if target["type"] == "ZONE" else RackPosition)(
                    target["location_code"]
                ),
                rcs_template_id="F01",
            )
        await session.execute(
            actions.insert().values(
                id=identity,
                rack_work_id=work["id"],
                kind=kind,
                source_ref=source_ref,
                client_request_id=handle.client_request_id,
                transport_task_id=handle.transport_task_id,
                target=target,
                target_face=face,
            )
        )
        return await self._one(session, "rack_action", actions.c.id == identity)

    async def _consume_action(
        self, session: AsyncSession, work: RowMapping, action: RowMapping
    ) -> None:
        result = await self.capabilities.transport.get_result(
            session,
            transport_task_id=action["transport_task_id"],
            client_request_id=action["client_request_id"],
            caller=self._caller(work),
        )
        if (
            result.availability != "FOUND"
            or result.owner_domain != "smt_manual"
            or result.cycle_id != work["id"]
        ):
            return
        observation = result.position_observation
        if observation and (action["observation_revision"] or 0) < observation["outcome_revision"]:
            projection = dict(
                state="POSITION_CONFLICT",
                observation_revision=observation["outcome_revision"],
                actual_position=observation["final_position"],
                actual_face=observation["arrival_face"],
            )
            await self._save(session, "rack_action", action["id"], **projection)
            action = {**action, **projection}
        outcome = result.outcome
        if outcome is None:
            return
        newer_observation = (
            observation is not None and observation["outcome_revision"] > result.outcome_revision
        )
        actual = observation if newer_observation else outcome
        successful = (
            outcome["status"] == "SUCCEEDED"
            and not newer_observation
            and not outcome["position_unknown"]
            and not result.has_pending_outcome
            and (
                action["target"]["type"] != "RACK_POSITION"
                or outcome["final_position"]
                == dict(kind="RACK_POSITION", location_code=action["target"]["location_code"])
            )
            and (action["target_face"] is None or outcome["arrival_face"] == action["target_face"])
            and not (
                action["kind"] == "DEPART"
                and outcome["final_position"]
                == dict(kind="RACK_POSITION", location_code=work["location_code"])
            )
        )
        position_conflict = (
            action["kind"] == "DEPART"
            and outcome["status"] == "SUCCEEDED"
            and not outcome["position_unknown"]
            and not successful
        )
        cancelled = outcome["status"] == "CANCELLED" and not outcome["position_unknown"]
        if cancelled and action["kind"] == "ENTER":
            position = outcome["final_position"]
            if position != dict(kind="RACK_POSITION", location_code=work["location_code"]):
                location = position["location_code"]
                bindings = await session.scalars(select(WorkLine.position_bindings))
                position_conflict = any(location in values.values() for values in bindings)
                if not position_conflict:
                    racks = self.tables["rack"]
                    position_conflict = bool(
                        await session.scalar(
                            select(racks.c.id)
                            .where(
                                racks.c.location_code == location,
                                racks.c.state.not_in(("DEPARTED", "ENTRY_CANCELLED")),
                            )
                            .limit(1)
                        )
                    )
        state = (
            "POSITION_CONFLICT"
            if newer_observation or result.has_pending_outcome or position_conflict
            else "SUCCEEDED"
            if successful
            else "POSITION_UNKNOWN"
            if outcome["position_unknown"]
            else "CANCELLED"
            if cancelled
            else "FAILED"
        )
        projection = dict(
            state=state,
            outcome_revision=result.outcome_revision,
            actual_position=actual["final_position"],
            actual_face=actual["arrival_face"],
        )
        if any(action[key] != value for key, value in projection.items()):
            await self._save(session, "rack_action", action["id"], **projection)
        if successful and action["kind"] == "ENTER" and work["role"] == "RETURN":
            await self._arrival_report(session, work, action, result)

    async def _arrival_report(
        self,
        session: AsyncSession,
        work: RowMapping,
        action: RowMapping,
        result: TransportTaskResult,
    ) -> None:
        from .request.outbound_return_rack_arrival_report_v1 import ArrivalReport

        reports = self.tables["request"]
        if await self._one(
            session,
            "request",
            reports.c.action_id == action["id"],
            reports.c.task_id == work["task_id"],
            reports.c.work_line_id == work["work_line_id"],
            reports.c.rack_work_id == work["id"],
            request_kind="RACK_ARRIVAL_REPORT",
        ):
            return
        operation_id = str(uuid7())
        payload = ArrivalReport(
            task_id=work["task_id"],
            transport_task_id=action["transport_task_id"],
            outcome_revision=result.outcome_revision,
            rack_id=work["rack_id"],
            final_position=dict(type="RACK_POSITION", location_code=work["location_code"]),
            arrival_face=result.outcome["arrival_face"],
        )
        reference = await self.capabilities.wms.send(session, payload, operation_id=operation_id)
        await session.execute(
            reports.insert().values(
                kind="RACK_ARRIVAL_REPORT",
                operation="outbound.return_rack.arrival_report@v1",
                operation_id=operation_id,
                task_id=work["task_id"],
                work_line_id=work["work_line_id"],
                request_snapshot=payload.model_dump(mode="json"),
                rack_work_id=work["id"],
                action_id=action["id"],
                message_id=reference.message_id,
            )
        )

    async def _consume_reports(self, session: AsyncSession, work: RowMapping) -> None:
        from .request.outbound_return_rack_arrival_report_v1 import OPERATION

        table = self.tables["request"]
        for report in (
            await session.execute(
                select(table).where(
                    table.c.rack_work_id == work["id"],
                    table.c.kind == "RACK_ARRIVAL_REPORT",
                    table.c.task_id == work["task_id"],
                    table.c.work_line_id == work["work_line_id"],
                    table.c.state != "CONSUMED",
                )
            )
        ).mappings():
            result = await self.capabilities.wms.get_result(
                session,
                message_id=report["message_id"],
                operation=OPERATION,
                operation_id=report["operation_id"],
            )
            if (
                result.availability == "FOUND"
                and result.response_state == "VALID"
                and (result.http_status, result.response_code)
                in {(200, "RECORDED"), (200, "DUPLICATE")}
                and result.response_data == {}
            ):
                await self._save(
                    session,
                    "request",
                    report["operation_id"],
                    request_kind="RACK_ARRIVAL_REPORT",
                    state="CONSUMED",
                    decision_code=result.response_code,
                    response=dict(
                        code=result.response_code,
                        timestamp=result.response_timestamp,
                        data=result.response_data,
                    ),
                )

    async def advance_rack(
        self, session: AsyncSession, *, rack_work_id: str, now_ms: int | None = None
    ) -> str:
        """Advance one original MANUAL_RACK in a caller-owned independent transaction."""
        now_ms = int(time() * 1000) if now_ms is None else now_ms
        racks, actions, members = (self.tables[k] for k in ("rack", "rack_action", "member"))
        work = await self._one(session, "rack", racks.c.id == rack_work_id)
        if work is None:
            return "NOT_FOUND"
        line, task = await self.repository.original(session, work["task_id"])
        work = (
            (
                await session.execute(
                    select(racks).where(racks.c.id == rack_work_id).with_for_update()
                )
            )
            .mappings()
            .one()
        )
        if task is None or task["work_line_id"] != work["work_line_id"]:
            return "OWNER_CONFLICT"
        await self.apply_completions(session, task=task)
        originals = (
            (
                await session.execute(
                    select(actions)
                    .where(actions.c.rack_work_id == rack_work_id)
                    .order_by(actions.c.id)
                )
            )
            .mappings()
            .all()
        )
        for action in originals:
            await self._consume_action(session, work, action)
        await self._consume_reports(session, work)
        latest = await self._one(session, "rack_action", actions.c.id == originals[-1]["id"])
        if latest["state"] not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return (
                latest["state"]
                if latest["state"] != "PENDING"
                else "WAITING_FOR_"
                + {"ENTER": "ENTRY", "ROTATE": "ROTATION", "DEPART": "DEPARTURE_RESULT"}[
                    latest["kind"]
                ]
            )
        if latest["kind"] == "DEPART":
            if latest["state"] != "SUCCEEDED":
                return latest["state"]
            predecessors = await self.capabilities.transport.list_unclosed_predecessors(
                session,
                caller=self._caller(work),
                cycle_id=work["id"],
                predecessor_ids=(latest["transport_task_id"],),
                rack_id=work["rack_id"],
            )
            if not predecessors.complete or predecessors.unresolved:
                return "WAITING_FOR_PREDECESSOR"
            reports = self.tables["request"]
            pending = bool(
                await session.scalar(
                    select(reports.c.operation_id)
                    .where(
                        reports.c.rack_work_id == rack_work_id,
                        reports.c.kind == "RACK_ARRIVAL_REPORT",
                        reports.c.task_id == work["task_id"],
                        reports.c.work_line_id == work["work_line_id"],
                        reports.c.state != "CONSUMED",
                    )
                    .limit(1)
                )
            )
            await self._save(session, "rack", rack_work_id, state="DEPARTED", pending=pending)
            return "DEPARTED"
        if latest["kind"] == "ENTER" and latest["state"] == "CANCELLED":
            predecessors = await self.capabilities.transport.list_unclosed_predecessors(
                session,
                caller=self._caller(work),
                cycle_id=work["id"],
                predecessor_ids=(latest["transport_task_id"],),
                rack_id=work["rack_id"],
            )
            if not predecessors.complete or predecessors.unresolved:
                return "WAITING_FOR_PREDECESSOR"
            if latest["actual_position"] != dict(
                kind="RACK_POSITION", location_code=work["location_code"]
            ):
                if work["state"] != "ENTRY_CANCELLED":
                    await self._save(session, "rack", rack_work_id, state="ENTRY_CANCELLED")
                return "ENTRY_CANCELLED"
            associated = (
                (await session.execute(select(members).where(members.c.rack_work_id == work["id"])))
                .mappings()
                .all()
            )
            if (
                work["role"] != "RETURN"
                or not associated
                or any(
                    m["status"] != "CANCELLED" or not m["cancelled_operation_id"]
                    for m in associated
                )
            ):
                return "RECOVERY_REQUIRED"
            return await self._departure(session, work, task, latest, now_ms)
        if latest["actual_position"] != dict(
            kind="RACK_POSITION", location_code=work["location_code"]
        ):
            return "POSITION_CONFLICT"
        entry = await self._one(session, "rack_action", actions.c.id == originals[0]["id"])
        if entry["kind"] != "ENTER" or entry["state"] != "SUCCEEDED":
            return "WAITING_FOR_ENTRY"
        predecessors = await self.capabilities.transport.list_unclosed_predecessors(
            session,
            caller=self._caller(work),
            cycle_id=work["id"],
            predecessor_ids=(latest["transport_task_id"],),
            rack_id=work["rack_id"],
        )
        if not predecessors.complete or predecessors.unresolved:
            return "WAITING_FOR_PREDECESSOR"
        if work["role"] == "TRANSFER":
            if task["status"] != "COMPLETED":
                return "WORKING"
            remaining = []
        else:
            remaining = (
                (
                    await session.execute(
                        select(members)
                        .where(
                            members.c.rack_work_id == work["id"],
                            members.c.status.in_(("PENDING", "STARTED")),
                        )
                        .order_by(members.c.plan_revision, members.c.member_order, members.c.id)
                    )
                )
                .mappings()
                .all()
            )
            if any(
                m["status"] == "STARTED" or m["rack_face"] == latest["actual_face"]
                for m in remaining
            ):
                return "WORKING" if latest["state"] == "SUCCEEDED" else latest["state"]
            if remaining:
                if latest["state"] != "SUCCEEDED":
                    return latest["state"]
                if (
                    line is None
                    or not line.is_enabled
                    or line.business_code != "smt_manual"
                    or task["status"] != "EXECUTING"
                    or line.position_bindings.get("RETURN_RACK") != work["location_code"]
                ):
                    return "NEW_WORK_BLOCKED"
                await self._action(
                    session,
                    work,
                    kind="ROTATE",
                    source_ref=remaining[0]["id"],
                    target=dict(type="RACK_POSITION", location_code=work["location_code"]),
                    face=remaining[0]["rack_face"],
                )
                return "WAITING_FOR_ROTATION"
        return await self._departure(session, work, task, latest, now_ms)

    async def _departure(
        self,
        session: AsyncSession,
        work: RowMapping,
        task: RowMapping,
        position: RowMapping,
        now_ms: int,
    ) -> str:
        from app.businesses._components.rack_dispatch.request.rack_departure_decide_v1 import (
            DECISION,
            OPERATION,
            DepartureRequest,
        )

        table = self.tables["request"]
        original = (
            (
                await session.execute(
                    select(table)
                    .where(
                        table.c.kind == "RACK_DEPARTURE",
                        table.c.rack_work_id == work["id"],
                        table.c.operation_id == work["departure_operation_id"],
                        table.c.task_id == work["task_id"],
                        table.c.work_line_id == work["work_line_id"],
                    )
                    .order_by(table.c.operation_id.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        if work["departure_operation_id"] is not None and original is None:
            return "WAITING_FOR_DEPARTURE"
        basis = dict(
            action_id=position["id"],
            revision=position["outcome_revision"],
            task_status=task["status"],
        )
        if original is not None:
            if original["response"] is None:
                result = await self.capabilities.wms.get_result(
                    session,
                    message_id=original["message_id"],
                    operation=OPERATION,
                    operation_id=original["operation_id"],
                )
                if (
                    result.availability != "FOUND"
                    or result.response_state != "VALID"
                    or (result.http_status, result.response_code) != (200, "DECIDED")
                ):
                    return "WAITING_FOR_DEPARTURE"
                try:
                    decision = DECISION.validate_python(result.response_data).model_dump(
                        mode="json"
                    )
                except ValueError:
                    return "INVALID_DEPARTURE"
                if (
                    decision.get("rack_destination")
                    == original["request_snapshot"]["current_location"]
                ):
                    return "INVALID_DEPARTURE"
                await self._save(
                    session,
                    "request",
                    original["operation_id"],
                    request_kind="RACK_DEPARTURE",
                    response=decision,
                    decision_code=decision["result"],
                    saved_at_ms=now_ms,
                    state="WAITING" if decision["result"] == "WAIT" else "CONSUMED",
                    due_at_ms=now_ms + decision["retry_after_ms"]
                    if decision["result"] == "WAIT"
                    else None,
                )
                original = await self._one(
                    session,
                    "request",
                    table.c.operation_id == original["operation_id"],
                    request_kind="RACK_DEPARTURE",
                )
            if original["response"]["result"] == "READY":
                if (
                    original["request_snapshot"]["current_location"]["location_code"]
                    != position["actual_position"]["location_code"]
                ):
                    return "POSITION_CONFLICT"
                await self._action(
                    session,
                    work,
                    kind="DEPART",
                    source_ref=original["operation_id"],
                    target=original["response"]["rack_destination"],
                    face=None,
                )
                return "WAITING_FOR_DEPARTURE_RESULT"
            if now_ms < original["due_at_ms"] and original["basis"] == basis:
                return "WAITING_FOR_DEPARTURE"
        payload = DepartureRequest(
            workline_code=work["workline_code"],
            rack_id=work["rack_id"],
            current_location=dict(
                type="RACK_POSITION", location_code=position["actual_position"]["location_code"]
            ),
            current_face=position["actual_face"],
        )
        operation_id = str(uuid7())
        maximum = await session.scalar(
            select(func.max(table.c.operation_id)).where(
                table.c.kind == "RACK_DEPARTURE",
                table.c.rack_work_id == work["id"],
                table.c.task_id == work["task_id"],
                table.c.work_line_id == work["work_line_id"],
            )
        )
        if maximum is not None and operation_id <= maximum:
            # Old code resumes the greatest ID; a retry must remain that same current attempt.
            milliseconds = int(validate_uuid7(maximum).replace("-", "")[:12], 16) + 1
            operation_id = str(
                uuid7(now=datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=milliseconds))
            )
        message = await self.capabilities.wms.send(session, payload, operation_id=operation_id)
        await session.execute(
            table.insert().values(
                kind="RACK_DEPARTURE",
                operation=OPERATION,
                operation_id=operation_id,
                task_id=work["task_id"],
                work_line_id=work["work_line_id"],
                previous_operation_id=work["departure_operation_id"],
                rack_work_id=work["id"],
                message_id=message.message_id,
                request_snapshot=payload.model_dump(mode="json"),
                basis=basis,
            )
        )
        await self._save(session, "rack", work["id"], departure_operation_id=operation_id)
        return "WAITING_FOR_DEPARTURE"

    async def accept_direct_pick_completed(
        self, ctx: CallbackContext, payload: DirectPickCompleted
    ) -> Outcome:
        return await self._accept_completed(ctx, payload, face=payload.rack_face)

    async def accept_return_rack_completed(
        self, ctx: CallbackContext, payload: ReturnRackCompleted
    ) -> Outcome:
        return await self._accept_completed(ctx, payload, face=None)

    async def _accept_completed(
        self,
        ctx: CallbackContext,
        payload: DirectPickCompleted | ReturnRackCompleted,
        *,
        face: str | None,
    ) -> Outcome:
        _, task = await self.repository.original(ctx.session, payload.task_id)
        if task is None:
            raise Unavailable()
        if task["business_type"] != "outbound":
            raise Rejected("INVALID_DATA")
        table = self.tables["input"]
        await ctx.session.execute(
            table.insert().values(
                operation_id=ctx.operation_id,
                operation=ctx.operation,
                receipt_id=ctx.receipt_id,
                task_id=payload.task_id,
                work_line_id=task["work_line_id"],
                rack_id=payload.rack_id,
                plan_revision=payload.plan_revision
                if face is not None
                else payload.through_plan_revision,
                rack_face=face,
                completed_at=payload.completed_at,
                timestamp=ctx.timestamp,
                payload=payload.model_dump(mode="json"),
                state="PENDING",
            )
        )
        await self.apply_completions(ctx.session, task=task)
        await self.repository.note_received_work(
            ctx.session, task, operation=ctx.operation, record_id=ctx.operation_id
        )
        return Accepted()

    async def apply_completions(self, session: AsyncSession, *, task: RowMapping) -> None:
        """The caller already holds the original task's WorkLine/Device/task locks."""
        table, members = self.tables["input"], self.tables["member"]
        notices = (
            await session.execute(
                select(table).where(
                    table.c.task_id == task["task_id"],
                    table.c.operation.in_(RACK_COMPLETION_OPERATIONS),
                    table.c.state == "PENDING",
                )
            )
        ).mappings()
        for notice in notices:
            if task["plan_revision"] < notice["plan_revision"]:
                continue
            scope = [
                members.c.task_id == task["task_id"],
                members.c.rack_id == notice["rack_id"],
                members.c.kind == "DIRECT",
            ]
            if notice["rack_face"] is None:
                scope.append(members.c.plan_revision <= notice["plan_revision"])
            else:
                scope.extend(
                    (
                        members.c.plan_revision == notice["plan_revision"],
                        members.c.rack_face == notice["rack_face"],
                    )
                )
            await session.execute(
                members.update()
                .where(*scope, members.c.status.in_(("PENDING", "STARTED")))
                .values(status="COMPLETED")
            )
            await session.execute(
                table.update()
                .where(
                    table.c.operation == notice["operation"],
                    table.c.operation_id == notice["operation_id"],
                )
                .values(state="APPLIED")
            )
