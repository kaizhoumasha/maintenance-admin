import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identifiers import uuid7
from app.core.strict_json import canonical_digest
from app.integrations.model_payload import validated_model_payload
from app.models import EcsCommand, EcsResultEvidence
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
        self, outbox, target, routes=None, annexes=None, *, clock=lambda: datetime.now(UTC)
    ):
        self.outbox, self.target = outbox, target
        self.routes = routes or UnconfiguredRoutes()
        self.annexes = annexes or {}
        self.clock = clock

    async def create(
        self,
        session,
        *,
        owner,
        call_key,
        device_code,
        timeout_ms,
        command,
    ):
        declaration = command_declaration(type(command))
        binding = declaration.bindings[0]
        params = validated_model_payload(command)

        async def resolve_route():
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
        query = select(EcsCommand).where(
            EcsCommand.owner_domain == owner.domain, EcsCommand.call_key_digest == key
        )

        def compare(row):
            if row.call_key != call_key or row.input_raw != raw_input:
                raise ValueError("business identity has different input")

        row = await session.scalar(query)
        if row is not None:
            compare(row)
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
            command_code=code,
            owner_domain=owner.domain,
            call_key=call_key,
            call_key_digest=key,
            input_raw=raw_input,
            device_code=device_code,
            task_type=task_type,
            route=frozen_route,
            request_raw=raw,
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
                insert(EcsCommand)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["owner_domain", "call_key_digest"])
                .returning(EcsCommand.command_code)
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
                    ),
                )
        return await self._view(session, row)

    async def _view(self, session: AsyncSession, row: EcsCommand) -> CommandView:
        result = None
        if row.applied_result_id:
            evidence = await session.scalar(
                select(EcsResultEvidence).where(
                    EcsResultEvidence.command_code == row.command_code,
                    EcsResultEvidence.digest == row.applied_result_id,
                )
            )
            result = evidence.core
        return CommandView(
            row.command_code,
            row.status,
            row.delivery_status,
            row.ack_raw is not None,
            result,
            row.hold_reason,
            row.needs_reconciliation,
        )

    async def get_command(
        self, session: AsyncSession, *, owner: CommandOwner, command_code: str
    ) -> CommandView | None:
        row = await session.scalar(
            select(EcsCommand).where(
                EcsCommand.command_code == command_code, EcsCommand.owner_domain == owner.domain
            )
        )
        return await self._view(session, row) if row else None

    async def get_by_call(
        self, session: AsyncSession, *, owner: CommandOwner, call_key: str
    ) -> CommandView | None:
        row = await session.scalar(
            select(EcsCommand).where(
                EcsCommand.owner_domain == owner.domain,
                EcsCommand.call_key_digest == hashlib.sha256(call_key.encode()).hexdigest(),
            )
        )
        if row is not None and row.call_key != call_key:
            raise ValueError("business key digest collision")
        return await self._view(session, row) if row else None
