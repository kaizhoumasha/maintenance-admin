"""One deployed composition, shared by API and worker in every environment."""

from .definition import DEFINITION as DEFINITION
from .ecs_command import MoveForward

ENTRY_MODULES = (
    "app.businesses.coarse_sorting.ecs_event.scan_completed",
    "app.businesses.coarse_sorting.request.inbound_coarse_sorting_validate_bind_v1",
)
MODEL_MODULES = ("app.businesses.coarse_sorting.models",)
COMMANDS = (MoveForward,)
PROGRESSION = "scan_pending"


def build_service(capabilities):
    from .models import CoarseSortingAction, CoarseSortingJob
    from .request.inbound_coarse_sorting_validate_bind_v1 import ValidateBind
    from .service import CoarseSortingService

    return CoarseSortingService(
        CoarseSortingJob.__table__, capabilities, ValidateBind, CoarseSortingAction.__table__
    )


def build_direct_put_service(capabilities, *, output_locations, clock):
    """Explicit assembly seam for candidate contracts outside the deployed catalog."""
    from dataclasses import replace

    from .request.inbound_coarse_sorting_execution_report_v1 import ExecutionReport
    from .request.inbound_coarse_sorting_storage_allocate_v1 import StorageAllocate
    from .service import DirectPutContracts

    return replace(
        build_service(capabilities),
        direct_put_contracts=DirectPutContracts(
            StorageAllocate, ExecutionReport, dict(output_locations)
        ),
        clock=clock,
    )


def build_ng_service(capabilities, *, ng_locations):
    """Explicit candidate assembly using trusted per-line NG destinations."""
    from dataclasses import replace

    from pydantic import TypeAdapter

    from .contracts import Reference

    locations = TypeAdapter(dict[str, Reference]).validate_python(dict(ng_locations), strict=True)
    return replace(build_service(capabilities), ng_locations=locations)


def build_rack_service(capabilities, *, output_locations, clock):
    """Candidate rack contracts are enabled only by explicit trusted assembly."""
    from dataclasses import replace

    from app.businesses._components.rack_dispatch.service import RackDispatch

    from .models import RackPreparation

    return replace(
        build_direct_put_service(capabilities, output_locations=output_locations, clock=clock),
        preparation_table=RackPreparation.__table__,
        rack_dispatch=RackDispatch("coarse_sorting", capabilities),
    )
