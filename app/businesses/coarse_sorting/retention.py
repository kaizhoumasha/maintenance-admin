"""Original WMS evidence retained by pending coarse responsibilities."""

from sqlalchemy import select, union_all

from .models import CoarseSortingAction, CoarseSortingJob, RackPreparation


async def wms_message_references(session, *, candidate_ids, line_ids=None):
    action, job, prep = CoarseSortingAction, CoarseSortingJob, RackPreparation
    queries = (
        select(job.work_line_id, action.message_id)
        .join(action, action.job_id == job.id)
        .where(
            job.pending,
            action.kind.in_(("ALLOCATE_STORAGE", "REPORT_COMPLETION")),
            action.message_id.in_(candidate_ids),
        ),
        select(prep.work_line_id, action.message_id)
        .join(action, action.id == prep.source_allocation_action_id)
        .where(
            prep.state.not_in(("READY", "DEPART_ACCEPTED")), action.message_id.in_(candidate_ids)
        ),
    )
    if line_ids is not None:
        queries = tuple(
            query.where(owner.work_line_id.in_(line_ids))
            for query, owner in zip(queries, (job, prep), strict=True)
        )
    return tuple(await session.execute(union_all(*queries)))
