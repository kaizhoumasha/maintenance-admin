"""Verify frozen requests and preserve active ECS openings."""

from hashlib import sha256

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

from migrations.http_exchange_runtime_v1 import restore_active_ecs_attempts

revision = "http_exchange_request_authority"
down_revision = "http_exchange_expand"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    if schema:
        bind.exec_driver_sql(
            "LOCK TABLE enterprise_backend.integration_exchange, "
            "enterprise_backend.integration_record, enterprise_backend.reliable_message, "
            "enterprise_backend.reliable_delivery_attempt IN ACCESS EXCLUSIVE MODE NOWAIT"
        )
    op.add_column(
        "reliable_message", sa.Column("frozen_request_wire", sa.LargeBinary()), schema=schema
    )
    op.add_column(
        "reliable_message", sa.Column("frozen_request_sha256", sa.String(64)), schema=schema
    )
    op.create_index(
        "reliable_message_operation_id_idx", "reliable_message", ["operation_id"], schema=schema
    )
    metadata = sa.MetaData(schema=schema)
    messages = sa.Table("reliable_message", metadata, autoload_with=bind, resolve_fks=False)
    records = sa.Table("integration_record", metadata, autoload_with=bind, resolve_fks=False)
    body = records.alias("body")
    cursor = 0
    while True:
        rows = (
            bind.execute(
                sa.select(
                    messages.c.id,
                    messages.c.exchange_id,
                    records.c.exchange_id.label("source_exchange"),
                    records.c.record_kind,
                    records.c.body_record_id,
                    records.c.body_state,
                    records.c.raw_body,
                    records.c.body_sha256,
                    records.c.stored_bytes,
                    body.c.id.label("canonical_id"),
                    body.c.body_record_id.label("canonical_parent"),
                    body.c.raw_body.label("canonical_raw"),
                    body.c.body_state.label("canonical_state"),
                    body.c.body_sha256.label("canonical_sha"),
                    body.c.stored_bytes.label("canonical_bytes"),
                )
                .select_from(
                    messages.outerjoin(
                        records, records.c.id == messages.c.request_record_id
                    ).outerjoin(body, body.c.id == records.c.body_record_id)
                )
                .where(messages.c.id > cursor)
                .order_by(messages.c.id)
                .limit(500)
            )
            .mappings()
            .all()
        )
        if not rows:
            break
        values = []
        for row in rows:
            if (
                row["source_exchange"] != row["exchange_id"]
                or row["record_kind"] != "REQUEST_REGISTERED"
            ):
                raise CommandError(f"frozen request integrity mismatch for message {row['id']}")
            raw, digest, size, state = (
                row["raw_body"],
                row["body_sha256"],
                row["stored_bytes"],
                row["body_state"],
            )
            if row["body_record_id"] is not None:
                if row["canonical_id"] is None or row["canonical_parent"] is not None:
                    raise CommandError(f"frozen request integrity mismatch for message {row['id']}")
                raw, digest, size, state = (
                    row["canonical_raw"],
                    row["canonical_sha"],
                    row["canonical_bytes"],
                    row["canonical_state"],
                )
            if (
                raw is None
                or state != "FULL"
                or size != len(raw)
                or digest != sha256(raw).hexdigest()
            ):
                raise CommandError(f"frozen request integrity mismatch for message {row['id']}")
            values.append({"message_id": row["id"], "wire": raw, "digest": digest})
        bind.execute(
            messages.update()
            .where(messages.c.id == sa.bindparam("message_id"))
            .values(
                frozen_request_wire=sa.bindparam("wire"),
                frozen_request_sha256=sa.bindparam("digest"),
            ),
            values,
        )
        cursor = rows[-1]["id"]
    restore_active_ecs_attempts(bind, schema, metadata, messages, records)


def downgrade():
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    op.drop_index("reliable_message_operation_id_idx", table_name="reliable_message", schema=schema)
    op.drop_column("reliable_message", "frozen_request_sha256", schema=schema)
    op.drop_column("reliable_message", "frozen_request_wire", schema=schema)
