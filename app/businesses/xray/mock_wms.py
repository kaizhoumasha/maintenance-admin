"""Test decisions for the existing local Mock WMS endpoint."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest

from pydantic import ValidationError
from sqlalchemy import select

from app.businesses._components.rack_dispatch.service import XRAY_SOURCES
from app.core.clock import unix_ms
from app.core.identifiers import validate_uuid7
from app.integrations.wms_request.adapter import mock_signature
from app.models import WorkLine

from .contracts import CountOk, LabelVariables, RackAccept
from .definition import DEFINITION
from .racks import RackPoint
from .request import inbound_xray_count_decide_v1 as count
from .request import inbound_xray_pkg_decide_v1 as pkg
from .request import inbound_xray_put_target_decide_v1 as outlet
from .request import inbound_xray_verify_decide_v1 as verify

SAMPLE_BARCODE = re.compile(r"P[^,\x00]+,Q[0-9]+,M[^,\x00]+,D[^,\x00]+,L[^,\x00]+,S[^,\x00]+")
OPERATIONS = {
    pkg.OPERATION: pkg.PkgDecide,
    count.OPERATION: count.CountDecide,
    verify.OPERATION: verify.VerifyDecide,
    outlet.OPERATION: outlet.PutTargetDecide,
}
WAIT = {"result": "WAIT", "retry_after_ms": 5000}
INVALID = (422, "REJECTED", {"reason_code": "INVALID_DATA"})
CONFLICT = (409, "CONFLICT", {"reason_code": "IDEMPOTENCY_CONFLICT"})
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class XrayWmsMock:
    def __init__(self, targets, rack_settings):
        self.targets = {}
        self.fixed_lines = set()
        for line, raw_targets in targets.items():
            if (
                line not in rack_settings
                or len(raw_targets) != 2
                or any(not SAMPLE_BARCODE.fullmatch(barcode) for barcode in raw_targets)
            ):
                raise ValueError(
                    f"{line}: X-Ray mock requires two full sample barcodes and a rack point"
                )
            point = RackPoint.model_validate(rack_settings[line])
            mapped = {
                barcode: RackAccept.model_validate(raw) for barcode, raw in raw_targets.items()
            }
            locations = [target.target_locator for target in mapped.values()]
            if len({(loc.rack_id, loc.rack_face) for loc in locations}) != 1 or len(
                {loc.slot_id for loc in locations}
            ) != len(locations):
                raise ValueError(f"{line}: X-Ray mock requires two slots on the same rack face")
            for loc in locations:
                slot = point.slots.get(loc.rack_face, {}).get(loc.slot_id)
                if (
                    loc.location_code != point.location_code
                    or (point.fixed_test_rack and loc.rack_id != point.fixed_test_rack)
                    or slot is None
                    or (slot.rack_layer, slot.rack_column) != (loc.rack_layer, loc.rack_column)
                ):
                    raise ValueError(f"{line}: X-Ray mock slot must match XRAY_RETURN_RACKS")
            self.targets[line] = mapped
            if point.fixed_test_rack:
                self.fixed_lines.add(line)
        # shortcut: single-worker memory; use durable reservations if runs must survive restart.
        self._replies = {}
        self._issued_labels = {}
        self._reserved = {}
        self._lock = asyncio.Lock()

    async def answer(self, app, session, sent, raw, signature):
        operation = sent.get("operation")
        line = sent["data"].get("workline_code")
        if not isinstance(operation, str) or operation not in OPERATIONS:
            return None
        if not isinstance(line, str) or line not in self.targets:
            return None
        timestamp = unix_ms(datetime.now(UTC))
        if not compare_digest(signature.encode(), mock_signature(raw)):
            return (401, "UNAUTHORIZED", {}, timestamp)
        try:
            operation_id = validate_uuid7(sent["operation_id"])
            payload = OPERATIONS[operation].model_validate(sent["data"])
        except (TypeError, ValueError):
            return (*INVALID, timestamp)
        async with self._lock:
            if original := self._replies.get(operation_id):
                old_request, response = original
                return response if old_request == sent else (*CONFLICT, timestamp)
            try:
                data = await self._decide(app, session, payload, operation_id)
            except (ValidationError, OverflowError, ValueError):
                return (*INVALID, timestamp)
            response = (200, "DECIDED", data, timestamp)
            self._replies[operation_id] = (sent, response)
            return response

    async def _decide(self, app, session, payload, operation_id):
        counted = payload.barcode in self.targets[payload.workline_code]
        if isinstance(payload, pkg.PkgDecide):
            return {"result": "COUNT_REQUIRED" if counted else "BYPASS_COUNT"}
        if isinstance(payload, outlet.PutTargetDecide) and payload.mode == "BYPASS" and not counted:
            return {"result": "ACCEPT", "target_locator": {"type": "TRANSIT_BIN"}}
        if not counted:
            raise ValueError("unknown counted barcode")

        material = (payload.workline_code, payload.barcode)
        if isinstance(payload, count.CountDecide):
            spkg = "XRAYMOCK" + sha256(operation_id.encode()).hexdigest()[:16].upper()
            part, _, manufacturer, date, lot, _ = payload.barcode.split(",")
            variables = LabelVariables(
                ID=payload.template_id,
                PHHPN=part[1:],
                SPKG=spkg,
                MMfrPN=manufacturer[1:],
                VVendor="M000000000",
                QQTYPC=str(payload.actual_qty),
                Location="",
                DDC=date[1:],
                DateTime=(EPOCH + timedelta(milliseconds=payload.observed_at)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                LLC=lot[1:],
            )
            response = CountOk(result="OK", label_variables=variables).model_dump(mode="json")
            self._issued_labels[spkg] = material
            return response
        if isinstance(payload, verify.VerifyDecide):
            if (
                payload.scan_status == "OK"
                and payload.new_barcode == payload.expected_spkg
                and self._issued_labels.get(payload.expected_spkg) == material
            ):
                return {"result": "OK"}
            return {
                "result": "NG",
                "reason_code": "VERIFY_MISMATCH",
                "reason_message": "CCD did not read the SPKG generated for this reel",
            }
        if self._issued_labels.get(payload.new_barcode) != material:
            raise ValueError("outlet SPKG does not match the original material")
        return await self._rack_target(app, session, payload)

    async def _rack_target(self, app, session, payload):
        target = self.targets[payload.workline_code][payload.barcode]
        loc = target.target_locator
        line = await session.scalar(
            select(WorkLine).where(
                WorkLine.line_code == payload.workline_code,
                WorkLine.business_code == DEFINITION.code,
                WorkLine.is_enabled,
            )
        )
        if line is None or line.position_bindings.get("RETURN_RACK") != loc.location_code:
            return WAIT
        dispatch = app.state.business_runtime.services[DEFINITION.code].components["rack_dispatch"]
        scope = {
            "work_line_id": line.id,
            "point": loc.location_code,
            "source_kinds": XRAY_SOURCES,
        }
        fixed = payload.workline_code in self.fixed_lines
        ready = () if fixed else await dispatch.get_point_preparations(session, **scope)
        if fixed or (
            len(ready) == 1
            and (ready[0].origin.rack_id, ready[0].actual_face) == (loc.rack_id, loc.rack_face)
        ):
            key = (loc.rack_id, loc.rack_face, loc.slot_id)
            owner = (payload.workline_code, payload.new_barcode)
            if self._reserved.setdefault(key, owner) == owner:
                return target.model_dump(mode="json")
        elif not ready and not await dispatch.jobs(session, **scope, pending=True):
            if not await dispatch.has_position_conflict(session, point=loc.location_code):
                return {
                    "result": "REPLENISH_REQUIRED",
                    "location_code": loc.location_code,
                    "incoming_rack": {"rack_id": loc.rack_id, "target_face": loc.rack_face},
                }
        return WAIT
