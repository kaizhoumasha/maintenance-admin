"""请求字段共用约束;Field(json_schema_extra=NO_AUDIT) 表示字段值不写入审计。"""

from typing import Annotated, Self

from pydantic import BaseModel, Field, model_validator

NO_AUDIT = {"audit": False}
DatabaseId = Annotated[int, Field(ge=1, le=2147483647)]
MAX_OFFSET = 100_000


class PageParams(BaseModel):
    """统一分页入参:行号从 1 开始,闭区间长度最多 100。"""

    start: int | None = Field(None, ge=1)
    end: int | None = Field(None, ge=1)

    @model_validator(mode="after")
    def check_offset(self) -> Self:
        if self.start is None and self.end is None:
            self.start, self.end = 1, 20
        elif self.start is None or self.end is None:
            raise ValueError("分页必须同时提供 start 和 end")
        if self.end < self.start:
            raise ValueError("分页结束行不能小于起始行")
        if self.end - self.start + 1 > 100:
            raise ValueError("分页窗口最多 100 行")
        if self.offset > 2**63 - 1:
            raise ValueError("分页 OFFSET 超出数据库范围")
        if self.offset > MAX_OFFSET:
            raise ValueError("分页 OFFSET 超出允许范围")
        return self

    @property
    def offset(self) -> int:
        return self.start - 1

    @property
    def window_size(self) -> int:
        return self.end - self.start + 1


class Page[T](BaseModel):
    """统一分页响应。"""

    items: list[T]
    total: int
    start: int
    end: int
