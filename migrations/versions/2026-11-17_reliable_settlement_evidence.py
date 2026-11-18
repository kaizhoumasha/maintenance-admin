"""Preserve undispatched lease evidence and the settled retry budget."""

import sqlalchemy as sa
from alembic import op

revision = "reliable_settlement_evidence_v1"
down_revision = "reliable_resume_compat_v1"
branch_labels = None
depends_on = None


def upgrade():
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    op.add_column(
        "reliable_message",
        sa.Column("undispatched_attempts", sa.Integer(), server_default="0", nullable=False),
        schema=schema,
    )
    op.add_column(
        "reliable_message", sa.Column("settled_retry_budget", sa.Integer()), schema=schema
    )
    if schema:
        op.execute("""
            CREATE FUNCTION enterprise_backend.keep_reliable_settlement_evidence() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.status = 'SENDING' AND OLD.scope_owner IS NOT NULL
                    AND OLD.lease_not_started IS TRUE AND NEW.lease_id IS NULL
                    AND NEW.attempts = OLD.attempts + 1
                    AND NEW.undispatched_attempts = OLD.undispatched_attempts THEN
                    NEW.undispatched_attempts := OLD.undispatched_attempts + 1;
                END IF;
                IF OLD.status = 'STALLED' AND NEW.status = 'PENDING'
                    AND NEW.not_received_count = 0 THEN
                    NEW.settled_retry_budget := NULL;
                END IF;
                RETURN NEW;
            END;
            $$
        """)
        op.execute("""
            CREATE TRIGGER reliable_settlement_evidence
            BEFORE UPDATE OF attempts, status ON enterprise_backend.reliable_message
            FOR EACH ROW EXECUTE FUNCTION enterprise_backend.keep_reliable_settlement_evidence()
        """)
    else:
        op.execute("""
            CREATE TRIGGER reliable_undispatched_attempt
            AFTER UPDATE OF attempts ON reliable_message
            FOR EACH ROW
            WHEN OLD.status = 'SENDING' AND OLD.scope_owner IS NOT NULL
                AND OLD.lease_not_started IS TRUE AND NEW.lease_id IS NULL
                AND NEW.attempts = OLD.attempts + 1
                AND NEW.undispatched_attempts = OLD.undispatched_attempts
            BEGIN
                UPDATE reliable_message SET undispatched_attempts = OLD.undispatched_attempts + 1
                WHERE id = NEW.id;
            END
        """)
        op.execute("""
            CREATE TRIGGER reliable_reset_settled_budget
            AFTER UPDATE OF status ON reliable_message
            FOR EACH ROW
            WHEN OLD.status = 'STALLED' AND NEW.status = 'PENDING' AND NEW.not_received_count = 0
            BEGIN
                UPDATE reliable_message SET settled_retry_budget = NULL WHERE id = NEW.id;
            END
        """)


def downgrade():
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    if schema:
        op.execute(
            "DROP TRIGGER reliable_settlement_evidence ON enterprise_backend.reliable_message"
        )
        op.execute("DROP FUNCTION enterprise_backend.keep_reliable_settlement_evidence()")
    else:
        op.execute("DROP TRIGGER reliable_undispatched_attempt")
        op.execute("DROP TRIGGER reliable_reset_settled_budget")
    op.drop_column("reliable_message", "settled_retry_budget", schema=schema)
    op.drop_column("reliable_message", "undispatched_attempts", schema=schema)
