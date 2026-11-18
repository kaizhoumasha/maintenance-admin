"""Keep access and refresh revocation across account reactivation."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "user_session_version_v1"
down_revision = "rack_ctu03_storage_release"
branch_labels = None
depends_on = None


def _users():
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    return sa.table(
        "user", sa.column("session_version", sa.Integer), sa.column("is_active"), schema=schema
    )


def upgrade():
    users = _users()
    op.add_column(
        "user",
        sa.Column("session_version", sa.Integer, nullable=False, server_default=sa.text("0")),
        schema=users.schema,
    )
    op.execute(users.update().where(users.c.is_active.is_(False)).values(session_version=1))


def downgrade():
    users = _users()
    if op.get_bind().scalar(sa.select(sa.exists().where(users.c.session_version != 0))):
        raise CommandError("Downgrade would restore revoked user tokens")
    op.drop_column("user", "session_version", schema=users.schema)
