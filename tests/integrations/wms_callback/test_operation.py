import pytest
from pydantic import ConfigDict

from app.integrations.wms_callback import (
    Accepted,
    CallbackPayload,
    Conflict,
    InvalidPayload,
    Rejected,
    callback_operation,
    registry,
)


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})


class Task(CallbackPayload):
    model_config = ConfigDict(json_schema_extra={"examples": [{"task_id": "t1"}]})

    task_id: str
    retries: int = 0


def make(result_for, payload=Task):
    @callback_operation("example.task@v1", data=payload)
    async def handler(ctx, payload):
        return result_for(payload)

    return registry.lookup("example.task@v1")


def test_parse_normalizes_defaults_and_drops_redundant_fields():
    op = make(lambda p: None)
    assert op.parse({"task_id": "t1", "extra": 1}) == {"task_id": "t1", "retries": 0}


@pytest.mark.parametrize("data", [{}, {"task_id": 1}, {"task_id": "t1", "retries": "2"}])
def test_parse_is_strict_about_json_types(data):
    with pytest.raises(InvalidPayload) as error:
        make(lambda p: None).parse(data)
    assert error.value.reason_code == "INVALID_EVIDENCE"


@pytest.mark.parametrize("payload,example", [(Task, {"task_id": "t1"}), (CallbackPayload, None)])
def test_example_comes_from_the_payload_schema(payload, example):
    assert make(lambda p: None, payload).example == example


@pytest.mark.parametrize(
    ("result", "expected"),
    [(None, Accepted), ({"id": 1}, Accepted), (Conflict({"x": 1}), Conflict)],
)
async def test_handle_turns_the_handler_result_into_an_outcome(result, expected):
    op = make(lambda p: result)

    class Ctx:
        parsed = {"task_id": "t1", "retries": 0}

    outcome = await op.handle(Ctx())
    assert isinstance(outcome, expected)
    if isinstance(result, dict):
        assert outcome.data == result


def test_outcomes_can_be_raised_or_returned():
    for outcome in (Accepted(), Conflict(), Rejected("X")):
        assert isinstance(outcome, Exception)
