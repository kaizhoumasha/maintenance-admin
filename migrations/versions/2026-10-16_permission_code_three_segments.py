"""permission_code_three_segments

权限码改为三段式 <module>:<resource>:<action>,并让一个码只对应一个路由:
role:read / role:write 被多个路由共用,拆成独立的码。已有角色的授权按旧码原样展开到新码上。
库里手工创建的其它旧格式权限码不动(没有路由引用它们)。audit_logs.action 里的历史值也不改写。

Revision ID: d52e8a6b1f93
Revises: b01745e5c719
Create Date: 2026-10-16 05:30:00.000000+00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "d52e8a6b1f93"
down_revision = "b01745e5c719"
branch_labels = None
depends_on = None

# 旧码 -> (旧码名称, [(新码, 新码名称), ...])
MAPPING: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "user:create": ("创建用户", [("admin:user:create", "创建用户")]),
    "user:read": ("查看用户", [("admin:user:read", "查看用户")]),
    "role:assign": ("分配用户角色", [("admin:user:assign_role", "分配用户角色")]),
    "audit:read": ("查看审计日志", [("admin:audit:read", "查看审计日志")]),
    "role:read": (
        "查看角色与权限",
        [("admin:role:read", "查看角色"), ("admin:permission:read", "查看权限")],
    ),
    "role:write": (
        "管理角色与权限",
        [
            ("admin:role:create", "创建角色"),
            ("admin:role:set_permissions", "设置角色权限"),
            ("admin:permission:create", "创建权限"),
        ],
    ),
}


def _permission_id(conn, code: str) -> int | None:
    return conn.execute(sa.text("SELECT id FROM permission WHERE code = :c"), {"c": code}).scalar()


def _get_or_create(conn, code: str, name: str) -> int:
    pid = _permission_id(conn, code)
    if pid is None:
        conn.execute(
            sa.text("INSERT INTO permission (code, name) VALUES (:c, :n)"), {"c": code, "n": name}
        )
        pid = _permission_id(conn, code)
    return pid


def _grant_same_roles(conn, source_id: int, target_id: int) -> None:
    # 拥有 source 的角色也拥有 target(已有的不重复插入)
    conn.execute(
        sa.text(
            "INSERT INTO role_permission (role_id, permission_id) "
            "SELECT role_id, :t FROM role_permission WHERE permission_id = :s "
            "AND role_id NOT IN (SELECT role_id FROM role_permission WHERE permission_id = :t)"
        ),
        {"s": source_id, "t": target_id},
    )


def _drop(conn, permission_id: int) -> None:
    # 先显式删关联行,不依赖外键级联(SQLite 默认不开外键)
    params = {"p": permission_id}
    conn.execute(sa.text("DELETE FROM role_permission WHERE permission_id = :p"), params)
    conn.execute(sa.text("DELETE FROM permission WHERE id = :p"), params)


def upgrade() -> None:
    conn = op.get_bind()
    for old_code, (_, news) in MAPPING.items():
        old_id = _permission_id(conn, old_code)
        if old_id is None:  # 这个库没有该旧码(例如从没跑过 seed)
            continue
        for new_code, new_name in news:
            _grant_same_roles(conn, old_id, _get_or_create(conn, new_code, new_name))
        _drop(conn, old_id)


def downgrade() -> None:
    """有损:拆分前合并的权限,只要角色拥有任何一个拆分后的码,就拥有合并后的旧码(权限可能变宽)。"""
    conn = op.get_bind()
    for old_code, (old_name, news) in MAPPING.items():
        new_ids = [i for code, _ in news if (i := _permission_id(conn, code)) is not None]
        if not new_ids:
            continue
        old_id = _get_or_create(conn, old_code, old_name)
        for new_id in new_ids:
            _grant_same_roles(conn, new_id, old_id)
            _drop(conn, new_id)
