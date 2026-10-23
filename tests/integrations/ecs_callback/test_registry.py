import pytest

from app.integrations.ecs_callback import registry
from app.integrations.ecs_callback.registry import EcsEventContext, ecs_event, events, lookup


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})


def test_registered_handler_is_found():
    @ecs_event("SCAN_COMPLETED")
    async def handler(ctx, data): ...

    assert lookup("SCAN_COMPLETED").func is handler
    assert lookup("SCAN_COMPLETED").data_model is None
    assert list(events()) == ["SCAN_COMPLETED"]
    assert lookup("OTHER") is None


@pytest.mark.parametrize("name", ["scan_completed", "1A", "A-B", ""])
def test_invalid_event_types_rejected(name):
    with pytest.raises(ValueError, match="invalid event type"):
        ecs_event(name)(lambda ctx, data: None)


def test_duplicate_names_both_functions():
    @ecs_event("DUP")
    async def first(ctx, data): ...

    with pytest.raises(ValueError) as exc:

        @ecs_event("DUP")
        async def second(ctx, data): ...

    assert f"{first.__module__}.{first.__qualname__}" in str(exc.value)
    assert "second" in str(exc.value)


def test_context_is_independent_kw_only_dataclass():
    with pytest.raises(TypeError):
        EcsEventContext(session=None)  # type: ignore[call-arg]
    ctx = EcsEventContext(
        session=None,  # type: ignore[arg-type]
        receipt_id=1,
        digest="d",
        device_code="D",
        event_type="X",
        timestamp=1,
        is_debug=False,
    )
    ctx.note("X", a=1)
    assert ctx.notes == [{"reason_code": "X", "a": 1}]
