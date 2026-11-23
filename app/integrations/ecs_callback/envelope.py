"""ECS 事件信封与 pydantic 错误到 `issues[]` 的翻译(只输出路径、码、期望类型,不回显值)。"""

from typing import Annotated, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

_TYPE_ERRORS = {
    "string_type": "string",
    "int_type": "integer",
    "int_parsing": "integer",
    "float_type": "number",
    "float_parsing": "number",
    "bool_type": "boolean",
    "bool_parsing": "boolean",
    "dict_type": "object",
    "model_type": "object",
    "list_type": "array",
}


class EcsEventEnvelope(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    device_code: Annotated[str, Field(min_length=1)]
    event_type: Annotated[str, Field(min_length=1)]
    timestamp: Annotated[int, Field(ge=0, le=2**63 - 1)]
    data: dict = {}


def _allowed_segments(model: type[BaseModel], seen: set[type] | None = None) -> set[str]:
    seen = set() if seen is None else seen
    if model in seen:
        return set()
    seen.add(model)
    names: set[str] = set()

    def walk(tp: object) -> None:
        if isinstance(tp, type) and issubclass(tp, BaseModel):
            names.update(_allowed_segments(tp, seen))
        for arg in get_args(tp):
            walk(arg)

    for name, info in model.model_fields.items():
        names.add(name)
        names.update(a for a in (info.alias, info.validation_alias) if isinstance(a, str))
        walk(info.annotation)
    return names


def _path(prefix: str, loc: tuple[int | str, ...], allowed: set[str]) -> str:
    path = prefix
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            # 客户端可控的 dict 键、extra 键、union 标签一律屏蔽成 *
            path += f"{'.' if path else ''}{part if part in allowed else '*'}"
    return path or "$"


def issues_from(error: ValidationError, model: type[BaseModel], prefix: str = "") -> list[dict]:
    allowed = _allowed_segments(model)
    issues: list[dict] = []
    seen: set[tuple[str, str, str | None]] = set()
    for err in error.errors(include_input=False, include_context=False, include_url=False):
        issue = {"field": _path(prefix, err["loc"], allowed)}
        if err["type"] == "missing":
            issue["code"] = "FIELD_REQUIRED"
        elif err["type"] in _TYPE_ERRORS:
            issue["code"] = "INVALID_TYPE"
            issue["expected"] = _TYPE_ERRORS[err["type"]]
        else:
            issue["code"] = "INVALID_VALUE"
        key = (issue["field"], issue["code"], issue.get("expected"))
        if key not in seen:
            seen.add(key)
            issues.append(issue)
    return issues
