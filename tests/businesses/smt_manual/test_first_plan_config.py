import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5])
def test_first_plan_configuration_rejects_nonpositive_or_noninteger(value):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            DATABASE_URL="test",
            REDIS_URL="test",
            JWT_SECRET_KEY="test",
            SMT_MANUAL_FIRST_PLAN_WAIT_MS=value,
        )


@pytest.mark.parametrize("value", [None, 1, 1500, "1500"])
def test_first_plan_configuration_is_optional_and_accepts_environment_integer(value):
    settings = Settings(
        _env_file=None,
        DATABASE_URL="test",
        REDIS_URL="test",
        JWT_SECRET_KEY="test",
        SMT_MANUAL_FIRST_PLAN_WAIT_MS=value,
    )
    assert settings.SMT_MANUAL_FIRST_PLAN_WAIT_MS == (int(value) if value is not None else None)


async def test_first_plan_deadline_is_explicit_and_durable(manual_completion_tools):
    flow = manual_completion_tools
    await flow.ready()
    await flow.confirm(now_ms=99999)
    assert await flow.confirmations() == []
    from app.businesses.smt_manual.task_flow import SmtManualTaskFlow

    flow.task = SmtManualTaskFlow(
        flow.task.capabilities, flow.roller, repository=flow.task.repository, first_plan_wait_ms=100
    )
    await flow.confirm(now_ms=1099)
    assert await flow.confirmations() == []
    await flow.accept_prepare()
    await flow.confirm(now_ms=1100)
    assert len(await flow.confirmations()) == 1
    task = (await flow.rows("task"))[0]
    assert task["prepare_accepted_at_ms"] == 1000
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.confirm(now_ms=1101) == "COMPLETED"


@pytest.mark.parametrize("pending", ["early_plan", "missing_plan", "completion"])
async def test_zero_revision_never_ignores_pending_plan_or_completion(
    manual_bins, manual_completion_tools, pending
):
    flow = manual_bins
    from app.businesses.smt_manual.task_flow import SmtManualTaskFlow

    flow.task = SmtManualTaskFlow(
        flow.task.capabilities, flow.roller, repository=flow.task.repository, first_plan_wait_ms=1
    )
    await flow.issue()
    await flow.prepare()
    if pending == "early_plan":
        response = await flow.deliver(
            flow.message(
                "plan_delta",
                dict(
                    task_id="T1",
                    plan_revision=1,
                    added_bin_source_racks=[dict(rack_id="R", rack_face=["A"])],
                ),
            )
        )
        assert response.status_code == 503
    await flow.accept_prepare()
    if pending == "missing_plan":
        response = await flow.deliver(
            flow.message(
                "plan_delta",
                dict(
                    task_id="T1",
                    plan_revision=2,
                    added_bin_source_racks=[dict(rack_id="R", rack_face=["A"])],
                ),
            )
        )
        assert response.status_code == 409
    if pending == "completion":
        await flow.deliver(flow.completion())
    await flow.confirm(now_ms=99999)
    assert await flow.confirmations() == []


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "100"])
def test_constructor_requires_typed_positive_milliseconds(value):
    from app.businesses.smt_manual.task_flow import SmtManualTaskFlow

    with pytest.raises(ValueError):
        SmtManualTaskFlow(None, None, first_plan_wait_ms=value)
