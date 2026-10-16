"""请求字段共用约束;Field(json_schema_extra=NO_AUDIT) 表示字段值不写入审计。"""

from typing import Annotated, Self

from fastapi import Query
from pydantic import BaseModel, Field, model_validator

NO_AUDIT = {"audit": False}
DatabaseId = Annotated[int, Field(ge=1, le=2147483647)]


class PageParams(BaseModel):
    """统一分页入参:page 从 1 开始,page_size 默认 20、最大 100;越界由校验返回 INVALID_REQUEST。"""

    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)

    @model_validator(mode="after")
    def check_offset(self) -> Self:
        if self.offset > 2**63 - 1:
            raise ValueError("分页 OFFSET 超出数据库范围")
        return self

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


PageQuery = Annotated[PageParams, Query()]


class Page[T](BaseModel):
    """统一分页响应。"""

    items: list[T]
    total: int
    page: int
    page_size: int
