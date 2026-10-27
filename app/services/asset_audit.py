from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


@dataclass(frozen=True)
class AssetActor:
    user_id: int
    trace_id: str
    ip: str | None
    user_agent: str | None


async def record_change(
    session: AsyncSession,
    *,
    actor: AssetActor,
    action: str,
    resource: str,
    resource_id: int,
    before: dict,
    after: dict,
    reason: str | None = None,
) -> None:
    session.add(
        AuditLog(
            user_id=actor.user_id,
            trace_id=actor.trace_id,
            action=action,
            resource=resource,
            resource_id=resource_id,
            ip=actor.ip,
            user_agent=actor.user_agent[:255] if actor.user_agent else None,
            detail={"before": before, "after": after, **({"reason": reason} if reason else {})},
        )
    )
    await session.flush()


@asynccontextmanager
async def mutation(session: AsyncSession):
    try:
        yield
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
