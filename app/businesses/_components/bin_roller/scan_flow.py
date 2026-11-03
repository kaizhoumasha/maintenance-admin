"""Original arrivals and local point actions; production alone permits SCAN2 release."""

import re
from dataclasses import asdict
from typing import TYPE_CHECKING

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identifiers import uuid7
from app.core.strict_json import canonical_digest
from app.integrations.ecs_callback import EcsEventContext
from app.integrations.ecs_command.contracts import CommandView, ResultEnvelope

from .contracts import ScanCompletedData
from .ecs_command import MoveForward, MoveLeft, MoveRight
from .ports import RollerWorkRef, Scan2Arrival
from .repository import RollerRepository

if TYPE_CHECKING:
    from .service import BinRoller

_BIN = re.compile(r"A[0-9]{9}\Z")


def bin_identity(raw: str) -> str | None:
    if len(raw) < 3 or raw[-2:] not in {"-A", "-B", "-C", "-D"}:
        return None
    code = raw[:-2]
    return code if _BIN.fullmatch(code) else None


async def get_scan_result(
    roller: "BinRoller", session: AsyncSession, code: str | None
) -> CommandView | None:
    if code is None:
        return None
    return await roller.capabilities.ecs.get_command(
        session, owner=roller.context, command_code=code
    )


def applied_result(
    view: CommandView | None, code: str | None, device_code: str
) -> ResultEnvelope | None:
    if (
        view is None
        or view.result is None
        or view.hold_reason
        or view.needs_reconciliation
        or view.status not in {"SUCCEEDED", "FAILED"}
    ):
        return None
    result = ResultEnvelope.model_validate(view.result)
    if (
        view.command_code != code
        or result.command_code != code
        or result.device_code != device_code
        or view.status != ("SUCCEEDED" if result.result == "SUCCESS" else "FAILED")
    ):
        return None
    return result


async def accept_scan(
    roller: "BinRoller", ctx: EcsEventContext, payload: ScanCompletedData
) -> None:
    facts = roller.tables["scan_fact"]
    original = (
        (await ctx.session.execute(select(facts).where(facts.c.digest == ctx.digest)))
        .mappings()
        .one_or_none()
    )
    # Technical identity survives receipt retention and subsequent workline/business rebindings.
    if original is not None:
        return
    if ctx.business_code != roller.context.domain:
        raise ValueError("scan business does not match the component owner")
    repository = roller.repository(ctx.work_line_id)
    jobs = ()
    fifo = ()
    bin_code = bin_identity(payload.bin_code)
    affects_return = ctx.device_role == "SCAN4" or (
        ctx.device_role == "SCAN3" and bin_code is not None and payload.bin_code.endswith("-B")
    )
    if affects_return:
        jobs = await roller.lock_jobs_on_line(ctx.session, ctx.work_line_id)
        await roller.lock_production_subjects(ctx.session, ctx.work_line_id, jobs)
        fifo = await repository.return_queue(ctx.session)
    if ctx.device_role == "SCAN2":
        await roller.production.lock_scan2_task(ctx.session, work_line_id=ctx.work_line_id)
    previous = await repository.latest_scan(ctx.session, ctx.device_role)
    separator = None
    if previous is not None and previous["command_code"] is not None:
        view = await get_scan_result(roller, ctx.session, previous["command_code"])
        if applied_result(view, previous["command_code"], previous["device_code"]) is not None:
            separator = previous["command_code"]
    arrival = await repository.accept_scan_fact(
        ctx.session,
        digest=ctx.digest,
        receipt_id=ctx.receipt_id,
        point=ctx.device_role,
        device_code=ctx.device_code,
        raw_bin_code=payload.bin_code,
        bin_code=bin_code,
        scanned_at=ctx.timestamp,
        separator_command_code=separator,
        binding_snapshot=asdict(ctx.binding),
        previous_arrival=previous,
    )
    if affects_return and arrival["first_digest"] == ctx.digest:
        identity = {
            "arrival_id": arrival["id"],
            "digest": ctx.digest,
            "receipt_id": ctx.receipt_id,
            "point": ctx.device_role,
        }
        decisions = roller.tables["decision"]
        active = (
            (
                await ctx.session.execute(
                    select(decisions).where(*repository.scoped(decisions), decisions.c.pending)
                )
            )
            .mappings()
            .all()
        )
        for decision in active:
            await ctx.session.execute(
                decisions.update()
                .where(
                    *repository.scoped(decisions),
                    decisions.c.kind == "DRAIN",
                    decisions.c.id == decision["id"],
                )
                .values(received_work=[*(decision["received_work"] or ()), identity])
            )
        if not active and not jobs and fifo and fifo[0]["id"] and fifo[0]["status"] == "PENDING":
            head = fifo[0]
            await repository.update_scan(
                ctx.session,
                head["fifo_arrival_id"],
                received_work=[*(head["received_work"] or ()), identity],
            )
        if jobs:
            await roller.rack_dispatch.note_facts_changed(
                ctx.session, job_ids=tuple(j.id for j in jobs)
            )
    if arrival["point"] == "SCAN2":
        if arrival["delegation_state"] != "ACCEPTED":
            await roller.production.accept_scan2(
                ctx,
                Scan2Arrival(
                    arrival_id=arrival["id"],
                    work_line_id=arrival["work_line_id"],
                    device_code=arrival["device_code"],
                    raw_bin_code=arrival["raw_bin_code"],
                    bin_code=arrival["bin_code"],
                    scanned_at=arrival["scanned_at"],
                ),
            )
            await repository.update_scan(
                ctx.session, arrival["id"], delegation_state="ACCEPTED", pending=False
            )
        return
    if arrival["first_digest"] == ctx.digest:
        await accept_point(roller, ctx.session, repository, arrival)


async def create_direction(
    roller: "BinRoller",
    session: AsyncSession,
    repository: RollerRepository,
    arrival: RowMapping,
    command: BaseModel,
) -> str:
    if arrival["command_code"] is not None:
        return arrival["command_code"]
    view = await roller.capabilities.ecs.create(
        session,
        owner=roller.context,
        call_key=f"bin_roller:scan:{arrival['id']}",
        device_code=arrival["device_code"],
        timeout_ms=30000,
        command=command,
    )
    await repository.update_scan(session, arrival["id"], command_code=view.command_code)
    return view.command_code


async def accept_point(
    roller: "BinRoller", session: AsyncSession, repository: RollerRepository, arrival: RowMapping
) -> None:
    if arrival["command_code"] is not None or not arrival["pending"]:
        return
    point = arrival["point"]
    valid = arrival["bin_code"] is not None and arrival["raw_bin_code"].endswith("-B")
    if point == "SCAN1":
        task = await roller.production.read_current_task(
            session, work_line_id=repository.work_line_id
        )
        command = (
            MoveForward() if valid and task.is_valid and task.task_id is not None else MoveRight()
        )
    else:
        round_ = await repository.bind_scan_round(session, arrival, valid=valid)
        if round_ is None and (point == "SCAN4" or valid):
            return
        if round_ is not None:
            attempts = await repository.point_attempts(session, round_["round_id"], point)
            if attempts:
                previous = attempts[-1]
                view = await get_scan_result(roller, session, previous["command_code"])
                result = applied_result(view, previous["command_code"], previous["device_code"])
                if (
                    result is None
                    or result.result != "FAILED"
                    or not valid
                    or round_["return_state"] != "PENDING"
                ):
                    await repository.update_scan(session, arrival["id"], pending=False)
                    return
        command = MoveForward() if valid else MoveLeft()
    await create_direction(roller, session, repository, arrival, command)


async def release_scan2(roller: "BinRoller", session: AsyncSession, *, arrival_id: str) -> str:
    arrival = await roller.original_record(session, "scan_arrival", arrival_id, for_update=True)
    if arrival["point"] != "SCAN2":
        raise ValueError("original SCAN2 arrival required")
    repository = roller.repository(arrival["work_line_id"])
    if arrival["release_command_code"] is not None:
        return arrival["release_command_code"]
    call_key = arrival["release_call_key"] or f"bin_roller:scan2_release:{arrival_id}"
    view = await roller.capabilities.ecs.create(
        session,
        owner=roller.context,
        call_key=call_key,
        device_code=arrival["device_code"],
        timeout_ms=30000,
        command=MoveForward(),
    )
    await repository.update_scan(
        session,
        arrival_id,
        release_record_id=arrival["release_record_id"] or str(uuid7()),
        release_call_key=call_key,
        release_state="PENDING",
        release_command_code=view.command_code,
        command_code=view.command_code,
    )
    return view.command_code


async def consume_scan_result(
    roller: "BinRoller", session: AsyncSession, work: RollerWorkRef
) -> str:
    if work.kind == "SCAN2_RELEASE":
        arrival = await roller.original_record(session, "scan2_release", work.record_id)
    elif work.kind == "SCAN":
        arrival = await roller.original_arrival(session, work.record_id)
        if arrival["point"] == "SCAN2":
            return "DELEGATED" if arrival["delegation_state"] == "ACCEPTED" else "WAITING"
    else:
        raise ValueError("unsupported scan work kind")
    code = (
        arrival["release_command_code"] if work.kind == "SCAN2_RELEASE" else arrival["command_code"]
    )
    result = applied_result(
        await get_scan_result(roller, session, code), code, arrival["device_code"]
    )
    if result is None:
        return "WAITING"
    digest = canonical_digest(result.model_dump())
    values = (
        dict(release_consumed_result_id=digest, release_state="CONSUMED")
        if work.kind == "SCAN2_RELEASE"
        else dict(consumed_result_id=digest, pending=arrival["point"] == "SCAN4")
    )
    await roller.repository(arrival["work_line_id"]).update_scan(session, arrival["id"], **values)
    return result.result
