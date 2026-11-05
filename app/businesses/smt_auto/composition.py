"""Actual AUTO assembly for isolated validation before formal catalog admission."""

from app.businesses._components.bin_roller.composition import COMPONENT as BIN_ROLLER
from app.businesses._components.rack_dispatch.composition import COMPONENT as RACK_DISPATCH
from app.businesses._components.sorting_task.composition import COMPONENT as SORTING_TASK

from .definition import DEFINITION as DEFINITION
from .ecs_command import PickAndPut

COMPONENTS = (SORTING_TASK, BIN_ROLLER, RACK_DISPATCH)
COMMANDS = (PickAndPut,)
MODEL_MODULES = ("app.businesses.smt_auto.models",)
ENTRY_MODULES = (
    "app.businesses.smt_auto.callback.putaway_source_rack_exhausted_v1",
    "app.businesses.smt_auto.request.putaway_work_plan_v1",
    "app.businesses.smt_auto.request.putaway_material_decide_v1",
    "app.businesses.smt_auto.request.putaway_material_movement_report_v1",
    "app.businesses.smt_auto.request.putaway_source_empty_decide_v1",
    "app.businesses.smt_auto.request.outbound_bin_work_plan_v1",
    "app.businesses.smt_auto.request.outbound_return_rack_work_plan_v1",
    "app.businesses.smt_auto.request.outbound_bin_material_decide_v1",
    "app.businesses.smt_auto.request.outbound_rackslot_material_decide_v1",
    "app.businesses.smt_auto.request.outbound_material_movement_report_v1",
    "app.businesses.smt_auto.request.outbound_source_empty_decide_v1",
)
PROGRESSION = "scan_pending"


def build_service(capabilities):
    from app.core.config import get_settings

    from .service import SmtAutoService

    settings = get_settings()
    return SmtAutoService(
        capabilities,
        worklines=settings.SMT_AUTO_WORKLINES,
        first_plan_wait_ms=settings.SMT_AUTO_FIRST_PLAN_WAIT_MS,
    )
