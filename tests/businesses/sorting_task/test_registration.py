"""The shared task contract has one source and only compiled consumers."""

from app.businesses import catalog
from app.integrations.wms_callback import CallbackErrors
from app.integrations.wms_callback import registry as callbacks
from app.integrations.wms_request import operation as requests


def test_two_consumers_share_one_task_registration(task_flow):
    f = task_flow
    assert len(callbacks.operations()) == 4
    assert len(requests.operations()) == 2
    for kind, operations in (
        ("wms_callback", callbacks.operations()),
        ("wms_request", requests.operations()),
    ):
        for name, registered in operations.items():
            assert catalog.contract_consumers((kind, name, None)) == {"first", "second"}
            assert registered.registration_source.module.startswith(
                "app.businesses._components.sorting_task."
            )
            assert catalog.input_target("third", (kind, name, None)) is None
            if kind == "wms_callback":
                for owner in ("first", "second"):
                    target = catalog.input_target(owner, (kind, name, None))
                    assert target.source == registered.registration_source
                    assert target.component_key == "sorting_task"
    assert f.component.model_modules == ()
    assert f.component.commands == ()
    assert f.component.requires_progression
    assert f.component.required_methods == ("read_task_owner", "accept", "recheck")
    assert [entry.name for entry in f.component.initial_entries] == ["sorting.task.issued@v1"]


def test_task_callbacks_register_shared_extensions_once(task_flow):
    for name, registered in callbacks.operations().items():
        assert registered.errors == CallbackErrors(
            "INVALID_DATA", "INVALID_ENVELOPE", "IDEMPOTENCY_CONFLICT"
        )
        assert callable(registered.archive_guard)
        assert (registered.recheck is not None) == (name == "sorting.task.plan_delta@v1")


def test_duplicate_formal_callback_source_still_fails(task_flow):
    import pytest

    name = "sorting.task.issued@v1"
    registered = callbacks.lookup(name)
    with pytest.raises(ValueError, match="duplicate operation"):
        callbacks.callback_operation(name, data=registered.data_model)(registered.func)
