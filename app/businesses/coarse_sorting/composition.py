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
    from .models import CoarseSortingScan
    from .request.inbound_coarse_sorting_validate_bind_v1 import ValidateBind
    from .service import CoarseSortingService

    return CoarseSortingService(CoarseSortingScan.__table__, capabilities, ValidateBind)
