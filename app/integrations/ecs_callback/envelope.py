"""ECS 事件信封与 pydantic 错误到 `issues[]` 的翻译(只输出路径、码、期望类型,不回显值)。"""

from typing import Annotated

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
    is_debug: bool = False
    data: dict = {}


def _path(prefix: str, loc: tuple[int | str, ...]) -> str:
    path = prefix
    for part in loc:
        path += f"[{part}]" if isinstance(part, int) else f"{'.' if path else ''}{part}"
    return path or "$"


def issues_from(error: ValidationError, prefix: str = "") -> list[dict]:
    issues = []
    for err in error.errors(include_input=False, include_context=False, include_url=False):
        issue = {"field": _path(prefix, err["loc"])}
        if err["type"] == "missing":
            issue["code"] = "FIELD_REQUIRED"
        elif err["type"] in _TYPE_ERRORS:
            issue["code"] = "INVALID_TYPE"
            issue["expected"] = _TYPE_ERRORS[err["type"]]
        else:
            issue["code"] = "INVALID_VALUE"
        issues.append(issue)
    return issues
