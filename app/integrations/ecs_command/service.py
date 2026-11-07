import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identifiers import uuid7
from app.core.strict_json import canonical_digest
from app.integrations.model_payload import validated_model_payload
from app.models import IntegrationExchange, IntegrationRecord
from app.reliable import MessageDraft
from app.reliable.lifecycle import ensure_savepoint_transaction
from app.reliable.types import DeliverySnapshot

from .contracts import PROTOCOL_RETRY_WINDOW, CommandEnvelope, CommandView, merge_annexes
from .declaration import command_declaration
from .protocol import OPERATION, PROTOCOL
from .routing import UnconfiguredRoutes, retry_window_error


@dataclass(frozen=True)
class CommandOwner:
    domain: str

    def __post_init__(self):
        if not self.domain or "\x00" in self.domain:
            raise ValueError("invalid command owner")


class EcsCommands:
    def __init__(
        self,
        outbox,
        target,
        routes=None,
        annexes=None,
        *,
        clock=lambda: datetime.now(UTC),
        authorize_new=None,
    ):
        self.outbox, self.target = outbox, target
        self.routes = routes or UnconfiguredRoutes()
        self.annexes = annexes or {}
        self.clock = clock
        self.authorize_new = authorize_new

    async def create(
        self,
        session,
        *,
        owner,
        call_key,
        device_code,
        timeout_ms,
        command,
        scope=None,
    ):
        if scope is not None and scope.owner_domain != owner.domain:
            raise ValueError("command scope owner mismatch")
        declaration = command_declaration(type(command))
        binding = declaration.bindings[0]
        params = validated_model_payload(command)

        async def resolve_route():
            if self.authorize_new is not None:
                await self.authorize_new(session, owner, device_code, type(command))
            route = await self.routes.resolve_declared(session, device_code, declaration)
            annex_key = (binding.annex_id, binding.annex_version)
            if annex_key != (route.annex_id, route.annex_version):
                raise ValueError("declared command does not match active route annex")
            models = self.annexes.get(annex_key)
            if models is None:
                raise ValueError("confirmed annex required")
            merge_annexes(
                {annex_key: models}, {annex_key: declaration.annex}, source="declared command"
            )
            return route

        return await self._enqueue(
            session,
            owner=owner,
            call_key=call_key,
            device_code=device_code,
            task_type=binding.task_type,
            timeout_ms=timeout_ms,
            params=params,
            resolve_route=resolve_route,
            scope=scope,
        )

    async def _enqueue(
        self,
        session,
        *,
        owner,
        call_key,
        device_code,
        task_type,
        timeout_ms,
        params,
        resolve_route,
        scope=None,
    ):
        if (
            not isinstance(owner, CommandOwner)
            or not isinstance(call_key, str)
            or not call_key
            or "\x00" in call_key
        ):
            raise ValueError("invalid business identity")
        key = hashlib.sha256(call_key.encode()).hexdigest()
        raw_input = json.dumps(
            dict(device_code=device_code, task_type=task_type, timeout=timeout_ms, params=params),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        query = select(IntegrationExchange).where(
            IntegrationExchange.role == "ECS_COMMAND",
            IntegrationExchange.owner_domain == owner.domain,
            IntegrationExchange.call_key_digest == key,
        )

        def compare(row):
            if row.call_key != call_key or row.input_raw != raw_input:
                raise ValueError("business identity has different input")

        row = await session.scalar(query)
        if row is not None:
            compare(row)
            await self.outbox.check_scope(
                session,
                target=row.target,
                operation=OPERATION,
                operation_id=row.command_code,
                scope=scope,
            )
            return await self._view(session, row)
        route = await resolve_route()
        if route.device_code != device_code or task_type not in route.commands:
            raise ValueError("route/device/action mismatch")
        if (
            not isinstance(route.retry_window, timedelta)
            or route.retry_window != PROTOCOL_RETRY_WINDOW
        ):
            raise retry_window_error()
        models = self.annexes.get((route.annex_id, route.annex_version))
        if models is None:
            raise ValueError("confirmed annex required")
        canonical_digest(params)
        # Callers validate original models; serialized JSON is not Python model input.
        if (
            type(timeout_ms) is not int
            or not models.timeout_min_ms <= timeout_ms <= models.timeout_max_ms
        ):
            raise ValueError("timeout is outside the confirmed annex range")
        now = self.clock()
        code = str(uuid7())
        envelope = CommandEnvelope(
            device_code=device_code,
            command_code=code,
            task_type=task_type,
            priority=1,
            timeout=timeout_ms,
            timestamp=int(now.timestamp() * 1000),
            params=params,
        )
        raw = envelope.model_dump_json().encode()
        if len(raw) > 262144 or any("\x00" in v for v in (device_code, task_type)):
            raise ValueError("invalid command payload")
        snapshot = DeliverySnapshot(route.origin, PROTOCOL, "HOLD")
        frozen_route = asdict(route)
        frozen_route["retry_window"] = route.retry_window.total_seconds()
        values = dict(
            role="ECS_COMMAND",
            peer_system="ECS",
            protocol=PROTOCOL,
            direction="OUTBOUND",
            interaction_kind="COMMAND",
            identity_kind="COMMAND_CODE",
            identity_value=code,
            target_key=self.target,
            message_type=task_type,
            command_code=code,
            owner_domain=owner.domain,
            call_key=call_key,
            call_key_digest=key,
            input_raw=raw_input,
            device_code=device_code,
            task_type=task_type,
            route=frozen_route,
            request_sha256=hashlib.sha256(raw).hexdigest(),
            target=self.target,
            delivery_snapshot=asdict(snapshot),
            expires_at=now + route.retry_window,
            created_at=now,
            status="QUEUED",
            delivery_status="PENDING",
            blocked=False,
            needs_reconciliation=False,
        )
        insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
        # Savepoint isolates a failed enqueue without rolling back the caller's work.
        await ensure_savepoint_transaction(session)
        async with session.begin_nested():
            winner = await session.scalar(
                insert(IntegrationExchange)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=["owner_domain", "call_key_digest"],
                    index_where=text("role='ECS_COMMAND'"),
                )
                .returning(IntegrationExchange.id)
            )
            row = await session.scalar(query.execution_options(populate_existing=True))
            compare(row)
            if winner is not None:
                await self.outbox.enqueue(
                    session,
                    MessageDraft(
                        self.target,
                        OPERATION,
                        code,
                        raw,
                        ttl=route.retry_window,
                        business_key=code,
                        delivery_snapshot=snapshot,
                        expires_at=values["expires_at"],
                        scope=scope,
                        exchange_id=row.id,
                    ),
                )
            else:
                await self.outbox.check_scope(
                    session,
                    target=row.target,
                    operation=OPERATION,
                    operation_id=row.command_code,
                    scope=scope,
                )
        return await self._view(session, row)

    @staticmethod
    async def _view(session: AsyncSession, row: IntegrationExchange) -> CommandView:
        from app.models import ReliableMessage

        message_id = await session.scalar(
            select(ReliableMessage.id).where(
                ReliableMessage.target == row.target,
                ReliableMessage.operation == OPERATION,
                ReliableMessage.operation_id == row.command_code,
            )
        )
        result = None
        if row.applied_result_id:
            evidence = await session.scalar(
                select(IntegrationRecord).where(
                    IntegrationRecord.exchange_id == row.id,
                    IntegrationRecord.record_kind == "RESULT_EVIDENCE",
                    IntegrationRecord.semantic_digest == row.applied_result_id,
                )
            )
            result = evidence.effective_payload
        return CommandView(
            row.command_code,
            row.status,
            row.delivery_status,
            row.ack_record_id is not None,
            result,
            row.hold_reason,
            row.needs_reconciliation,
            message_id=message_id,
            task_type=row.task_type,
            device_code=row.device_code,
        )

    async def get_command(
        self, session: AsyncSession, *, owner: CommandOwner, command_code: str
    ) -> CommandView | None:
        from .results import get_command

        return await get_command(session, owner_domain=owner.domain, command_code=command_code)

    async def get_by_call(
        self, session: AsyncSession, *, owner: CommandOwner, call_key: str
    ) -> CommandView | None:
        row = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.role == "ECS_COMMAND",
                IntegrationExchange.owner_domain == owner.domain,
                IntegrationExchange.call_key_digest
                == hashlib.sha256(call_key.encode()).hexdigest(),
            )
        )
        if row is not None and row.call_key != call_key:
            raise ValueError("business key digest collision")
        return await self._view(session, row) if row else None
