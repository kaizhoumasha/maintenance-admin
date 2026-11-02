"""The same complete manual host is assembled by API and worker."""

from app.businesses._components.bin_roller.composition import COMPONENT as BIN_ROLLER
from app.businesses._components.sorting_task.composition import COMPONENT as SORTING_TASK

from .definition import DEFINITION as DEFINITION

COMPONENTS = (SORTING_TASK, BIN_ROLLER)
ENTRY_MODULES = (
    "app.businesses.smt_manual.callback.manual_bin_work_completed_v1",
    "app.businesses.smt_manual.callback.outbound_manual_rack_direct_pick_completed_v1",
    "app.businesses.smt_manual.callback.outbound_return_rack_task_completed_v1",
    "app.businesses.smt_manual.request.manual_bin_work_admission_decide_v1",
    "app.businesses.smt_manual.request.outbound_return_rack_arrival_report_v1",
)
MODEL_MODULES = ("app.businesses.smt_manual.models",)
COMMANDS = ()
PROGRESSION = "scan_pending"


def build_service(capabilities):
    from app.core import get_settings

    from .service import SmtManualService

    return SmtManualService(
        capabilities, first_plan_wait_ms=get_settings().SMT_MANUAL_FIRST_PLAN_WAIT_MS
    )
