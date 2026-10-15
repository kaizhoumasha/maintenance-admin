"""multi_login

Revision ID: a3f8c2d71e55
Revises: 7c1d3f5a9b20
Create Date: 2026-10-15 05:50:00.000000+00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "a3f8c2d71e55"
down_revision = "7c1d3f5a9b20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("allow_multi_login", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    # 已有的 admin 角色用户允许多点登录
    op.execute(
        'UPDATE "user" SET allow_multi_login = true WHERE id IN ('
        "SELECT ur.user_id FROM user_role ur JOIN role r ON r.id = ur.role_id "
        "WHERE r.name = 'admin')"
    )


def downgrade() -> None:
    op.drop_column("user", "allow_multi_login")
