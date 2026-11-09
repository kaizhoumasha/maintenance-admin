from app.integrations.wms_request import RequestPayload

from .contracts import Identifier, NonnegativeInt


class ReelRequest(RequestPayload):
    workline_code: Identifier
    reel_id: Identifier


class EventRequest(ReelRequest):
    source_event_id: Identifier


class ReportRequest(ReelRequest):
    occurred_at: NonnegativeInt
