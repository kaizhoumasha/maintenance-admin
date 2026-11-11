from app.integrations.wms_request import RequestPayload

from .contracts import Identifier, NonnegativeInt


class WorklineRequest(RequestPayload):
    workline_code: Identifier


class ReportRequest(WorklineRequest):
    occurred_at: NonnegativeInt
