import logging

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _log_diagnostics(sync_session):
    transaction = sync_session.get_nested_transaction() or sync_session.get_transaction()
    queued = sync_session.info["callback_diagnostics"]
    diagnostics = queued.pop(transaction, [])
    if transaction.nested:
        queued.setdefault(transaction.parent, []).extend(diagnostics)
        return
    for detail in diagnostics:
        logger.warning("callback diagnostic %r", detail, extra=detail)


def _drop_diagnostics(sync_session):
    transaction = sync_session.get_nested_transaction() or sync_session.get_transaction()
    sync_session.info["callback_diagnostics"].pop(transaction, None)


def queue_diagnostics(session: AsyncSession, details: list[dict]) -> None:
    """按当前(嵌套)事务排队诊断:提交后输出,回滚则丢弃,保存点释放时并入父事务。"""
    sync_session = session.sync_session
    if "callback_diagnostics" not in sync_session.info:
        sync_session.info["callback_diagnostics"] = {}
        event.listen(sync_session, "after_commit", _log_diagnostics)
        event.listen(sync_session, "after_rollback", _drop_diagnostics)
    transaction = sync_session.get_nested_transaction() or sync_session.get_transaction()
    sync_session.info["callback_diagnostics"].setdefault(transaction, []).extend(details)
