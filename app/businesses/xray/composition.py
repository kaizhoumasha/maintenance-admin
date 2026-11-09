from app.businesses._components.rack_dispatch.composition import COMPONENT as RACK_DISPATCH

from .definition import DEFINITION as DEFINITION
from .ecs_command import (
    ApplyCountDecision,
    ApplyPkgDecision,
    ApplyVerifyDecision,
    PutToBin,
    PutToRack,
)

ENTRY_MODULES = (
    "app.businesses.xray.ecs_event.scan_completed",
    "app.businesses.xray.ecs_event.xray_count_completed",
    "app.businesses.xray.ecs_event.material_arrived",
    "app.businesses.xray.ecs_event.ng_discharged",
    "app.businesses.xray.ecs_event.xray_ng_requested",
    "app.businesses.xray.request.inbound_xray_pkg_decide_v1",
    "app.businesses.xray.request.inbound_xray_count_decide_v1",
    "app.businesses.xray.request.inbound_xray_verify_decide_v1",
    "app.businesses.xray.request.inbound_xray_put_target_decide_v1",
    "app.businesses.xray.request.inbound_xray_put_report_v1",
    "app.businesses.xray.request.inbound_xray_ng_report_v1",
)
MODEL_MODULES = ("app.businesses.xray.models",)
COMMANDS = (ApplyPkgDecision, ApplyCountDecision, ApplyVerifyDecision, PutToRack, PutToBin)
PROGRESSION = "scan_pending"
COMPONENTS = (RACK_DISPATCH,)


def build_service(capabilities):
    from app.businesses._components.rack_dispatch.service import RackDispatch
    from app.core import get_settings

    from .contracts import BinActual
    from .racks import RackPoint, XrayRacks
    from .request import inbound_xray_count_decide_v1 as count_decide
    from .request import inbound_xray_ng_report_v1 as ng_report
    from .request import inbound_xray_pkg_decide_v1 as pkg_decide
    from .request import inbound_xray_put_report_v1 as put_report
    from .request import inbound_xray_put_target_decide_v1 as put_target_decide
    from .request import inbound_xray_verify_decide_v1 as verify_decide
    from .service import XrayService

    mappings = {}
    for line, targets in get_settings().XRAY_TRANSIT_TARGETS.items():
        if set(targets) != set("ABCD"):
            raise ValueError("XRAY_TRANSIT_TARGETS requires A, B, C and D for each configured line")
        mapped = {slot: BinActual.model_validate(target) for slot, target in targets.items()}
        if any(slot != target.bin_location for slot, target in mapped.items()):
            raise ValueError("XRAY_TRANSIT_TARGETS slot must match bin_location")
        mappings[line] = mapped
    requests = {
        "ENTRY": (pkg_decide.OPERATION, pkg_decide.PkgDecide),
        "COUNT": (count_decide.OPERATION, count_decide.CountDecide),
        "VERIFY": (verify_decide.OPERATION, verify_decide.VerifyDecide),
        "OUTLET": (put_target_decide.OPERATION, put_target_decide.PutTargetDecide),
        "PUT_REPORT": (put_report.OPERATION, put_report.PutReport),
        "NG_REPORT": (ng_report.OPERATION, ng_report.NgReport),
    }
    racks = XrayRacks(
        RackDispatch(DEFINITION.code, capabilities),
        {
            line: RackPoint.model_validate(config)
            for line, config in get_settings().XRAY_RETURN_RACKS.items()
        },
    )
    return XrayService(capabilities, mappings, requests, racks)
