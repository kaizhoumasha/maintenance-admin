from app.businesses.smt_auto.models import AutoTask
from app.businesses.smt_auto.queue import Cancellation
from app.businesses.smt_auto.service import AutoTaskQueue


def test_queue_orders_ready_tasks_and_skips_future_tasks():
    tasks = [
        AutoTask(
            task_id="b",
            business_type="outbound",
            work_line_id=1,
            workline_code="L1",
            issued_operation_id="o",
            issued_timestamp=2,
            issued_data={},
            dispatch_sequence=1,
            not_before_ms=10,
        ),
        AutoTask(
            task_id="a",
            business_type="inbound",
            work_line_id=1,
            workline_code="L1",
            issued_operation_id="o",
            issued_timestamp=1,
            issued_data={},
            dispatch_sequence=1,
        ),
    ]
    assert [t.task_id for t in AutoTaskQueue().eligible(tasks, now_ms=0)] == ["a"]


def test_cancellation_is_idempotent_and_stops_pending_work():
    task = AutoTask(
        task_id="a",
        business_type="inbound",
        work_line_id=1,
        workline_code="L1",
        issued_operation_id="o",
        issued_timestamp=42,
        issued_data={},
        dispatch_sequence=1,
    )
    cancellation = Cancellation("a", "operator")
    cancellation.apply(task, closed_at_ms=99)
    cancellation.apply(task, closed_at_ms=100)
    assert (task.status, task.pending, task.closed_at_ms, task.cancel_reason) == (
        "CANCELLED",
        False,
        99,
        "operator",
    )
