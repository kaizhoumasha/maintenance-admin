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
    roller: "BinRoller", session: AsyncSession, arrival: RowMapping
) -> CommandView | None:
    code = arrival["command_code"]
    if code is None:
        return None
    return await roller.capabilities.ecs.get_command(
        session, owner=roller.context, command_code=code
    )


def applied_result(view: CommandView | None, arrival: RowMapping) -> ResultEnvelope | None:
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
        view.command_code != arrival["command_code"]
        or result.command_code != arrival["command_code"]
        or result.device_code != arrival["device_code"]
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
    previous = await repository.latest_scan(ctx.session, ctx.device_role)
    separator = None
    if previous is not None and previous["command_code"] is not None:
        view = await get_scan_result(roller, ctx.session, previous)
        if applied_result(view, previous) is not None:
            separator = previous["command_code"]
    arrival = await repository.accept_scan_fact(
        ctx.session,
        digest=ctx.digest,
        receipt_id=ctx.receipt_id,
        point=ctx.device_role,
        device_code=ctx.device_code,
        raw_bin_code=payload.bin_code,
        bin_code=bin_identity(payload.bin_code),
        scanned_at=ctx.timestamp,
        separator_command_code=separator,
        binding_snapshot=asdict(ctx.binding),
        previous_arrival=previous,
    )
    if arrival["point"] == "SCAN2":
        if not arrival["delegated"]:
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
            await repository.update_scan(ctx.session, arrival["id"], delegated=True, pending=False)
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
            attempts = await repository.point_attempts(session, round_["id"], point)
            if attempts:
                previous = attempts[-1]
                view = await get_scan_result(roller, session, previous)
                result = applied_result(view, previous)
                if (
                    result is None
                    or result.result != "FAILED"
                    or not valid
                    or round_["status"] != "PENDING"
                ):
                    await repository.update_scan(session, arrival["id"], pending=False)
                    return
        command = MoveForward() if valid else MoveLeft()
    await create_direction(roller, session, repository, arrival, command)


async def release_scan2(roller: "BinRoller", session: AsyncSession, *, arrival_id: str) -> str:
    arrival = await roller.original_arrival(session, arrival_id)
    if arrival["point"] != "SCAN2":
        raise ValueError("original SCAN2 arrival required")
    repository = roller.repository(arrival["work_line_id"])
    releases = roller.tables["scan2_release"]
    release = await repository._one(session, releases, releases.c.arrival_id == arrival_id)
    if release is not None and release["command_code"] is not None:
        return release["command_code"]
    release = await repository._register(
        session,
        releases,
        dict(
            **repository.scope,
            id=str(uuid7()),
            arrival_id=arrival_id,
            call_key=f"bin_roller:scan2_release:{arrival_id}",
            command_code=None,
            pending=True,
            consumed_result_id=None,
        ),
        ["owner_domain", "work_line_id", "arrival_id"],
        ["call_key"],
    )
    view = await roller.capabilities.ecs.create(
        session,
        owner=roller.context,
        call_key=release["call_key"],
        device_code=arrival["device_code"],
        timeout_ms=30000,
        command=MoveForward(),
    )
    await session.execute(
        releases.update()
        .where(*repository.scoped(releases), releases.c.id == release["id"])
        .values(command_code=view.command_code)
    )
    await repository.update_scan(session, arrival_id, command_code=view.command_code)
    return view.command_code


async def consume_scan_result(
    roller: "BinRoller", session: AsyncSession, work: RollerWorkRef
) -> str:
    if work.kind == "SCAN2_RELEASE":
        releases = roller.tables["scan2_release"]
        release = (
            (
                await session.execute(
                    select(releases).where(
                        releases.c.owner_domain == roller.context.domain,
                        releases.c.id == work.record_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if release is None:
            raise ValueError("original SCAN2 release missing")
        arrival = await roller.original_arrival(session, release["arrival_id"])
        table, row = releases, release
    elif work.kind == "SCAN":
        arrival = await roller.original_arrival(session, work.record_id)
        table, row = roller.tables["scan_arrival"], arrival
        if arrival["point"] == "SCAN2":
            return "DELEGATED" if arrival["delegated"] else "WAITING"
    else:
        raise ValueError("unsupported scan work kind")
    result = applied_result(await get_scan_result(roller, session, arrival), arrival)
    if result is None:
        return "WAITING"
    await session.execute(
        table.update()
        .where(table.c.id == row["id"])
        .values(
            consumed_result_id=canonical_digest(result.model_dump()),
            pending=work.kind == "SCAN" and arrival["point"] == "SCAN4",
        )
    )
    return result.result
