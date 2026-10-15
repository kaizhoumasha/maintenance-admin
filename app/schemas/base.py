"""请求字段共用约束;Field(json_schema_extra=NO_AUDIT) 表示字段值不写入审计。"""

from typing import Annotated

from pydantic import Field

NO_AUDIT = {"audit": False}
DatabaseId = Annotated[int, Field(ge=1, le=2147483647)]
