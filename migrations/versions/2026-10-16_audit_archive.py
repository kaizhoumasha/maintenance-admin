"""audit_archive

新增 audit_logs_archive 归档表,给 audit_logs.created_at 加索引(归档任务按它扫描),
并补三个新权限码:
- admin:user:list / admin:audit:read_archive:授给已拥有 admin:user:read / admin:audit:read 的角色
- admin:maintenance:archive_audit:只授给名为 admin 的角色,其它角色需手动授权

Revision ID: e7a41c9b3d20
Revises: d52e8a6b1f93
Create Date: 2026-10-16 07:00:00.000000+00:00
"""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "e7a41c9b3d20"
down_revision = "d52e8a6b1f93"
branch_labels = None
depends_on = None

# (新码, 名称, 授给已拥有该码的角色;None 表示只授给 admin 角色)
NEW_PERMISSIONS = [
    ("admin:user:list", "查看用户列表", "admin:user:read"),
    ("admin:audit:read_archive", "查看归档审计日志", "admin:audit:read"),
    ("admin:maintenance:archive_audit", "触发审计日志归档", None),
]


def _permission_id(conn, code: str) -> int | None:
    return conn.execute(sa.text("SELECT id FROM permission WHERE code = :c"), {"c": code}).scalar()


def upgrade() -> None:
    op.create_index(op.f("audit_logs_created_at_idx"), "audit_logs", ["created_at"], unique=False)
    op.create_table(
        "audit_logs_archive",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=255), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("ip", sa.String(length=45), nullable=True),
        sa.Column("resource_id", sa.Integer(), nullable=True),
        sa.Column("resource", sa.String(length=255), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("audit_logs_archive_pkey")),
    )
    for col in ("created_at", "user_id", "action", "resource_id"):
        op.create_index(
            op.f(f"audit_logs_archive_{col}_idx"), "audit_logs_archive", [col], unique=False
        )

    conn = op.get_bind()
    for code, name, inherit_from in NEW_PERMISSIONS:
        # 码可能已被 POST /permissions 建过:查不到再建,授权跳过已有的
        pid = _permission_id(conn, code)
        if pid is None:
            conn.execute(
                sa.text("INSERT INTO permission (code, name) VALUES (:c, :n)"),
                {"c": code, "n": name},
            )
            pid = _permission_id(conn, code)
        if inherit_from is None:
            roles = "SELECT id AS role_id FROM role WHERE name = 'admin'"
        else:
            roles = (
                "SELECT rp.role_id FROM role_permission rp "
                "JOIN permission p ON p.id = rp.permission_id WHERE p.code = :inherit_from"
            )
        conn.execute(
            sa.text(
                "INSERT INTO role_permission (role_id, permission_id) "
                f"SELECT r.role_id, :p FROM ({roles}) r WHERE r.role_id NOT IN "
                "(SELECT role_id FROM role_permission WHERE permission_id = :p)"
            ),
            {"p": pid, "inherit_from": inherit_from},
        )


def downgrade() -> None:
    conn = op.get_bind()
    # 检查与删表之间禁止归档写入,避免判空后又有数据进入。
    conn.execute(sa.text("LOCK TABLE audit_logs_archive IN ACCESS EXCLUSIVE MODE"))
    if conn.execute(sa.text("SELECT 1 FROM audit_logs_archive LIMIT 1")).scalar() is not None:
        raise CommandError("audit_logs_archive 非空，禁止降级；请先安全迁移归档数据。")
    for code, _, _ in NEW_PERMISSIONS:
        pid = _permission_id(conn, code)
        if pid is not None:
            conn.execute(
                sa.text("DELETE FROM role_permission WHERE permission_id = :p"), {"p": pid}
            )
            conn.execute(sa.text("DELETE FROM permission WHERE id = :p"), {"p": pid})
    op.drop_table("audit_logs_archive")
    op.drop_index(op.f("audit_logs_created_at_idx"), table_name="audit_logs")
