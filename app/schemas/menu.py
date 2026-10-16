from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import PERMISSION_CODE_PATTERN
from app.schemas.base import DatabaseId


class MenuIn(BaseModel):
    parent_id: DatabaseId | None = None
    name: str = Field(max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    path: str = Field(min_length=1, max_length=255)
    component: str = Field(min_length=1, max_length=255)
    redirect: str | None = Field(None, max_length=255)
    title: str = Field(min_length=1, max_length=64)
    icon: str | None = Field(None, max_length=64)
    sort: int = Field(0, ge=-32768, le=32767)
    hidden: bool = False
    keep_alive: bool = False
    permission_code: str | None = Field(None, max_length=64, pattern=PERMISSION_CODE_PATTERN)


class MenuRead(MenuIn):
    model_config = ConfigDict(from_attributes=True)

    id: int


class MenuNode(MenuRead):
    children: list["MenuNode"] = []


class RouteMeta(BaseModel):
    title: str
    icon: str | None = None
    hidden: bool | None = None
    keepAlive: bool | None = None  # noqa: N815 -- vue-router meta 的约定写法
    permission: str | None = None


class RouteRecord(BaseModel):
    """vue-router 4 的 RouteRecordRaw(component 为字符串,由前端解析);空值不输出。"""

    name: str
    path: str
    component: str
    redirect: str | None = None
    meta: RouteMeta
    children: list["RouteRecord"] | None = None
