from typing import Annotated

from pydantic import ConfigDict, Field, RootModel

from app.businesses._components.sorting_task.contracts import Identifier, NonNegative
from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

from ..outbound_contracts import BinLocator, RackLocator


class BinEmpty(RequestPayload):
    task_id: Identifier
    source_locator: BinLocator
    observed_at: NonNegative


class RackEmpty(RequestPayload):
    task_id: Identifier
    source_locator: RackLocator
    plan_revision: Annotated[int, Field(ge=1, le=2**63 - 1)]
    observed_at: NonNegative


@request_operation("outbound.source.empty_decide@v1", Endpoint.DECISIONS)
class EmptyDecide(RootModel[BinEmpty | RackEmpty], RequestPayload):
    model_config = ConfigDict(strict=True, extra=None)
