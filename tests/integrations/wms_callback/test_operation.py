import pytest
from pydantic import ConfigDict

from app.integrations.wms_callback import (
    Accepted,
    CallbackOperation,
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


def make(execute_body, payload=Task):
    class Op(CallbackOperation):
        Payload = payload

        async def execute(self, ctx, payload):
            return execute_body(payload)

    return Op()


def test_parse_normalizes_defaults_and_drops_redundant_fields():
    op = make(lambda p: None)
    assert op.parse({"task_id": "t1", "extra": 1}) == {"task_id": "t1", "retries": 0}


@pytest.mark.parametrize("data", [{}, {"task_id": 1}, {"task_id": "t1", "retries": "2"}])
def test_parse_is_strict_about_json_types(data):
    with pytest.raises(InvalidPayload) as error:
        make(lambda p: None).parse(data)
    assert error.value.reason_code == "INVALID_EVIDENCE"


def test_example_comes_from_the_payload_schema():
    class Bare(CallbackPayload):
        value: int

    assert make(lambda p: None).example == {"task_id": "t1"}
    assert make(lambda p: None, Bare).example is None


@pytest.mark.parametrize(
    ("result", "expected"),
    [(None, Accepted), ({"id": 1}, Accepted), (Conflict({"x": 1}), Conflict)],
)
async def test_handle_turns_the_execute_result_into_an_outcome(result, expected):
    op = make(lambda p: result)

    class Ctx:
        parsed = {"task_id": "t1", "retries": 0}

    outcome = await op.handle(Ctx())
    assert isinstance(outcome, expected)
    if isinstance(result, dict):
        assert outcome.data == result


def test_operation_must_declare_payload_and_override_execute():
    with pytest.raises(TypeError, match="Payload"):
        callback_operation("example.broken@v1")(type("NoPayload", (CallbackOperation,), {}))

    class NoExecute(CallbackOperation):
        Payload = Task

    with pytest.raises(TypeError, match="execute"):
        callback_operation("example.broken@v1")(NoExecute)
    assert registry.lookup("example.broken@v1") is None


def test_outcomes_can_be_raised_or_returned():
    for outcome in (Accepted(), Conflict(), Rejected("X")):
        assert isinstance(outcome, Exception)
