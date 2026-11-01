"""One shared contract declaration, assembled only by real consuming hosts."""

from app.businesses.components import ComponentDefinition
from app.businesses.definitions import InitialEntry

COMPONENT = ComponentDefinition(
    key="sorting_task",
    initial_entries=(InitialEntry(kind="wms_callback", name="sorting.task.issued@v1"),),
    entry_modules=(
        "app.businesses._components.sorting_task.callback.sorting_task_issued_v1",
        "app.businesses._components.sorting_task.callback.sorting_task_queue_changed_v1",
        "app.businesses._components.sorting_task.callback.sorting_task_plan_delta_v1",
        "app.businesses._components.sorting_task.callback.sorting_task_cancel_v1",
        "app.businesses._components.sorting_task.request.sorting_task_prepare_v1",
        "app.businesses._components.sorting_task.request.sorting_task_completion_confirm_v1",
    ),
    required_methods=("read_task_owner", "accept", "recheck"),
    requires_progression=True,
)
