from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

from ..contracts import Identifier, NonNegative


@request_operation("sorting.task.completion_confirm@v1", Endpoint.DECISIONS)
class CompletionConfirm(RequestPayload):
    task_id: Identifier
    last_applied_plan_revision: NonNegative
