"""Revalidate model fields before either integration freezes their JSON representation."""

from copy import copy
from dataclasses import fields, is_dataclass

from pydantic import BaseModel
from pydantic_core import SchemaValidator


def _schema_nodes(schema, definitions=None):
    pending, visited = [schema], set()
    while pending:
        node = pending.pop()
        if not isinstance(node, (dict, list, tuple)) or id(node) in visited:
            continue
        visited.add(id(node))
        if isinstance(node, dict):
            yield node
            if node.get("type") == "definition-ref" and definitions:
                pending.append(definitions.get(node["schema_ref"]))
            pending.extend(
                item
                for key, item in node.items()
                if key not in {"metadata", "config", "serialization", "default"}
            )
        else:
            pending.extend(node)


def _dataclass_contract(value, context, scope):
    definitions = {node["ref"]: node for node in _schema_nodes(context) if "ref" in node}
    candidates = [
        node
        for node in _schema_nodes(scope, definitions)
        if node.get("type") == "dataclass" and node["cls"] is type(value)
    ]
    if not candidates:
        candidates = [
            node
            for node in _schema_nodes(context, definitions)
            if node.get("type") == "dataclass" and node["cls"] is type(value)
        ]
    if len(candidates) != 1:
        reason = "ambiguous" if candidates else "unresolved"
        raise ValueError(
            f"{reason} original dataclass contract; use the unchanged legacy operation/data form"
        )
    schema = candidates[0]
    # A prebuilt wrapper cannot validate assignments; reuse the exact class's original validator.
    validator = type(value).__dict__.get("__pydantic_validator__")
    if validator is None or schema.get("generic_origin"):
        validator = SchemaValidator(
            {"type": "definitions", "schema": schema, "definitions": list(definitions.values())}
        )
    return schema, validator


def _validation_input(value, active, context=None, scope=None):
    structured = isinstance(value, (BaseModel, dict, list, tuple, set, frozenset)) or (
        is_dataclass(value) and not isinstance(value, type)
    )
    if not structured:
        return value
    if id(value) in active:
        raise ValueError("payload contains a cyclic model or container")
    active.add(id(value))
    try:
        return _structured_input(value, active, context, scope)
    finally:
        active.remove(id(value))


def _structured_input(value, active, context, scope):
    if isinstance(value, BaseModel):
        context = type(value).__pydantic_core_schema__
        model_schema = next(
            node
            for node in _schema_nodes(context)
            if node.get("type") == "model" and node["cls"] is type(value)
        )
        if model_schema.get("root_model"):
            try:
                root = value.root
            except AttributeError:
                raise ValueError("root field is missing") from None
            data = _validation_input(root, active, context, model_schema["schema"])
            return type(value).model_validate(data, by_alias=False, by_name=True)
        fields_schema = next(
            node
            for node in _schema_nodes(model_schema["schema"])
            if node.get("type") == "model-fields"
        )["fields"]
        data = {
            name: _validation_input(
                item, active, context, fields_schema.get(name, {}).get("schema")
            )
            for name, item in {**vars(value), **(value.model_extra or {})}.items()
        }
        return type(value).model_validate(data, by_alias=False, by_name=True)
    if is_dataclass(value) and not isinstance(value, type):
        if any(field.frozen for field in getattr(type(value), "__pydantic_fields__", {}).values()):
            raise ValueError(
                "dataclasses containing Field(frozen=True) are not eligible for model calls; "
                "use the unchanged legacy operation/data form"
            )
        # Strict dataclasses require instances; validate a copy without trusting its fields.
        validated = copy(value)
        schema, validator = _dataclass_contract(value, context, scope)
        fields_schema = {
            field["name"]: field["schema"]
            for field in next(
                node
                for node in _schema_nodes(schema["schema"])
                if node.get("type") == "dataclass-args"
            )["fields"]
        }
        names = dict.fromkeys(
            [field.name for field in fields(value)] + list(getattr(value, "__dict__", {}))
        )
        for name in names:
            try:
                item = getattr(value, name)
            except AttributeError:
                raise ValueError(f"dataclass field {name!r} is missing") from None
            validated = validator.validate_assignment(
                validated,
                name,
                _validation_input(item, active, context, fields_schema.get(name)),
                by_name=True,
            )
        return validated
    if isinstance(value, dict):
        return {
            _validation_input(key, active, context, scope): _validation_input(
                item, active, context, scope
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return type(value)(_validation_input(item, active, context, scope) for item in value)
    return value


def validated_model_payload(payload: BaseModel) -> dict:
    """Use original validators and JSON serializers, including field-name alias input."""
    model = _validation_input(payload, set())
    return model.model_dump(mode="json")
