from sqlalchemy import String, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.rbac import Role, user_role


class User(TimestampMixin, Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True)
    # False=单点登录:新登录踢掉旧会话的 refresh;管理员由 seed_admin 置 True
    allow_multi_login: Mapped[bool] = mapped_column(default=False, server_default=false())
    roles: Mapped[list[Role]] = relationship(secondary=user_role, lazy="raise")
