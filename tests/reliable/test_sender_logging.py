import asyncio
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import ProgrammingError

from app.reliable.sender import Sender


def database_error(code, message="private_driver_message", *, constraint=None):
    cause = RuntimeError(message)
    cause.sqlstate = code
    cause.constraint_name = constraint
    return ProgrammingError("private_sql", {"secret": "private_parameter"}, cause)


async def scan_script(caplog, steps, *, busy_steps=()):
    """每次轮询执行一个步骤;成功步骤仍经过原来的等待路径。"""
    index = 0
    now = 0
    waits = []
    calls = []

    async def stage(phase):
        calls.append((index, phase))
        failing_phase, error, _ = steps[index]
        if phase == failing_phase:
            raise error
        if phase == "recover_expired" and index in busy_steps:
            sender.inflight = {object(): None for _ in range(sender.budget.max_concurrency)}
        return [] if phase == "claim" else 0

    store = SimpleNamespace(
        maintain_snapshots=lambda *a: stage("maintain_snapshots"),
        expire_pending=lambda *a: stage("expire_pending"),
        recover_expired=lambda *a: stage("recover_expired"),
        claim=lambda *a: stage("claim"),
    )

    async def wait(seconds):
        nonlocal index, now
        waits.append(seconds)
        sender.inflight.clear()
        now += steps[index][2]
        index += 1
        if index == len(steps):
            sender.stopping = True
        await asyncio.sleep(0)

    sender = Sender(store, {}, {}, {}, monotonic=lambda: now, wait=wait)
    with caplog.at_level(logging.INFO, logger="app.reliable.sender"):
        async with asyncio.timeout(2):
            await sender.run()
    assert waits == [sender.budget.poll_interval_s] * len(steps)
    return [r for r in caplog.records if r.name == "app.reliable.sender"], calls


@pytest.mark.parametrize(
    "phase", ["maintain_snapshots", "expire_pending", "recover_expired", "claim"]
)
async def test_scan_logs_failure_phase_and_recovers_once(caplog, phase):
    records, calls = await scan_script(
        caplog,
        [(phase, database_error("42703"), 1), (None, None, 1), (None, None, 1)],
    )
    failed = [r for r in records if "scan failed" in r.getMessage()]
    recovered = [r for r in records if "scan recovered" in r.getMessage()]
    assert len(failed) == len(recovered) == 1
    assert failed[0].phase == recovered[0].phase == phase
    assert "42703" in failed[0].getMessage() and "Traceback" in failed[0].getMessage()
    assert recovered[0].failure_count == 1
    assert "private_" not in caplog.text
    assert (1, "claim") in calls and (2, "claim") in calls


async def test_repeated_scan_errors_summarize_without_changing_polling(caplog):
    records, calls = await scan_script(
        caplog,
        [("claim", database_error("42703"), 20)] * 5 + [(None, None, 1)],
    )
    failed = [r for r in records if "scan failed" in r.getMessage()]
    summaries = [r for r in records if "scan failure summary" in r.getMessage()]
    recovered = [r for r in records if "scan recovered" in r.getMessage()]
    assert len(failed) == 1 and len(summaries) == 1 and len(recovered) == 1
    assert "Traceback" not in summaries[0].getMessage()
    assert summaries[0].suppressed_count == 3
    assert summaries[0].failure_count == 4
    assert recovered[0].failure_count == 5 and recovered[0].suppressed_count == 1
    assert len([c for c in calls if c[1] == "claim"]) == 6


@pytest.mark.parametrize("changed", ["phase", "sqlstate", "type", "location"])
async def test_changed_error_is_reported_immediately_without_false_recovery(caplog, changed):
    phase = "claim" if changed == "phase" else "expire_pending"
    error = (
        RuntimeError("private_error")
        if changed == "type"
        else database_error("42P01" if changed == "sqlstate" else "42703")
    )
    # 不同抛出位置必须能区分,即便异常类型和诊断码相同。
    if changed == "location":
        try:
            raise error
        except ProgrammingError:
            pass
    records, _ = await scan_script(
        caplog,
        [("expire_pending", database_error("42703"), 1)] * 2 + [(phase, error, 1)],
    )
    assert len([r for r in records if "scan failed" in r.getMessage()]) == 2
    assert not any("scan recovered" in r.getMessage() for r in records)
    summaries = [r for r in records if "scan failure summary" in r.getMessage()]
    assert len(summaries) == 1 and summaries[0].suppressed_count == 1
    assert "private_" not in caplog.text


async def test_cancellation_is_not_a_failure_or_recovery(caplog):
    with pytest.raises(asyncio.CancelledError):
        await scan_script(caplog, [("claim", asyncio.CancelledError(), 1)])
    assert not any("scan failed" in r.getMessage() for r in caplog.records)
    assert not any("scan recovered" in r.getMessage() for r in caplog.records)


async def test_skipped_claim_does_not_report_claim_recovery(caplog):
    records, calls = await scan_script(
        caplog,
        [
            ("claim", database_error("42703"), 1),
            (None, None, 1),
            ("claim", database_error("42703"), 1),
            (None, None, 1),
        ],
        busy_steps=(1,),
    )
    assert (1, "claim") not in calls
    assert len([r for r in records if "scan failed" in r.getMessage()]) == 1
    recovered = [r for r in records if "scan recovered" in r.getMessage()]
    assert len(recovered) == 1 and recovered[0].failure_count == 2


async def test_failure_after_recovery_starts_new_diagnostic(caplog):
    records, _ = await scan_script(
        caplog,
        [
            ("claim", database_error("42703"), 1),
            (None, None, 1),
            ("claim", database_error("42703"), 1),
        ],
    )
    assert len([r for r in records if "scan failed" in r.getMessage()]) == 2
    assert len([r for r in records if "scan recovered" in r.getMessage()]) == 1


async def test_shutdown_error_identifies_cleanup_phase(caplog):
    class Client:
        async def aclose(self):
            raise RuntimeError("private_close_error")

    sender = Sender(object(), {}, {}, {"peer": Client()})
    with caplog.at_level(logging.ERROR, logger="app.reliable.sender"):
        await sender.aclose()
    records = [r for r in caplog.records if "task failed" in r.getMessage()]
    assert len(records) == 1 and records[0].phase == "HTTP close"
    assert "Traceback" in records[0].getMessage()
    assert "private_close_error" not in caplog.text


@pytest.mark.parametrize(
    "code,message,missing",
    [
        (
            "42703",
            "column reliable_message.delivery_snapshot does not exist",
            "reliable_message.delivery_snapshot",
        ),
        (
            "42P01",
            'relation "enterprise_backend.ecs_command" does not exist',
            "enterprise_backend.ecs_command",
        ),
        ("42703", 'column "delivery_snapshot" does not exist', "delivery_snapshot"),
    ],
)
async def test_summary_contains_standalone_safe_database_diagnosis(caplog, code, message, missing):
    records, _ = await scan_script(
        caplog,
        [("maintain_snapshots", database_error(code, message), 60)] * 2,
    )
    summary = next(r.getMessage() for r in records if "scan failure summary" in r.getMessage())
    assert f"sqlstate={code}" in summary and "driver_type=RuntimeError" in summary
    assert f"missing={missing}" in summary
    assert "origin=app/reliable/sender.py:" in summary
    assert "alembic upgrade head" in summary
    assert "Traceback" not in summary and "private_" not in summary


async def test_changed_error_summary_keeps_previous_diagnosis(caplog):
    records, _ = await scan_script(
        caplog,
        [("claim", database_error("42703", "column old_column does not exist"), 1)] * 2
        + [("claim", database_error("42P01", "relation new_table does not exist"), 1)],
    )
    summary = next(r.getMessage() for r in records if "scan failure summary" in r.getMessage())
    assert "sqlstate=42703" in summary and "missing=old_column" in summary
    assert "new_table" not in summary and "42P01" not in summary


@pytest.mark.parametrize(
    "code,message",
    [
        ("42703", 'column "private_payload with spaces" does not exist'),
        ("42703", "column safe_column does not exist; private_payload"),
        ("23505", "column private_payload does not exist"),
        ("42703", "private_driver_message"),
    ],
)
async def test_summary_never_falls_back_to_raw_error_message(caplog, code, message):
    records, _ = await scan_script(
        caplog,
        [("claim", database_error(code, message), 60)] * 2,
    )
    summary = next(r.getMessage() for r in records if "scan failure summary" in r.getMessage())
    assert f"sqlstate={code}" in summary and "origin=" in summary
    assert "private_" not in caplog.text and "missing=" not in summary


async def test_sender_instances_do_not_share_failure_or_recovery_state(caplog):
    await scan_script(caplog, [("claim", database_error("42703"), 1)] * 2)
    records, _ = await scan_script(caplog, [("claim", database_error("42703"), 1), (None, None, 1)])
    assert len([r for r in records if "scan failed" in r.getMessage()]) == 2
    recovered = [r for r in records if "scan recovered" in r.getMessage()]
    assert len(recovered) == 1 and recovered[0].failure_count == 1


@pytest.mark.parametrize("field,code", [("missing", "42703"), ("constraint", "23505")])
async def test_changed_database_object_is_reported_before_summary_interval(caplog, field, code):
    if field == "missing":
        old = database_error(code, "column old_object does not exist")
        new = database_error(code, "column new_object does not exist")
    else:
        old = database_error(code, constraint="old_object")
        new = database_error(code, constraint="new_object")
    records, calls = await scan_script(
        caplog,
        [("claim", old, 1)] * 2 + [("claim", new, 1)] * 2 + [(None, None, 1)],
    )
    failed = [r for r in records if "scan failed" in r.getMessage()]
    assert len(failed) == 2
    assert [getattr(r, field) for r in failed] == ["old_object", "new_object"]
    summaries = [r for r in records if "scan failure summary" in r.getMessage()]
    assert len(summaries) == 1 and getattr(summaries[0], field) == "old_object"
    assert summaries[0].failure_count == 2 and summaries[0].suppressed_count == 1
    assert "reason=changed" in summaries[0].getMessage()
    recovered = [r for r in records if "scan recovered" in r.getMessage()]
    assert len(recovered) == 1 and getattr(recovered[0], field) == "new_object"
    assert recovered[0].failure_count == 2 and recovered[0].suppressed_count == 1
    assert len([c for c in calls if c[1] == "claim"]) == 5
    assert "private_" not in caplog.text


@pytest.mark.parametrize("field,code", [("missing", "42703"), ("constraint", "23505")])
async def test_same_safe_database_object_still_suppresses_message_variants(caplog, field, code):
    if field == "missing":
        first = database_error(code, "column stable_column does not exist")
        second = database_error(code, 'column "stable_column" does not exist')
    else:
        first = database_error(code, "private_first_driver_message", constraint="stable_constraint")
        second = database_error(
            code, "private_second_driver_message", constraint="stable_constraint"
        )
    records, _ = await scan_script(
        caplog, [("claim", first, 60), ("claim", second, 1), (None, None, 1)]
    )
    assert len([r for r in records if "scan failed" in r.getMessage()]) == 1
    summaries = [r for r in records if "scan failure summary" in r.getMessage()]
    assert len(summaries) == 1 and summaries[0].failure_count == 2
    recovered = [r for r in records if "scan recovered" in r.getMessage()]
    assert len(recovered) == 1 and recovered[0].failure_count == 2
    assert "private_" not in caplog.text
