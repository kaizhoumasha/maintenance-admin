"""Coarse sorting Mock decisions and durable test slot reservations."""

import re
from datetime import UTC, datetime
from hashlib import sha256
from hmac import compare_digest
from ipaddress import ip_address
from secrets import choice, randbelow

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import unix_ms
from app.core.database import SessionDep
from app.core.identifiers import validate_uuid7
from app.core.request_body import read_body_capped
from app.core.routing import new_router
from app.core.strict_json import json_object
from app.integrations.ecs_callback.binding import DeviceBinding
from app.integrations.ecs_callback.envelope import EcsEventEnvelope
from app.integrations.wms_request import Endpoint
from app.integrations.wms_request.adapter import (
    COARSE_MOCK_PATH,
    COARSE_MOCK_ROUND_HEADER,
    COARSE_MOCK_ROUND_ID,
    MOCK_SIGNATURE_HEADER,
    coarse_mock_signature,
)
from app.models import WorkLine

from .contracts import ALLOCATE, REPORT, VALIDATE, MockRound, RandomMockRound
from .definition import DEFINITION
from .mock_storage import CoarseSortingMockRound
from .request.inbound_coarse_sorting_execution_report_v1 import ExecutionReport
from .request.inbound_coarse_sorting_storage_allocate_v1 import StorageAllocate
from .request.inbound_coarse_sorting_validate_bind_v1 import ValidateBind

router = new_router(tags=["dev-coarse-sorting-mock-wms"])
OPERATIONS = {VALIDATE: ValidateBind, ALLOCATE: StorageAllocate, REPORT: ExecutionReport}
INVALID = (422, "REJECTED", {"reason_code": "INVALID_DATA"})
STATE_CONFLICT = (409, "CONFLICT", {"reason_code": "STATE_CONFLICT"})
ID_CONFLICT = (409, "CONFLICT", {"reason_code": "IDEMPOTENCY_CONFLICT"})
UNAVAILABLE = (503, "UNAVAILABLE", {})
REEL_BARCODE = re.compile(
    r"P[^,\s\x00]+,Q[0-9]+,M[^,\s\x00]+,D[^,\s\x00]+,L[^,\s\x00]+,S[^,\s\x00]+"
)


async def authorize_mock_scan(
    config: MockRound,
    session: AsyncSession,
    event: EcsEventEnvelope,
    binding: DeviceBinding | None,
    *,
    source_ip: str,
    real_callback: bool,
) -> bool:
    if (
        event.event_type != "SCAN_COMPLETED"
        or binding is None
        or binding.business_code != DEFINITION.code
        or binding.device_role != "INPUT_ARM"
    ):
        return True
    line_code = await session.scalar(
        select(WorkLine.line_code).where(WorkLine.id == binding.work_line_id)
    )
    if line_code != config.workline_code:
        return True
    if not real_callback:
        return False
    try:
        return ip_address(source_ip) in config.ecs_source_ips
    except ValueError:
        return False


def _decide(
    config: MockRound,
    payload: ValidateBind | StorageAllocate | ExecutionReport,
    state: dict,
    operation_id: str,
    rack_matches: bool,
) -> tuple[int, str, dict]:
    if isinstance(payload, ValidateBind):
        if isinstance(config, RandomMockRound):
            if REEL_BARCODE.fullmatch(payload.barcode) is None:
                return INVALID
            if not rack_matches:
                return UNAVAILABLE
            if randbelow(10) == 0:
                return 200, "DECIDED", {"result": "NG", "reason_code": "MOCK_RANDOM_NG"}
            reservations = state.setdefault("reservations", {})
            reserved = {reservation["cell"] for reservation in reservations.values()}
            empty = [index for index in range(len(config.empty_cells)) if index not in reserved]
            if not empty:
                return STATE_CONFLICT
            binding_id = f"COARSE-MOCK-{operation_id}"
            reservations[binding_id] = {"cell": choice(empty)}
            return 200, "DECIDED", {"result": "OK", "binding_id": binding_id}

        if payload.barcode == config.ng_barcode:
            if state.get("ng_validated"):
                return STATE_CONFLICT
            state["ng_validated"] = True
            return 200, "DECIDED", {"result": "NG", "reason_code": "BARCODE_INVALID"}
        if (
            payload.barcode != config.normal_barcode
            and payload.barcode not in config.additional_normal_barcodes
        ):
            return INVALID
        if state.get("binding_id"):
            return STATE_CONFLICT
        if not rack_matches:
            return UNAVAILABLE
        binding_id = f"COARSE-MOCK-{operation_id}"
        state["binding_id"] = binding_id
        return 200, "DECIDED", {"result": "OK", "binding_id": binding_id}

    if isinstance(config, RandomMockRound):
        reservation = state.get("reservations", {}).get(payload.binding_id)
        if reservation is None:
            return STATE_CONFLICT
        target = config.empty_cells[reservation["cell"]]
    else:
        if payload.binding_id != state.get("binding_id"):
            return STATE_CONFLICT
        reservation = state
        target = config.allocation
    if isinstance(payload, StorageAllocate):
        if not rack_matches or reservation.get("allocated"):
            return 200, "DECIDED", {"result": "WAIT", "workline_code": payload.workline_code}
        reservation["allocated"] = True
        return 200, "DECIDED", target.model_dump(mode="json")

    if (
        not reservation.get("allocated")
        or reservation.get("reported")
        or (
            payload.bin_code,
            payload.bin_location,
            payload.cell_id,
            payload.cell_index,
        )
        != (
            target.bin_code,
            target.bin_location,
            target.cell_id,
            target.cell_index,
        )
    ):
        return STATE_CONFLICT
    reservation["reported"] = True
    return 200, "RECORDED", {}


@router.post(COARSE_MOCK_PATH + "/{endpoint}", include_in_schema=False)
async def answer(request: Request, endpoint: Endpoint, session: SessionDep) -> Response:
    raw = await read_body_capped(request, 262144)
    if len(raw) > 262144:
        return Response(status_code=413)
    round_id = request.headers.get(COARSE_MOCK_ROUND_HEADER, "")
    signature = request.headers.get(MOCK_SIGNATURE_HEADER, "").encode()
    if re.fullmatch(COARSE_MOCK_ROUND_ID, round_id) is None or not compare_digest(
        signature, coarse_mock_signature(raw, round_id)
    ):
        return Response(status_code=401)
    sent = json_object(raw)
    if (
        sent is None
        or set(sent) != {"operation_id", "operation", "timestamp", "data"}
        or type(sent["timestamp"]) is not int
        or not 0 <= sent["timestamp"] <= 2**63 - 1
    ):
        return Response(status_code=400)
    try:
        operation_id = validate_uuid7(sent["operation_id"])
    except (TypeError, ValueError):
        return Response(status_code=400)

    def respond(status: int, code: str, data: dict, timestamp: int) -> JSONResponse:
        return JSONResponse(
            {"operation_id": operation_id, "code": code, "timestamp": timestamp, "data": data},
            status_code=status,
        )

    now = unix_ms(datetime.now(UTC))
    operation = sent["operation"]
    model = OPERATIONS.get(operation) if isinstance(operation, str) else None
    if model is None or (operation == REPORT) != (endpoint == Endpoint.FACTS):
        return respond(422, "REJECTED", {"reason_code": "UNSUPPORTED_OPERATION"}, now)
    try:
        payload = model.model_validate(sent["data"])
    except ValueError:
        return respond(*INVALID, now)
    config = request.app.state.coarse_sorting_mock_round
    configuration = config.configuration_digest
    body_digest = sha256(raw).hexdigest()
    async with session.begin():
        if round_id == config.round_id:
            if payload.workline_code != config.workline_code:
                return respond(*INVALID, now)
            insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
            await session.execute(
                insert(CoarseSortingMockRound)
                .values(round_id=round_id, configuration_digest=configuration, state={})
                .on_conflict_do_nothing()
            )
        ledger = await session.get(CoarseSortingMockRound, round_id, with_for_update=True)
        if ledger is None:
            return respond(*STATE_CONFLICT, now)
        state = dict(ledger.state)
        replies = state.get("replies", {})
        if original := replies.get(operation_id):
            digest, code, data, timestamp = original
            if digest != body_digest:
                return respond(*ID_CONFLICT, now)
            # Older candidates persisted 503 only for a full pool that cannot recover in place.
            if code == "UNAVAILABLE":
                return respond(*STATE_CONFLICT, timestamp)
            status = 409 if code == "CONFLICT" else 200
            return respond(status, "DUPLICATE" if code == "RECORDED" else code, data, timestamp)
        if round_id != config.round_id or ledger.configuration_digest != configuration:
            return respond(*STATE_CONFLICT, now)

        positions = await session.scalar(
            select(WorkLine.position_bindings).where(
                WorkLine.line_code == payload.workline_code,
                WorkLine.business_code == DEFINITION.code,
                WorkLine.is_enabled,
            )
        )
        rack_matches = (
            positions is not None and positions.get("OUTPUT_RACK") == config.rack_position
        )
        status, code, data = _decide(config, payload, state, operation_id, rack_matches)
        # Validation conflicts are permanent within this round and must replay on retry.
        if status == 200 or (status == 409 and isinstance(payload, ValidateBind)):
            state["replies"] = {**replies, operation_id: [body_digest, code, data, now]}
            ledger.state = state
    return respond(status, code, data, now)
