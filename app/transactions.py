"""Best-effort notifications after committed work; durable work owns recovery."""

import logging
from collections.abc import Callable

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.diagnostics import log_exception

logger = logging.getLogger(__name__)
_KEY = "committed_notifications"


def _committed(session):
    transaction = session.get_nested_transaction() or session.get_transaction()
    callbacks = session.info[_KEY].pop(transaction, [])
    if transaction.nested:
        session.info[_KEY].setdefault(transaction.parent, []).extend(callbacks)
        return
    for callback in callbacks:
        try:
            callback()
        except Exception as error:
            log_exception(
                logger,
                "Committed notification unavailable; durable work remains pending",
                error,
                component="transactions",
                phase="after_commit",
            )


def _rolled_back(session):
    transaction = session.get_nested_transaction() or session.get_transaction()
    session.info[_KEY].pop(transaction, None)


def _ended(session, transaction):
    session.info[_KEY].pop(transaction, None)


def after_commit(session: AsyncSession, callback: Callable[[], None]) -> None:
    """Queue under the current savepoint; only an outer commit may publish."""
    sync = session.sync_session
    transaction = sync.get_nested_transaction() or sync.get_transaction()
    if transaction is None:
        raise RuntimeError("A notification requires an active transaction")
    if _KEY not in sync.info:
        sync.info[_KEY] = {}
        event.listen(sync, "after_commit", _committed)
        event.listen(sync, "after_rollback", _rolled_back)
        event.listen(sync, "after_transaction_end", _ended)
    sync.info[_KEY].setdefault(transaction, []).append(callback)
