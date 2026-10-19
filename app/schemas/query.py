from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.core.errors import AppError, ErrorCode
from app.schemas.base import PageParams

FilterOp = Literal["eq", "in", "ilike", "ge", "le", "between", "is_null", "not_null"]


class FilterCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    op: FilterOp
    value: Any = None

    @model_validator(mode="after")
    def check_value(self) -> Self:
        value = self.value
        if self.op in ("is_null", "not_null"):
            valid = value is None
        elif self.op == "in":
            valid = isinstance(value, list) and 1 <= len(value) <= 100 and None not in value
        elif self.op == "between":
            valid = isinstance(value, list) and len(value) == 2 and None not in value
        elif self.op == "ilike":
            valid = (
                isinstance(value, str)
                and len(value) <= 64
                and (len(value) - len(value.rstrip("\\"))) % 2 == 0
            )
        else:
            valid = value is not None and not isinstance(value, (list, dict))
        if not valid:
            raise ValueError("筛选值形状无效")
        return self


class FilterGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    couple: Literal["and", "or", "not"] = "and"
    conditions: list[FilterCondition | FilterGroup] = Field(min_length=1)

    def _depth(self) -> int:
        return 1 + max(
            (node._depth() for node in self.conditions if isinstance(node, FilterGroup)), default=0
        )

    @model_validator(mode="after")
    def check_group(self) -> Self:
        if self.couple == "not" and len(self.conditions) != 1:
            raise ValueError("not 只允许一个条件")
        if self._depth() > 3:
            raise ValueError("筛选嵌套超过三层")
        return self


def _filter_bytes(raw: str) -> str:
    try:
        valid = len(raw.encode("utf-8")) <= 2048
    except UnicodeError:
        valid = False
    if not valid:
        raise ValueError("filter 必须为有效 UTF-8 且不超过 2048 字节")
    return raw


FilterText = Annotated[
    Annotated[str, AfterValidator(_filter_bytes)] | None,
    Field(
        description="筛选条件组 JSON，UTF-8 最多 2048 字节；ilike 接受完整模式",
        examples=[
            '{"conditions":[{"field":"created_at","op":"ge","value":"2026-01-01T00:00:00Z"}]}'
        ],
    ),
]


class UserListParams(PageParams):
    model_config = ConfigDict(extra="forbid")
    filter: FilterText = None


class CursorParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(20, ge=1, le=100)
    cursor: str | None = Field(None, max_length=64)
    filter: FilterText = None


class CursorPage[T](BaseModel):
    items: list[T]
    next_cursor: str | None


def parse_filter(raw: str | None) -> FilterGroup | None:
    if raw is None:
        return None
    try:
        return FilterGroup.model_validate_json(_filter_bytes(raw))
    except (ValidationError, ValueError):
        # ValidationError 包含 input_value,不能直接回显或挂到业务异常链。
        raise AppError(
            ErrorCode.INVALID_REQUEST, "筛选格式无效", "filter: JSON、结构或长度无效"
        ) from None
