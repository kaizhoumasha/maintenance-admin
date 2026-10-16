from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Select, and_, not_, or_
from sqlalchemy.sql.elements import ColumnElement

from app.core.errors import AppError, ErrorCode
from app.schemas.query import FilterCondition, FilterGroup


@dataclass(frozen=True)
class FieldSpec:
    column: Any
    value_type: Any
    ops: tuple[str, ...]
    clause: Callable[[str, Any], ColumnElement[bool]] | None = None


def _invalid(message: str, location: str) -> AppError:
    return AppError(ErrorCode.INVALID_REQUEST, message, f"{location}: {message}")


def _condition(
    node: FilterCondition, specs: dict[str, FieldSpec], location: str
) -> ColumnElement[bool]:
    spec = specs.get(node.field)
    if spec is None:
        raise _invalid("筛选字段不支持", f"{location}.field")
    op, value = node.op, node.value
    if op not in spec.ops:
        raise _invalid("筛选算子不支持", f"{location}.op")
    if op == "is_null":
        return spec.column.is_(None)
    if op == "not_null":
        return spec.column.is_not(None)

    adapter = TypeAdapter(spec.value_type)

    def convert(raw: Any, path: str) -> Any:
        try:
            if isinstance(raw, str) and "\x00" in raw:
                raise ValueError
            if spec.value_type is datetime:
                if not isinstance(raw, str):
                    raise ValueError
                parsed = datetime.fromisoformat(raw)
                if parsed.tzinfo is None:
                    raise ValueError
                return parsed.astimezone(UTC)
            return adapter.validate_python(raw, strict=True)
        except (ValidationError, ValueError, TypeError, OverflowError):
            raise _invalid("筛选值类型或范围无效", path) from None

    path = f"{location}.value"
    if op in ("in", "between"):
        value = [convert(v, f"{path}.{i}") for i, v in enumerate(value)]
    else:
        value = convert(value, path)
    if spec.clause:
        return spec.clause(op, value)
    column = spec.column
    match op:
        case "eq":
            return column == value
        case "in":
            return column.in_(value)
        case "ilike":
            return column.ilike(value, escape="\\")
        case "ge":
            return column >= value
        case "le":
            return column <= value
        case "between":
            return column.between(*value)


def apply_filter(
    stmt: Select[Any], group: FilterGroup | None, specs: dict[str, FieldSpec]
) -> Select[Any]:
    if group is None:
        return stmt

    def compile_group(node: FilterGroup, location: str) -> ColumnElement[bool]:
        clauses = [
            compile_group(child, f"{location}.conditions.{i}")
            if isinstance(child, FilterGroup)
            else _condition(child, specs, f"{location}.conditions.{i}")
            for i, child in enumerate(node.conditions)
        ]
        match node.couple:
            case "and":
                return and_(*clauses)
            case "or":
                return or_(*clauses)
            case "not":
                return not_(clauses[0])

    return stmt.where(compile_group(group, "filter"))
