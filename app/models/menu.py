from sqlalchemy import ForeignKey, String, false
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Menu(TimestampMixin, Base):
    """前端路由菜单;字段与 vue-router 的 RouteRecordRaw 一一对应(见 menu_service.to_routes)。"""

    __tablename__ = "menu"

    id: Mapped[int] = mapped_column(primary_key=True)
    # RESTRICT:有子菜单不能直接删父级,服务层先给出 409
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("menu.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(64), unique=True)  # 路由名,全局唯一
    path: Mapped[str] = mapped_column(String(255))  # 顶级以 / 开头,子级相对父级
    component: Mapped[str] = mapped_column(String(255))  # "Layout" 表示目录,其余是页面组件路径
    redirect: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(64))
    icon: Mapped[str | None] = mapped_column(String(64))
    sort: Mapped[int] = mapped_column(default=0, server_default="0")
    hidden: Mapped[bool] = mapped_column(default=False, server_default=false())
    keep_alive: Mapped[bool] = mapped_column(default=False, server_default=false())
    # 为空表示所有登录用户可见;不设外键,创建时校验存在
    permission_code: Mapped[str | None] = mapped_column(String(64))
