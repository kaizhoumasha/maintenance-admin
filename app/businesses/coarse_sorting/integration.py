"""Tail stages explicitly enabled in the shared WES integration stack."""

from dataclasses import replace
from datetime import UTC, datetime
from functools import cache

from pydantic import TypeAdapter, ValidationError

from app.businesses._components.rack_dispatch.composition import COMPONENT

from . import composition
from .contracts import Reference, mock_round_config
from .definition import DEFINITION as DEFINITION
from .ecs_command import MoveToNg, PutToBin

ENTRY_MODULES = (
    *composition.ENTRY_MODULES,
    "app.businesses.coarse_sorting.request.inbound_coarse_sorting_storage_allocate_v1",
    "app.businesses.coarse_sorting.request.inbound_coarse_sorting_execution_report_v1",
)
MODEL_MODULES = composition.MODEL_MODULES
COMMANDS = (*composition.COMMANDS, MoveToNg, PutToBin)
COMPONENTS = (COMPONENT,)
PROGRESSION = composition.PROGRESSION
LOCATIONS = TypeAdapter(dict[Reference, Reference])


def validate_configuration(settings) -> None:
    from app.reliable.config import parse_reliable_config, targets_for_protocol

    targets = parse_reliable_config(settings).targets
    for protocol in ("wms-decisions", "wms-facts", "wms-transport"):
        names = targets_for_protocol(targets, protocol)
        if len(names) != 1 or targets[names[0]].address_mode != "STATIC":
            raise ValueError(
                f"Coarse sorting integration requires one STATIC target for {protocol}"
            )
    for name in ("COARSE_SORTING_OUTPUT_LOCATIONS", "COARSE_SORTING_NG_LOCATIONS"):
        try:
            LOCATIONS.validate_python(getattr(settings, name), strict=True)
        except ValidationError:
            raise ValueError(f"{name} must map workline codes to valid locations") from None
    if settings.COARSE_SORTING_WMS_MOCK:
        from app.integrations.wms_request.adapter import (
            COARSE_MOCK_DECISIONS_TARGET,
            COARSE_MOCK_FACTS_TARGET,
        )

        line = mock_round_config.validate_python(settings.COARSE_SORTING_WMS_MOCK).workline_code
        if (
            line not in settings.COARSE_SORTING_OUTPUT_LOCATIONS
            or line not in settings.COARSE_SORTING_NG_LOCATIONS
        ):
            raise ValueError("Coarse sorting WMS mock requires both completion locations")
        for protocol, name in (
            ("wms-decisions", COARSE_MOCK_DECISIONS_TARGET),
            ("wms-facts", COARSE_MOCK_FACTS_TARGET),
        ):
            target = targets.get(name)
            if (
                target is None
                or target.protocol != protocol
                or not target.explicit_only
                or target.address_mode != "STATIC"
            ):
                raise ValueError(f"Coarse sorting WMS mock requires explicit STATIC target {name}")


@cache
def register_requests() -> None:
    """Register the candidate payloads only during the integration bootstrap."""
    from app.integrations.wms_request import Endpoint, request_operation

    from .contracts import ALLOCATE, REPORT
    from .request.inbound_coarse_sorting_execution_report_v1 import ExecutionReport
    from .request.inbound_coarse_sorting_storage_allocate_v1 import StorageAllocate

    request_operation(ALLOCATE, Endpoint.DECISIONS, requires_business_owner=True)(StorageAllocate)
    request_operation(REPORT, Endpoint.FACTS, requires_business_owner=True)(ExecutionReport)


def build_service(capabilities):
    from app.core import get_settings

    settings = get_settings()
    mock = None
    if settings.COARSE_SORTING_WMS_MOCK:
        mock = mock_round_config.validate_python(settings.COARSE_SORTING_WMS_MOCK)
    return replace(
        composition.build_rack_service(
            capabilities,
            output_locations=settings.COARSE_SORTING_OUTPUT_LOCATIONS,
            clock=lambda: datetime.now(UTC),
        ),
        ng_locations=settings.COARSE_SORTING_NG_LOCATIONS,
        mock_workline=mock.workline_code if mock else None,
        mock_round_id=mock.round_id if mock else None,
    )
