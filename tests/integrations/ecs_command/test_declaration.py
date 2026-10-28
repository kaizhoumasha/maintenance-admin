from dataclasses import make_dataclass
from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from typing_extensions import TypedDict

from app.integrations.ecs_command.contracts import ErrorDetail
from app.integrations.registration import local_registration


class StrictPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    quantity: int = 1


class MoveError(ErrorDetail):
    code: Literal["BLOCKED"]


def declare(model, **options):
    from app.integrations.ecs_command import ecs_command

    settings = {
        "device_roles": ("ARM",),
        "annex_id": "test.move",
        "annex_version": "1",
        "result": StrictPayload,
        "error": MoveError,
        "timeout_min_ms": 1,
        "timeout_max_ms": 60_000,
    }
    settings.update(options)
    return ecs_command(settings.pop("task_type", "MOVE"), **settings)(model)


def params(name="Move", **fields):
    return create_model(name, __base__=StrictPayload, __module__=__name__, **fields)


def test_declaration_preserves_original_models_and_source():
    from app.integrations.ecs_command import compile_commands

    model = params()
    assert declare(model) is model
    bindings, annexes = compile_commands(model)
    (binding,) = bindings
    annex = annexes["test.move", "1"]
    assert binding.source is model
    assert binding.registration_source.module == __name__
    assert binding.registration_source.source_file.endswith("test_declaration.py")
    assert binding.registration_source.symbol == "Move"
    assert binding.registration_source.local_only is False
    assert (binding.device_role, binding.task_type) == ("ARM", "MOVE")
    assert annex.params_model is model
    assert annex.result_model is StrictPayload
    assert annex.error_model is MoveError
    assert (annex.timeout_min_ms, annex.timeout_max_ms) == (1, 60_000)
    with pytest.raises(ValidationError):
        annex.params_model(quantity="1")
    with pytest.raises(ValidationError):
        annex.params_model(unexpected=True)
    assert compile_commands() == ((), {})
    assert compile_commands(model) == (bindings, annexes)


@pytest.mark.parametrize(
    "options",
    [
        {"device_roles": ("arm",)},
        {"device_roles": ()},
        {"device_roles": ("ARM", "ARM")},
        {"device_roles": "ARM"},
        {"device_roles": ["ARM"]},
        {"task_type": "move.to"},
        {"annex_id": ""},
        {"annex_version": " "},
        {"annex_id": "move\x00"},
        {"timeout_min_ms": True},
        {"timeout_max_ms": 2.5},
        {"timeout_min_ms": -1},
        {"timeout_min_ms": 100, "timeout_max_ms": 99},
        {"timeout_max_ms": 2**63},
        {"result": dict},
        {"error": StrictPayload},
    ],
)
def test_invalid_declaration_leaves_the_model_available_for_a_valid_declaration(options):
    from app.integrations.ecs_command import compile_commands

    model = params()
    with pytest.raises(ValueError) as caught:
        declare(model, **options)
    assert "Move" in str(caught.value)
    assert "test_declaration.py" in str(caught.value)
    assert "修复" in str(caught.value)
    assert compile_commands(declare(model))[0][0].source is model


@pytest.mark.parametrize("slot", ["params", "result", "error"])
@pytest.mark.parametrize("config", [{"strict": False, "extra": "forbid"}])
def test_each_contract_model_requires_strict_types(slot, config):
    model = create_model("Loose", __base__=ErrorDetail, __config__=ConfigDict(**config))
    with pytest.raises(ValueError, match="strict|extra"):
        declare(
            model if slot == "params" else params(), **({slot: model} if slot != "params" else {})
        )


@pytest.mark.parametrize("slot", ["params", "result", "error"])
@pytest.mark.parametrize("nested", [BaseModel])
def test_nested_contract_models_must_also_be_strict(slot, nested):
    base = ErrorDetail if slot == "error" else StrictPayload
    model = create_model("Nested", __base__=base, children=(list[nested], ...))
    with pytest.raises(ValueError, match="strict|extra"):
        declare(
            model if slot == "params" else params(), **({slot: model} if slot != "params" else {})
        )


@pytest.mark.parametrize("kind", ["typed_dict", "dataclass"])
@pytest.mark.parametrize("config", [{"strict": False, "extra": "forbid"}])
def test_lax_nested_structures_reject_declaration_without_poisoning_the_parameter_model(
    kind, config
):
    from app.integrations.ecs_command import compile_commands

    nested = (
        TypedDict("Nested", {"quantity": int})
        if kind == "typed_dict"
        else make_dataclass("Nested", [("quantity", int)])
    )
    nested.__pydantic_config__ = ConfigDict(**config)
    result = create_model("NestedResult", __base__=StrictPayload, nested=(nested, ...))
    model = params()
    with pytest.raises(ValueError, match="strict|extra"):
        declare(model, result=result)
    assert compile_commands(declare(model))[0][0].source is model


@pytest.mark.parametrize("kind", ["typed_dict", "dataclass"])
def test_nested_structures_inheriting_strict_configuration_remain_eligible(kind):
    from app.integrations.ecs_command import compile_commands

    nested = (
        TypedDict("Nested", {"quantity": int})
        if kind == "typed_dict"
        else make_dataclass("Nested", [("quantity", int)])
    )
    model = params(nested=(nested, ...))
    _, annexes = compile_commands(declare(model))
    annex = annexes["test.move", "1"]
    assert annex.params_model is model
    assert annex.params_model(nested=nested(quantity=7)).model_dump()["nested"] == {"quantity": 7}
    with pytest.raises(ValidationError):
        annex.params_model.model_validate({"nested": {"quantity": "7", "extra": 1}})


@pytest.mark.parametrize("slot", ["params", "result", "error"])
@pytest.mark.parametrize(
    "annotation", [Annotated[int, Field(strict=False)], list[Annotated[int, Field(strict=False)]]]
)
def test_explicitly_lax_fields_are_rejected_even_inside_containers(slot, annotation):
    base = ErrorDetail if slot == "error" else StrictPayload
    model = create_model("LaxField", __base__=base, value=(annotation, ...))
    with pytest.raises(ValueError, match="strict"):
        declare(
            model if slot == "params" else params(), **({slot: model} if slot != "params" else {})
        )


def test_strict_nested_recursive_and_aliased_models_keep_their_contract():
    from app.integrations.ecs_command import compile_commands

    class Node(StrictPayload):
        children: list["Node"] = []

    model = params(node=(Node, ...), target=(str, Field(alias="target_code")))
    bindings, annexes = compile_commands(declare(model))
    annex = annexes["test.move", "1"]
    assert bindings[0].source is model
    assert annex.params_model(node={"quantity": 2}, target_code="A").model_dump(
        mode="json", by_alias=True
    ) == {"quantity": 1, "node": {"quantity": 2, "children": []}, "target_code": "A"}


def test_repeated_decoration_does_not_replace_the_original_contract():
    from app.integrations.ecs_command import compile_commands

    model = declare(params())
    with pytest.raises(ValueError, match="already|duplicate"):
        declare(model, task_type="OTHER")
    assert compile_commands(model)[0][0].task_type == "MOVE"


def test_undeclared_subclass_does_not_inherit_a_declaration():
    from app.integrations.ecs_command import compile_commands

    model = declare(params())
    child = type("Child", (model,), {})
    with pytest.raises(ValueError, match="declar"):
        compile_commands(child)
    assert (
        compile_commands(declare(child, task_type="CHILD", annex_id="child"))[0][0].source is child
    )


@pytest.mark.parametrize("conflict", ["binding", "annex", "same_model"])
def test_compile_conflicts_name_both_original_sources_and_do_not_accumulate(conflict):
    from app.integrations.ecs_command import compile_commands

    first = declare(params("First"))
    second = (
        first
        if conflict == "same_model"
        else declare(params("Second"), task_type="MOVE" if conflict == "binding" else "OTHER")
    )
    with pytest.raises(ValueError) as caught:
        compile_commands(first, second)
    assert "First" in str(caught.value)
    if conflict != "same_model":
        assert "Second" in str(caught.value)
    assert "修复" in str(caught.value)
    assert len(compile_commands(first)[0]) == 1
    assert compile_commands() == ((), {})


def test_local_declaration_is_never_compiled_into_formal_commands():
    from app.integrations.ecs_command import compile_commands

    with local_registration():
        model = declare(params())
    with pytest.raises(ValueError, match="local"):
        compile_commands(model)
    assert compile_commands(declare(params("Formal")))[0][0].registration_source.local_only is False


def test_declaration_respects_formal_module_first_import_guard(monkeypatch):
    model = params()
    monkeypatch.setattr(model, "__module__", "app.businesses.probe.ecs_command.move")
    with local_registration(), pytest.raises(RuntimeError, match="local_registration"):
        declare(model)
    assert declare(model) is model


def test_dev_module_cannot_be_laundered_by_an_ordinary_import(monkeypatch):
    from app.integrations.ecs_command import compile_commands

    model = params()
    monkeypatch.setattr(model, "__module__", "app.businesses.probe.dev.commands")
    with pytest.raises(ValueError, match="local|dev"):
        compile_commands(declare(model))


def test_compile_rechecks_the_original_source_and_model_contract(monkeypatch):
    from app.integrations.ecs_command import compile_commands

    model = declare(params())
    monkeypatch.setattr(model, "__module__", "app.businesses.other.ecs_command.move")
    with pytest.raises(ValueError, match="source|来源"):
        compile_commands(model)


def test_compile_rejects_models_whose_validation_was_relaxed_after_declaration():
    from app.integrations.ecs_command import compile_commands

    model = declare(params())
    model.model_config = ConfigDict(strict=False, extra="forbid")
    model.model_rebuild(force=True)
    with pytest.raises(ValueError, match="strict"):
        compile_commands(model)


def test_unresolved_model_annotations_fail_without_completing_the_model():
    model = params(child=("MissingModel", ...))
    with pytest.raises(ValueError, match="model|annotation"):
        declare(model)
    assert model.__pydantic_complete__ is False


def test_one_model_compiles_each_declared_role_with_one_original_annex():
    from app.integrations.ecs_command import compile_commands, ecs_command

    model = ecs_command(
        "MOVE",
        device_roles=("ARM", "EXIT"),
        annex_id="shared",
        annex_version="1",
        result=StrictPayload,
        error=MoveError,
        timeout_min_ms=1,
        timeout_max_ms=100,
    )(params())
    bindings, annexes = compile_commands(model)
    assert [binding.device_role for binding in bindings] == ["ARM", "EXIT"]
    assert all(binding.source is model for binding in bindings)
    assert list(annexes) == [("shared", "1")]
    assert annexes["shared", "1"].params_model is model


@pytest.mark.parametrize("extra", ["ignore", "allow"])
def test_declaration_preserves_strict_model_extra_policy(extra):
    from app.integrations.ecs_command import compile_commands, ecs_command

    model = create_model(
        "Permissive", __config__=ConfigDict(strict=True, extra=extra), qty=(int, ...)
    )
    ecs_command(
        "MOVE",
        device_roles=("ARM",),
        annex_id="permissive",
        annex_version="1",
        result=model,
        error=MoveError,
        timeout_min_ms=1,
        timeout_max_ms=100,
    )(model)
    _, annexes = compile_commands(model)
    assert annexes["permissive", "1"].params_model is model
    assert model.model_validate({"qty": 1, "custom": 2}).model_dump() == (
        {"qty": 1, "custom": 2} if extra == "allow" else {"qty": 1}
    )
    with pytest.raises(ValidationError):
        model(qty="1")


def test_strict_root_object_keeps_its_original_contract():
    from pydantic import RootModel

    from app.integrations.ecs_command import compile_commands, ecs_command

    class Object(RootModel[dict[str, int]]):
        model_config = ConfigDict(strict=True)

    ecs_command(
        "MOVE",
        device_roles=("ARM",),
        annex_id="root",
        annex_version="1",
        result=Object,
        error=MoveError,
        timeout_min_ms=1,
        timeout_max_ms=100,
    )(Object)
    _, annexes = compile_commands(Object)
    assert annexes["root", "1"].params_model is Object
    assert Object({"qty": 1}).model_dump() == {"qty": 1}
    with pytest.raises(ValidationError):
        Object({"qty": "1"})
