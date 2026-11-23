from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import SCHEMA
from app.models.base import Base


class WorkLinePosition(Base):
    __tablename__ = "workline_positions"
    __table_args__ = (
        UniqueConstraint("work_line_id", "position_code"),
        UniqueConstraint("position_code"),
        CheckConstraint(
            "capacity IS NULL OR (capacity BETWEEN 1 AND 2147483647 "
            "AND capacity = CAST(capacity AS INTEGER))",
            name="capacity",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    work_line_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.work_lines.id", ondelete="RESTRICT")
    )
    position_code: Mapped[str] = mapped_column(String(100))
    capacity: Mapped[int | None] = mapped_column(Integer().evaluates_none(), default=1)
