"""Keep the retry budget boundary when an older writer resumes a message."""

from alembic import op

revision = "reliable_resume_compat_v1"
down_revision = "reliable_retry_budget_v1"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION enterprise_backend.reset_resumed_retry_budget() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                UPDATE enterprise_backend.reliable_delivery_attempt
                SET counts_toward_retry_budget = false
                WHERE message_id = NEW.id AND counts_toward_retry_budget;
                RETURN NEW;
            END;
            $$
        """)
        op.execute("""
            CREATE TRIGGER reliable_resume_retry_budget
            AFTER UPDATE OF status, not_received_count ON enterprise_backend.reliable_message
            FOR EACH ROW
            WHEN (OLD.status = 'STALLED' AND NEW.status = 'PENDING' AND NEW.not_received_count = 0)
            EXECUTE FUNCTION enterprise_backend.reset_resumed_retry_budget()
        """)
    else:
        op.execute("""
            CREATE TRIGGER reliable_resume_retry_budget
            AFTER UPDATE OF status, not_received_count ON reliable_message
            FOR EACH ROW
            WHEN OLD.status = 'STALLED' AND NEW.status = 'PENDING' AND NEW.not_received_count = 0
            BEGIN
                UPDATE reliable_delivery_attempt
                SET counts_toward_retry_budget = false
                WHERE message_id = NEW.id AND counts_toward_retry_budget;
            END
        """)


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER reliable_resume_retry_budget ON enterprise_backend.reliable_message"
        )
        op.execute("DROP FUNCTION enterprise_backend.reset_resumed_retry_budget()")
    else:
        op.execute("DROP TRIGGER reliable_resume_retry_budget")
