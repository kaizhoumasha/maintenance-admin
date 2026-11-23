import pytest

from app.core import dev_scenarios
from app.core.dev_scenarios import DevOutcome, DevRequest, dev_scenario, scenarios


class In(DevRequest):
    value: int


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(dev_scenarios, "_registry", {})


async def handler(session, req: In) -> DevOutcome:
    return DevOutcome("key", "rid")


def test_a_registered_scenario_gets_a_discriminated_model():
    dev_scenario("a.b", "标题", example={"value": 1})(handler)
    [scenario] = scenarios()
    assert scenario.model.model_validate({"scenario": "a.b", "value": 1}).scenario == "a.b"


@pytest.mark.parametrize("name", ["single", "A.b", "a..b", "a.b-c"])
def test_invalid_names_are_rejected_at_registration(name):
    with pytest.raises(ValueError, match="invalid scenario name"):
        dev_scenario(name, "t", example={"value": 1})(handler)


def test_duplicate_names_are_rejected_and_name_both_handlers():
    dev_scenario("a.b", "t", example={"value": 1})(handler)

    async def other(session, req: In) -> DevOutcome:
        return DevOutcome("key", "rid")

    with pytest.raises(ValueError, match="duplicate.*handler.*other"):
        dev_scenario("a.b", "t", example={"value": 1})(other)


def test_an_invalid_example_fails_at_registration_not_in_swagger():
    with pytest.raises(ValueError, match="example is invalid"):
        dev_scenario("a.b", "t", example={"value": "not a number"})(handler)
