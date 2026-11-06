"""Candidate allocation contract. Formal registration awaits WMS signoff."""

from app.integrations.wms_request import RequestPayload

from ..contracts import Reference


class StorageAllocate(RequestPayload):
    workline_code: Reference
    binding_id: Reference
