"""One ordinary/drain rack lifecycle, using only the caller's transaction."""

from dataclasses import dataclass
from time import time
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identifiers import uuid7
from app.integrations.wms_transport import (
    HandoffPosition,
    Rack,
    RackPosition,
    TransportCaller,
    TransportTaskResult,
    Zone,
)

from .models import TABLES
from .repository import RollerRepository


async def select_supply_cycle(roller, session, demand_id):
    demand = await roller.original_record(session, "supply_demand", demand_id)
    if demand["cycle_id"]:
        return demand["cycle_id"]
    if demand["withdrawn"]:
        return None
    current = await roller.production.read_current_task(
        session, work_line_id=demand["work_line_id"]
    )
    if not current.is_valid or current.task_id != demand["task_id"]:
        return None
    repo = roller.repository(demand["work_line_id"])
    cycles = repo.tables["rack_cycle"]
    original = await repo._one(
        session,
        cycles,
        cycles.c.rack_id == demand["rack_id"],
        cycles.c.task_id == demand["task_id"],
        cycles.c.purpose == "ORDINARY",
        cycles.c.status == "ACTIVE",
    )
    if original is not None:
        departure = repo.tables["departure"]
        if await session.scalar(
            select(departure.c.id)
            .where(*repo.scoped(departure), departure.c.cycle_id == original["id"])
            .limit(1)
        ):
            return None
        return original["id"]
    else:
        resources = await roller.read_resources(session, repo.work_line_id)
        if resources is None:
            return None
        original = await repo.create_cycle(
            session,
            cycle_id=str(uuid7()),
            rack_id=demand["rack_id"],
            purpose="ORDINARY",
            task_id=demand["task_id"],
            source_ref=demand["id"],
            resources=resources,
        )
    await repo.bind_supply(session, demand_id=demand_id, cycle_id=original["id"])
    return original["id"]


async def rack_action(roller, session, repo, cycle, *, kind, source_ref, target, face, source):
    actions = repo.tables["rack_action"]
    conditions = [actions.c.cycle_id == cycle["id"], actions.c.kind == kind]
    if kind == "CTU02":
        conditions.append(actions.c.source_ref == source_ref)
    action = await repo._one(session, actions, *conditions)
    if action is None:
        entry = await repo._one(
            session, actions, actions.c.cycle_id == cycle["id"], actions.c.kind == "CTU01"
        )
        caller = (
            TransportCaller(entry["caller_workline_id"], entry["station_id"])
            if entry
            else TransportCaller(
                cycle["resources"]["caller_workline_id"], cycle["resources"]["station_id"]
            )
        )
        action = await repo.register_rack_action(
            session,
            cycle_id=cycle["id"],
            kind=kind,
            source_ref=source_ref,
            action_id=str(uuid7()),
            client_request_id=str(uuid7()),
            transport_task_id=None,
            operation_id=None,
            target=target,
            target_face=face,
            caller_workline_id=caller.workline_id,
            station_id=caller.station_id,
        )
    if action["transport_task_id"] is None:
        caller = TransportCaller(action["caller_workline_id"], action["station_id"])
        args = dict(
            client_request_id=action["client_request_id"],
            caller=caller,
            rack_id=cycle["rack_id"],
            cycle_id=cycle["id"],
            target_face=action["target_face"],
            rcs_template_id=kind,
        )
        destination = action["target"]
        if kind == "CTU02":
            handle = await roller.capabilities.transport.rotate_rack(
                session, **args, position=RackPosition(destination["location_code"])
            )
        else:
            handle = await roller.capabilities.transport.move_rack(
                session,
                **args,
                source=source,
                target=(Zone if destination["kind"] == "ZONE" else RackPosition)(
                    destination["location_code"]
                ),
            )
        view = await roller.capabilities.transport.get_result(
            session,
            transport_task_id=handle.transport_task_id,
            client_request_id=handle.client_request_id,
            caller=caller,
        )
        await repo.bind_transport(
            session,
            action_id=action["id"],
            transport_task_id=handle.transport_task_id,
            operation_id=view.operation_id,
        )
    return await repo.get_action(session, action["id"])


async def advance_departure(roller, session, repo, cycle, source_ref):
    from .request.rack_departure_decide_v1 import DECISION, OPERATION, DepartureRequest

    table = repo.tables["departure"]
    original = (
        (
            await session.execute(
                select(table)
                .where(*repo.scoped(table), table.c.cycle_id == cycle["id"])
                .order_by(table.c.id.desc())
                .limit(1)
            )
        )
        .mappings()
        .one_or_none()
    )
    if original is not None and original["decision"] is None:
        result = await roller.capabilities.wms.get_result(
            session,
            message_id=original["message_id"],
            operation=OPERATION,
            operation_id=original["operation_id"],
        )
        if (
            result.availability != "FOUND"
            or result.response_state != "VALID"
            or result.message_id != original["message_id"]
            or result.operation != OPERATION
            or result.operation_id != original["operation_id"]
            or (result.http_status, result.response_code) != (200, "DECIDED")
        ):
            return "WAITING_FOR_DEPARTURE"
        try:
            decision = DECISION.validate_python(result.response_data).model_dump(mode="json")
        except ValueError:
            return "INVALID_DEPARTURE"
        if decision.get("rack_destination") == original["request"]["current_location"]:
            return "INVALID_DEPARTURE"
        await session.execute(
            table.update()
            .where(*repo.scoped(table), table.c.id == original["id"])
            .values(
                decision=decision,
                status=decision["result"],
                due_at=int(time() * 1000) + decision["retry_after_ms"]
                if decision["result"] == "WAIT"
                else None,
            )
        )
        original = await repo._one(session, table, table.c.id == original["id"])
    if original is not None and original["status"] == "READY":
        destination = original["decision"]["rack_destination"]
        await rack_action(
            roller,
            session,
            repo,
            cycle,
            kind="CTU03",
            source_ref=original["id"],
            target=dict(kind=destination["type"], location_code=destination["location_code"]),
            face=None,
            source=RackPosition(original["request"]["current_location"]["location_code"]),
        )
        return "WAITING_FOR_DEPARTURE_ACCEPTANCE"
    if original is not None:
        if original["status"] != "WAIT" or original["due_at"] > int(time() * 1000):
            return "WAITING_FOR_DEPARTURE"
        source_ref = original["id"]
    position = await repo.current_position(session, "RACK", cycle["rack_id"])
    if position is None:
        return "WAITING_FOR_ORIGINAL_POSITION"
    position_action = await repo.get_action(session, position["source_id"])
    if position_action is None or position_action["cycle_id"] != cycle["id"]:
        return "WAITING_FOR_ORIGINAL_POSITION"
    payload = DepartureRequest(
        workline_code=cycle["resources"]["workline_code"],
        rack_id=cycle["rack_id"],
        current_location=dict(type="RACK_POSITION", location_code=position["location_code"]),
        current_face=position["rack_face"],
    )
    new = await repo._register(
        session,
        table,
        dict(
            id=str(uuid7()),
            **repo.scope,
            cycle_id=cycle["id"],
            source_ref=source_ref,
            operation_id=str(uuid7()),
            request=payload.model_dump(mode="json"),
        ),
        ("cycle_id", "source_ref"),
        (),
    )
    if new["message_id"] is None:
        reference = await roller.capabilities.wms.send(
            session, payload, operation_id=new["operation_id"]
        )
        await session.execute(
            table.update()
            .where(*repo.scoped(table), table.c.id == new["id"])
            .values(message_id=reference.message_id)
        )
    if original is not None:
        await session.execute(
            table.update()
            .where(*repo.scoped(table), table.c.id == original["id"])
            .values(pending=False)
        )
    return "WAITING_FOR_DEPARTURE"


async def advance_cycle(roller, session, cycle_id):
    """The only rack lifecycle. Box results are consumed by their original work owner."""
    from .feed_flow import opportunity, prepare_feed, start_feed

    cycle = await roller.original_record(session, "rack_cycle", cycle_id)
    repo = roller.repository(cycle["work_line_id"])
    actions, services, departures = (
        repo.tables[k] for k in ("rack_action", "face_service", "departure")
    )
    originals = (
        (
            await session.execute(
                select(actions)
                .where(*repo.scoped(actions), actions.c.cycle_id == cycle_id)
                .order_by(actions.c.id)
            )
        )
        .mappings()
        .all()
    )
    for action in originals:
        if action["transport_task_id"] is not None and action["pending"]:
            result = await roller.capabilities.transport.get_result(
                session,
                transport_task_id=action["transport_task_id"],
                client_request_id=action["client_request_id"],
                caller=TransportCaller(action["caller_workline_id"], action["station_id"]),
            )
            await apply_rack_result(session, action_id=action["id"], result=result, repository=repo)
    cycle = await repo.get_cycle(session, cycle_id)
    if cycle["status"] == "RELEASED":
        await session.execute(
            services.update()
            .where(*repo.scoped(services), services.c.cycle_id == cycle_id)
            .values(status="COMPLETED", pending=False)
        )
        await session.execute(
            departures.update()
            .where(*repo.scoped(departures), departures.c.cycle_id == cycle_id)
            .values(pending=False)
        )
        return "RELEASED"
    if await session.scalar(
        select(departures.c.id)
        .where(*repo.scoped(departures), departures.c.cycle_id == cycle_id)
        .limit(1)
    ):
        return await advance_departure(roller, session, repo, cycle, cycle_id)
    while True:
        service = await repo._one(
            session, services, services.c.cycle_id == cycle_id, services.c.pending.is_(True)
        )
        if service is None:
            completed = (
                (
                    await session.execute(
                        select(services)
                        .where(*repo.scoped(services), services.c.cycle_id == cycle_id)
                        .order_by(services.c.id)
                    )
                )
                .mappings()
                .all()
            )
            demand = None
            if cycle["purpose"] == "ORDINARY":
                current_task = await roller.production.read_current_task(
                    session, work_line_id=repo.work_line_id
                )
                demands = repo.tables["supply_demand"]
                query = select(demands).where(
                    *repo.scoped(demands),
                    demands.c.task_id == cycle["task_id"],
                    demands.c.rack_id == cycle["rack_id"],
                    demands.c.service_id.is_(None),
                    or_(
                        demands.c.cycle_id == cycle_id,
                        demands.c.cycle_id.is_(None)
                        & (current_task.is_valid and current_task.task_id == cycle["task_id"]),
                    ),
                    or_(demands.c.withdrawn.is_(False), demands.c.cycle_id == cycle_id),
                )
                demand = (
                    (await session.execute(query.order_by(demands.c.id).limit(1)))
                    .mappings()
                    .one_or_none()
                )
                if demand is not None:
                    source_ref, face = demand["id"], demand["rack_face"]
            else:
                drain = await roller.original_record(session, "drain", cycle["source_ref"])
                candidate = next(
                    c for c in drain["decision"]["racks"] if c["rack_id"] == cycle["rack_id"]
                )
                remaining = [
                    f
                    for f in candidate["rack_face"]
                    if f not in {s["rack_face"] for s in completed}
                ]
                if remaining and (
                    not completed
                    or not await roller.return_channel_empty(
                        session, work_line_id=repo.work_line_id
                    )
                ):
                    face = remaining[0]
                    source_ref = drain["id"] + ":" + face
                else:
                    remaining = []
            if (cycle["purpose"] == "ORDINARY" and demand is None) or (
                cycle["purpose"] == "DRAIN" and not remaining
            ):
                return await advance_departure(
                    roller, session, repo, cycle, completed[-1]["id"] if completed else cycle_id
                )
            service = await repo._register(
                session,
                services,
                dict(
                    id=str(uuid7()),
                    **repo.scope,
                    cycle_id=cycle_id,
                    source_ref=source_ref,
                    rack_face=face,
                    task_id=cycle["task_id"],
                    plan_revision=demand["plan_revision"] if demand else None,
                ),
                ("cycle_id", "source_ref"),
                ("rack_face", "task_id", "plan_revision"),
            )
            if demand is not None:
                await repo.bind_supply(
                    session, demand_id=demand["id"], cycle_id=cycle_id, service_id=service["id"]
                )
        resources = cycle["resources"]
        entry = await repo._one(
            session, actions, actions.c.cycle_id == cycle_id, actions.c.kind == "CTU01"
        )
        if service["status"] != "READY":
            if entry is None:
                await rack_action(
                    roller,
                    session,
                    repo,
                    cycle,
                    kind="CTU01",
                    source_ref=service["id"],
                    target=dict(
                        kind="RACK_POSITION", location_code=resources["positions"]["FIVE_RACK"]
                    ),
                    face=service["rack_face"],
                    source=Rack(cycle["rack_id"]),
                )
                return "WAITING_FOR_RACK"
            if entry["status"] != "SUCCEEDED":
                return "WAITING_FOR_RACK"
            position = await repo.current_position(session, "RACK", cycle["rack_id"])
            position_action = (
                await repo.get_action(session, position["source_id"]) if position else None
            )
            # A newer mismatch fact cannot authorize the older successful action's successor.
            if (
                position_action is None
                or position_action["cycle_id"] != cycle_id
                or position_action["status"] != "SUCCEEDED"
                or position["location_code"] != position_action["target"]["location_code"]
                or position["rack_face"] != position_action["target_face"]
            ):
                return "WAITING_FOR_ORIGINAL_POSITION"
            if position["rack_face"] != service["rack_face"]:
                await rack_action(
                    roller,
                    session,
                    repo,
                    cycle,
                    kind="CTU02",
                    source_ref=service["id"],
                    target=entry["target"],
                    face=service["rack_face"],
                    source=RackPosition(entry["target"]["location_code"]),
                )
                return "WAITING_FOR_RACK"
            await session.execute(
                services.update()
                .where(*repo.scoped(services), services.c.id == service["id"])
                .values(status="READY")
            )
        caller = TransportCaller(entry["caller_workline_id"], entry["station_id"])
        if cycle["purpose"] == "ORDINARY":
            feed_id = await start_feed(
                roller,
                session,
                demand_id=service["source_ref"],
                caller=caller,
                target=HandoffPosition(resources["positions"]["INLET"]),
            )
            await prepare_feed(roller, session, service["source_ref"])
            feed = await roller.original_record(session, "feed", feed_id)
        else:
            await opportunity(
                repo,
                session,
                feed=dict(service_id=service["id"]),
                source_ref=service["id"],
                kind="PURE_RETURN",
            )
        opportunities = repo.tables["return_opportunity"]
        current = await repo._one(
            session,
            opportunities,
            opportunities.c.service_id == service["id"],
            opportunities.c.ended.is_(False),
        )
        if current is not None:
            batches = repo.tables["return_batch"]
            saved = (
                (
                    await session.execute(
                        select(batches)
                        .where(*repo.scoped(batches), batches.c.opportunity_id == current["id"])
                        .order_by(batches.c.id.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            source_ref = saved["id"] if saved and not saved["pending"] else current["id"]
            batch_id = await roller.start_return_batch(
                session,
                opportunity_id=current["id"],
                source_ref=source_ref,
                workline_code=resources["workline_code"],
                caller=caller,
                source=HandoffPosition(resources["positions"]["OUTLET"]),
            )
            if batch_id is not None:
                return "WAITING_FOR_RETURN"
            if current["kind"] == "SEGMENT":
                await prepare_feed(roller, session, service["source_ref"])
                return "WAITING_FOR_FEED"
        if cycle["purpose"] == "ORDINARY" and not feed["feed_complete"]:
            return "WAITING_FOR_FEED"
        await session.execute(
            services.update()
            .where(*repo.scoped(services), services.c.id == service["id"])
            .values(status="COMPLETED", pending=False)
        )


@dataclass(frozen=True, slots=True)
class RackApplyResult:
    action_changed: bool
    projection_update: Literal["UPDATED", "HISTORY_ONLY", "NEEDS_ASSOCIATION"]


async def apply_rack_result(
    session: AsyncSession,
    *,
    action_id: str,
    result: TransportTaskResult,
    repository: RollerRepository | None = None,
) -> RackApplyResult:
    if repository is None:
        table = TABLES["rack_action"]
        original = (
            (await session.execute(select(table).where(table.c.id == action_id)))
            .mappings()
            .one_or_none()
        )
        if original is None:
            return RackApplyResult(False, "NEEDS_ASSOCIATION")
        repository = RollerRepository(original["owner_domain"], original["work_line_id"])
    action = await repository.get_action(session, action_id)
    if action is None:
        return RackApplyResult(False, "NEEDS_ASSOCIATION")
    cycle = await repository.get_cycle(session, action["cycle_id"])
    expected_kind = "RACK_ROTATE" if action["kind"] == "CTU02" else "RACK_MOVE"
    if cycle is None or (
        result.availability != "FOUND"
        or result.transport_task_id != action["transport_task_id"]
        or result.client_request_id != action["client_request_id"]
        or result.owner_domain != action["owner_domain"]
        or result.operation_id != action["operation_id"]
        or result.workline_id != action["caller_workline_id"]
        or result.station_id != action["station_id"]
        or result.cycle_id != cycle["id"]
        or result.kind != expected_kind
    ):
        return RackApplyResult(False, "NEEDS_ASSOCIATION")
    actions, cycles = repository.tables["rack_action"], repository.tables["rack_cycle"]
    scope = repository.scoped(actions)
    if action["kind"] == "CTU03":
        accepted = (
            result.submission_state == "AVAILABLE"
            and result.submission_response_state == "VALID"
            and (result.submission_http_status, result.submission_response_code)
            in {(202, "RECEIVED"), (200, "DUPLICATE")}
        )
        succeeded = result.outcome is not None and result.outcome["status"] == "SUCCEEDED"
        if (accepted or succeeded) and cycle["status"] != "RELEASED":
            basis = dict(
                transport_task_id=result.transport_task_id,
                client_request_id=result.client_request_id,
                operation_id=result.operation_id,
                submission_message_id=result.submission_message_id,
                submission_target=result.submission_target,
                submission_http_status=result.submission_http_status,
                submission_response_code=result.submission_response_code,
                outcome_revision=result.outcome_revision if succeeded else None,
            )
            await session.execute(
                actions.update()
                .where(*scope, actions.c.id == action_id)
                .values(status="ACCEPTED", pending=False, acceptance_basis=basis)
            )
            await session.execute(
                cycles.update()
                .where(*repository.scoped(cycles), cycles.c.id == cycle["id"])
                .values(status="RELEASED", pending=False, release_basis=basis)
            )
            positions = repository.tables["position_fact"]
            original_actions = select(actions.c.id).where(*scope, actions.c.cycle_id == cycle["id"])
            await session.execute(
                positions.update()
                .where(
                    *repository.scoped(positions),
                    positions.c.source_kind == "RACK",
                    positions.c.source_id.in_(original_actions),
                    positions.c.is_current.is_(True),
                )
                .values(is_current=False)
            )
            return RackApplyResult(True, "HISTORY_ONLY")
        return RackApplyResult(False, "HISTORY_ONLY")
    changed = False
    projection = "HISTORY_ONLY"
    outcome, observation = result.outcome, result.position_observation
    for data in (outcome, observation):
        if data is not None and (
            data["rack_id"] != cycle["rack_id"]
            or data["transport_task_id"] != action["transport_task_id"]
            or data["kind"] != expected_kind
        ):
            return RackApplyResult(False, "NEEDS_ASSOCIATION")
    position = outcome
    # Only revisions of this original task decide whether its observation supersedes its outcome.
    if observation is not None and (
        position is None or observation["outcome_revision"] > position["outcome_revision"]
    ):
        position = observation
    if (
        position is not None
        and position["final_position"] is not None
        and not position.get("position_unknown")
    ):
        revision = position["outcome_revision"]
        projection = await repository.record_position(
            session,
            object_kind="RACK",
            object_code=cycle["rack_id"],
            location_code=position["final_position"]["location_code"],
            rack_face=position.get("arrival_face"),
            source_kind="RACK",
            source_id=action_id,
            source_revision=revision,
            history_only=cycle["status"] == "RELEASED",
        )
        if action["position_revision"] is None or revision > action["position_revision"]:
            await session.execute(
                actions.update()
                .where(*scope, actions.c.id == action_id)
                .values(position_revision=revision)
            )
    if outcome is not None and (
        action["consumed_revision"] is None
        or outcome["outcome_revision"] > action["consumed_revision"]
    ):
        status = "UNKNOWN" if outcome.get("position_unknown") else outcome["status"]
        await session.execute(
            actions.update()
            .where(*scope, actions.c.id == action_id)
            .values(
                status=status,
                consumed_revision=outcome["outcome_revision"],
                pending=status != "SUCCEEDED",
            )
        )
        changed = True
    return RackApplyResult(changed, projection)
