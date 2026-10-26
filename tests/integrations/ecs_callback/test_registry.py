import pytest

from app.integrations.ecs_callback import CallbackPayload, registry
from app.integrations.ecs_callback.registry import EcsEventContext, ecs_event, events, lookup


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})


def test_registered_handler_is_found():
    @ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=CallbackPayload)
    async def handler(ctx, data): ...

    assert lookup("SCAN1", "SCAN_COMPLETED").func is handler
    assert lookup("SCAN1", "SCAN_COMPLETED").data_model is CallbackPayload
    assert list(events()) == [("SCAN1", "SCAN_COMPLETED")]
    assert lookup("SCAN1", "OTHER") is None


@pytest.mark.parametrize("name", ["scan_completed", "1A", "A-B", ""])
def test_invalid_event_types_rejected(name):
    with pytest.raises(ValueError, match="invalid event type"):
        ecs_event(name, device_role="SCAN1", data=CallbackPayload)(lambda ctx, data: None)


def test_duplicate_names_both_functions():
    @ecs_event("DUP", device_role="SCAN1", data=CallbackPayload)
    async def first(ctx, data): ...

    with pytest.raises(ValueError) as exc:

        @ecs_event("DUP", device_role="SCAN1", data=CallbackPayload)
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
        binding=None,
    )
    ctx.note("X", a=1)
    assert ctx.notes == [{"reason_code": "X", "a": 1}]
