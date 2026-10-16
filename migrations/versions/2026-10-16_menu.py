"""menu

新增 menu 表(前端路由菜单)。菜单数据和新权限码由 scripts.seed_admin 写入。

Revision ID: c9f2b7a41d08
Revises: e7a41c9b3d20
Create Date: 2026-10-16 15:00:00.000000+00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "c9f2b7a41d08"
down_revision = "e7a41c9b3d20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "menu",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("path", sa.String(length=255), nullable=False),
        sa.Column("component", sa.String(length=255), nullable=False),
        sa.Column("redirect", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=64), nullable=False),
        sa.Column("icon", sa.String(length=64), nullable=True),
        sa.Column("sort", sa.Integer(), server_default="0", nullable=False),
        sa.Column("hidden", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("keep_alive", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("permission_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["menu.id"], name=op.f("menu_parent_id_fkey"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("menu_pkey")),
        sa.UniqueConstraint("name", name=op.f("menu_name_key")),
    )
    op.create_index(op.f("menu_parent_id_idx"), "menu", ["parent_id"])


def downgrade() -> None:
    op.drop_index(op.f("menu_parent_id_idx"), table_name="menu")
    op.drop_table("menu")
