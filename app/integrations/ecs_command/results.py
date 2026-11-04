"""Read original commands without sender or route assembly."""

from sqlalchemy import select

from app.models import EcsCommand


async def get_command(session, *, owner_domain: str, command_code: str):
    from .service import EcsCommands

    with session.no_autoflush:
        row = await session.scalar(
            select(EcsCommand).where(
                EcsCommand.owner_domain == owner_domain,
                EcsCommand.command_code == command_code,
            )
        )
        return await EcsCommands._view(session, row) if row is not None else None


async def get_message_ids(session, *, owner_domain, command_codes, candidate_ids):
    """Read original frozen command/message associations, without result consumption."""
    from app.models import ReliableMessage

    from .protocol import OPERATION

    if not command_codes or not candidate_ids:
        return {}
    with session.no_autoflush:
        rows = await session.execute(
            select(EcsCommand.command_code, ReliableMessage.id)
            .join(
                ReliableMessage,
                (ReliableMessage.target == EcsCommand.target)
                & (ReliableMessage.operation == OPERATION)
                & (ReliableMessage.operation_id == EcsCommand.command_code),
            )
            .where(
                EcsCommand.owner_domain == owner_domain,
                EcsCommand.command_code.in_(command_codes),
                ReliableMessage.id.in_(candidate_ids),
            )
        )
        return dict(rows.all())
