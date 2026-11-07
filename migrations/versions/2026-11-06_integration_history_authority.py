"""Prepare reconciled storage for the new writer; retain all legacy evidence."""

from hashlib import sha256

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

from migrations.integration_history_v1 import validate_originals
from migrations.integration_history_views_v1 import create_views, drop_views

revision = "integrationhistoryauthority"
down_revision = "integrationhistoryexpand"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    for query in (
        "SELECT count(*) FROM reliable_message WHERE exchange_id IS NULL OR "
        "request_record_id IS NULL",
        "SELECT count(*) FROM transport_task WHERE submission_exchange_id IS NULL",
        "SELECT count(*) FROM ecs_command c LEFT JOIN integration_exchange e "
        "ON e.command_code=c.command_code AND e.role='ECS_COMMAND' WHERE e.id IS NULL",
        "SELECT count(*) FROM callback_receipt c LEFT JOIN integration_exchange e "
        "ON e.receipt_id=c.id AND e.role='RECEIPT' WHERE e.id IS NULL",
    ):
        if bind.scalar(sa.text(query)):
            raise CommandError("refusing authority preparation: unmapped original identities")
    for column, original in (
        ("request_record_id", "request_raw"),
        ("terminal_response_record_id", "response_raw"),
        ("last_response_record_id", "last_response_raw"),
    ):
        rows = bind.execute(
            sa.text(
                f"SELECT m.{original} AS original, r.exchange_id AS record_exchange, "
                "m.exchange_id AS message_exchange, r.body_record_id, "
                "COALESCE(b.raw_body,r.raw_body) AS body, "
                "COALESCE(b.body_sha256,r.body_sha256) AS digest, "
                "COALESCE(b.stored_bytes,r.stored_bytes) AS stored_bytes, "
                "b.body_record_id AS chained_ref "
                f"FROM reliable_message m LEFT JOIN integration_record r ON r.id=m.{column} "
                "LEFT JOIN integration_record b ON b.id=r.body_record_id "
                f"WHERE m.{original} IS NOT NULL"
            )
        ).mappings()
        for row in rows:
            body = row["body"]
            if (
                body is None
                or bytes(body) != bytes(row["original"])
                or row["record_exchange"] != row["message_exchange"]
                or row["chained_ref"] is not None
                or row["digest"] != sha256(bytes(body)).hexdigest()
                or row["stored_bytes"] != len(body)
            ):
                raise CommandError("refusing authority preparation: original body mismatch")
    validate_originals(bind)
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    drop_views(bind)
    with op.batch_alter_table("reliable_message", schema=schema) as batch:
        batch.alter_column("request_raw", existing_type=sa.LargeBinary(), nullable=True)
        for column in ("exchange_id", "request_record_id"):
            batch.alter_column(column, existing_type=sa.Uuid(), nullable=False)
    with op.batch_alter_table("transport_task", schema=schema) as batch:
        batch.alter_column("submission_exchange_id", existing_type=sa.Uuid(), nullable=False)

    create_views(bind)


def downgrade():
    bind = op.get_bind()
    if any(
        bind.scalar(sa.text(query))
        for query in (
            "SELECT count(*) FROM integration_record WHERE capture_origin='LIVE'",
            "SELECT count(*) FROM integration_exchange WHERE capture_origin='LIVE'",
            "SELECT count(*) FROM reliable_message WHERE request_raw IS NULL",
        )
    ):
        raise CommandError("refusing downgrade: preserve new authoritative facts")
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    drop_views(bind)
    with op.batch_alter_table("transport_task", schema=schema) as batch:
        batch.alter_column("submission_exchange_id", existing_type=sa.Uuid(), nullable=True)
    with op.batch_alter_table("reliable_message", schema=schema) as batch:
        for column in ("exchange_id", "request_record_id"):
            batch.alter_column(column, existing_type=sa.Uuid(), nullable=True)
        batch.alter_column("request_raw", existing_type=sa.LargeBinary(), nullable=False)

    create_views(bind)
