"""One deployed composition, shared by API and worker in every environment."""

from .definition import DEFINITION as DEFINITION

ENTRY_MODULES = ("app.businesses.coarse_sorting.ecs_event.scan_completed",)
MODEL_MODULES = ("app.businesses.coarse_sorting.models",)
COMMANDS = ()


def build_service(capabilities):
    from .models import CoarseSortingScan
    from .service import CoarseSortingService

    return CoarseSortingService(CoarseSortingScan.__table__)
