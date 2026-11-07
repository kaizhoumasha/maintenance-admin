"""Independent AUTO transactions with bounded whole-transaction retries."""

from sqlalchemy.exc import DBAPIError

from app.businesses._components.bin_roller.service import RackLockSetChanged
from app.businesses.runtime import Capabilities
from app.reliable.lifecycle import EvidencePersistenceError, database_failure_code

from .task_service import AutoTaskService


class AutoS1Transactions:
    def __init__(
        self, capabilities: Capabilities, *, task_flow=None, first_plan_wait_ms=None, service=None
    ):
        self.capabilities = capabilities
        self.host = (
            service
            if service is not None
            else AutoTaskService(wms=capabilities.wms, first_plan_wait_ms=first_plan_wait_ms)
        )
        self.task_flow = task_flow
        self.service = service

    async def advance(self, kind: str, record_id: str | int, *, now_ms: int):
        if self.service is None and kind not in {
            "PREPARE_NEXT",
            "PREPARE_RESULT",
            "TASK",
            "BIN_RELEASE",
        }:
            raise ValueError("unsupported AUTO S1 work kind")
        if self.service is None and kind in {"TASK", "BIN_RELEASE"} and self.task_flow is None:
            raise ValueError("AUTO S5 host assembly required")
        for attempt in range(3):
            try:
                async with self.capabilities.session_factory() as session, session.begin():
                    if session.get_bind().dialect.name == "postgresql":
                        await session.connection(
                            execution_options={
                                "isolation_level": "REPEATABLE READ"
                                if kind
                                in {
                                    "INBOUND_REPORT",
                                    "OUTBOUND_REPORT",
                                    "SCAN2_RELEASE",
                                    "BIN_RELEASE_RESULT",
                                    "ARCHIVE",
                                }
                                else "SERIALIZABLE"
                            }
                        )
                    if self.service is not None:
                        return await self.service.advance_original(
                            session, kind, record_id, now_ms=now_ms
                        )
                    if kind == "TASK":
                        return await self.task_flow.advance(
                            session, task_id=record_id, now_ms=now_ms
                        )
                    if kind == "BIN_RELEASE":
                        work_line_id, arrival_id = record_id
                        return await self.task_flow.release_bin(
                            session, work_line_id=work_line_id, arrival_id=arrival_id, now_ms=now_ms
                        )
                    if kind == "PREPARE_NEXT":
                        return await self.host.prepare_next(
                            session, work_line_id=record_id, now_ms=now_ms
                        )
                    return await self.host.advance_prepare(
                        session, task_id=record_id, now_ms=now_ms
                    )
            except RackLockSetChanged:
                if attempt == 2:
                    raise
            except (DBAPIError, EvidencePersistenceError) as error:
                if database_failure_code(error) != "40001" or attempt == 2:
                    raise
