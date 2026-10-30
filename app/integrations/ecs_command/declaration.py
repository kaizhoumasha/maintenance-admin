"""Explicit command metadata; compilation neither discovers nor executes business code."""

from dataclasses import dataclass, replace

from pydantic import BaseModel

from app.integrations.registration import DEV_MODULES, validate_registration_source

from .contracts import CommandBinding, DeviceAnnex, ErrorDetail, merge_annexes

_DECLARATION = "__ecs_command_declaration__"


@dataclass(frozen=True)
class CommandDeclaration:
    model: type[BaseModel]
    bindings: tuple[CommandBinding, ...]
    annex: DeviceAnnex


def _strict_model(model):
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise ValueError("params/result/error must be BaseModel classes")
    if not model.__pydantic_complete__:
        raise ValueError("resolve model annotations before declaring the command")
    pending = [model.__pydantic_core_schema__]
    visited = set()
    while pending:
        node = pending.pop()
        if not isinstance(node, (dict, list, tuple)) or id(node) in visited:
            continue
        visited.add(id(node))
        if not isinstance(node, dict):
            pending.extend(node)
            continue
        if node.get("type") in {"model", "typed-dict", "dataclass"}:
            config = node.get("config", {})
            if config.get("strict") is not True:
                raise ValueError("every nested model requires strict=True")
        if node.get("strict") is False:
            raise ValueError("explicit strict=False fields are not eligible")
        pending.extend(
            value
            for key, value in node.items()
            if key not in {"metadata", "config", "serialization", "default"}
        )


def _validate_annex(annex):
    for model in (annex.params_model, annex.result_model, annex.error_model):
        _strict_model(model)
    if not issubclass(annex.error_model, ErrorDetail):
        raise ValueError("error must inherit ErrorDetail")


def ecs_command(
    task_type,
    *,
    device_roles: tuple[str, ...],
    annex_id,
    annex_version,
    result,
    error,
    timeout_min_ms,
    timeout_max_ms,
):
    """Declare one exact parameter class, preserving its original contract and identity."""

    def decorate(model):
        if not isinstance(model, type) or not issubclass(model, BaseModel):
            raise ValueError("ecs_command requires a BaseModel class; 修复: 装饰原参数模型")
        source = validate_registration_source(model)
        try:
            if _DECLARATION in model.__dict__:
                raise ValueError("command model already has a declaration")
            if not isinstance(device_roles, tuple) or not device_roles:
                raise ValueError("device_roles must be a nonempty tuple")
            if len(set(device_roles)) != len(device_roles):
                raise ValueError("device_roles must be unique")
            bindings = tuple(
                CommandBinding(role, task_type, annex_id, annex_version, model)
                for role in device_roles
            )
            annex = DeviceAnnex(model, result, error, timeout_min_ms, timeout_max_ms)
            _validate_annex(annex)
            merge_annexes({}, {(annex_id, annex_version): annex}, source=source.location)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{source.location}: {exc}; 修复: 校对原模型的严格字段、角色、附录和超时声明"
            ) from None
        setattr(model, _DECLARATION, CommandDeclaration(model, bindings, annex))
        return model

    return decorate


def command_declaration(model) -> CommandDeclaration:
    """Read only an exact class's own declaration and recheck its original source."""
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise ValueError("command requires a declared BaseModel class; 修复: 使用原声明模型")
    source = validate_registration_source(model)
    declaration = model.__dict__.get(_DECLARATION)
    try:
        if not isinstance(declaration, CommandDeclaration) or declaration.model is not model:
            raise ValueError("exact command model has no declaration")
        if (
            not declaration.bindings
            or declaration.annex.params_model is not model
            or any(
                binding.source is not model
                or source != replace(binding.registration_source, local_only=source.local_only)
                for binding in declaration.bindings
            )
        ):
            raise ValueError("command declaration source/model changed")
        _validate_annex(declaration.annex)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{source.location}: {exc}; 修复: 使用精确原声明类并保留原合同和来源"
        ) from None
    return declaration


def compile_commands(
    *command_models: type[BaseModel],
) -> tuple[tuple[CommandBinding, ...], dict[tuple[str, str], DeviceAnnex]]:
    """Convert the explicit formal list; failures leave no shared compiler state."""
    bindings, annexes, binding_sources, annex_sources = [], {}, {}, {}
    for model in command_models:
        declaration = command_declaration(model)
        binding, annex = declaration.bindings[0], declaration.annex
        source = binding.registration_source
        parts = source.module.split(".")
        entry_index = 4 if parts[:3] == ["app", "businesses", "_components"] else 3
        if source.local_only or (
            parts[:2] in (["app", "integrations"], ["app", "businesses"])
            and len(parts) > entry_index
            and parts[entry_index] in DEV_MODULES
        ):
            raise ValueError(
                f"{source.location}: local-only command cannot enter formal compilation; "
                "修复: 正式清单只包含正式来源的命令"
            )
        annex_key = (binding.annex_id, binding.annex_version)
        try:
            annexes = merge_annexes(annexes, {annex_key: annex}, source=source.location)
        except ValueError as exc:
            raise ValueError(
                f"{exc}; original source: {annex_sources.get(annex_key)}; "
                "修复: 同一附录身份保留精确原模型与超时合同"
            ) from None
        for binding in declaration.bindings:
            key = (binding.device_role, binding.task_type)
            if key in binding_sources:
                raise ValueError(
                    f"duplicate command binding {key!r}: {binding_sources[key]} "
                    f"and {source.location}; "
                    "修复: 每个角色/任务只声明一次"
                )
            bindings.append(binding)
            binding_sources[key] = source.location
        annex_sources[annex_key] = source.location
    return tuple(bindings), annexes
