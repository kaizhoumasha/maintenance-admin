"""Evolve coarse scans in place and link original validation and movement actions."""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

from app.core.identifiers import uuid7
from app.core.strict_json import canonical_digest

revision = "coarsejobstage1"
down_revision = "smtautosources"
branch_labels = None
depends_on = None

SCAN = "coarse_sorting_scan"
JOB = "coarse_sorting_job"
ACTION = "coarse_sorting_action"
VALIDATE = "inbound.coarse_sorting.validate_bind@v1"
COLUMNS = (
    ("next_check_at", sa.DateTime(timezone=True)),
    ("rack_position_code", sa.String()),
    ("rack_binding_version", sa.Integer()),
    ("current_action_id", sa.String(36)),
    ("allocation_action_id", sa.String(36)),
    ("placed_at_ms", sa.BigInteger()),
    ("completed_at", sa.DateTime(timezone=True)),
)
CHECKS = {
    "state": (
        "state IN ('AWAITING_VALIDATION', 'AWAITING_MOVE', 'AWAITING_ALLOCATION', "
        "'AWAITING_PUT', 'AWAITING_REPORT', 'AWAITING_NG', 'COMPLETED')"
    ),
    "position": "(rack_position_code IS NULL) = (rack_binding_version IS NULL)",
    "allocation": (
        "state NOT IN ('AWAITING_PUT', 'AWAITING_REPORT', 'COMPLETED') "
        "OR (current_action_id IS NOT NULL AND allocation_action_id IS NOT NULL)"
    ),
    "placement": "state NOT IN ('AWAITING_REPORT', 'COMPLETED') OR placed_at_ms IS NOT NULL",
    "completion": "(state = 'COMPLETED') = (completed_at IS NOT NULL)",
}


def upgrade() -> None:
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    prefix = f"{schema}." if schema else ""
    op.rename_table(SCAN, JOB, schema=schema)
    for name, kind in COLUMNS:
        op.add_column(JOB, sa.Column(name, kind, nullable=True), schema=schema)
    op.add_column(
        JOB,
        sa.Column("state", sa.String(32), server_default="AWAITING_VALIDATION", nullable=False),
        schema=schema,
    )
    op.add_column(
        JOB,
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema=schema,
    )
    op.create_table(
        ACTION,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "job_id",
            sa.String(),
            sa.ForeignKey(f"{prefix}{JOB}.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("round_no", sa.Integer(), nullable=False),
        sa.Column(
            "predecessor_action_id",
            sa.String(36),
            sa.ForeignKey(f"{prefix}{ACTION}.id", ondelete="RESTRICT"),
        ),
        sa.Column("rack_generation_no", sa.Integer()),
        sa.Column("request_data", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("resource_snapshot", sa.JSON(none_as_null=True)),
        sa.Column("operation", sa.String()),
        sa.Column("operation_id", sa.String(36)),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("command_owner", sa.String()),
        sa.Column("call_key", sa.String()),
        sa.Column("command_code", sa.String()),
        sa.Column("device_code", sa.String()),
        sa.Column("evidence_history", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_consumed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "kind IN ('VALIDATE_BIND', 'MOVE_FORWARD', 'ALLOCATE_STORAGE', "
            "'PUT_TO_BIN', 'REPORT_COMPLETION')",
            name=op.f("coarse_sorting_action_kind_check"),
        ),
        sa.CheckConstraint(
            "round_no >= 1 AND (kind = 'ALLOCATE_STORAGE' OR round_no = 1)",
            name=op.f("coarse_sorting_action_round_check"),
        ),
        sa.CheckConstraint(
            "(kind = 'ALLOCATE_STORAGE') = (rack_generation_no IS NOT NULL) "
            "AND (rack_generation_no IS NULL OR rack_generation_no >= 0)",
            name=op.f("coarse_sorting_action_generation_check"),
        ),
        sa.CheckConstraint(
            "((kind IN ('VALIDATE_BIND', 'ALLOCATE_STORAGE', 'REPORT_COMPLETION')) "
            "AND operation IS NOT NULL AND operation_id IS NOT NULL AND message_id IS NOT NULL "
            "AND command_owner IS NULL AND call_key IS NULL "
            "AND command_code IS NULL AND device_code IS NULL) OR "
            "((kind IN ('MOVE_FORWARD', 'PUT_TO_BIN')) "
            "AND operation IS NULL AND operation_id IS NULL AND message_id IS NULL "
            "AND command_owner IS NOT NULL AND call_key IS NOT NULL "
            "AND command_code IS NOT NULL AND device_code IS NOT NULL "
            "AND resource_snapshot IS NOT NULL)",
            name=op.f("coarse_sorting_action_protocol_check"),
        ),
        sa.UniqueConstraint(
            "job_id", "kind", "round_no", name="coarse_sorting_action_job_kind_round_key"
        ),
        sa.UniqueConstraint("operation", "operation_id", name="coarse_sorting_action_wms_key"),
        sa.UniqueConstraint("command_owner", "call_key", name="coarse_sorting_action_ecs_key"),
        sa.UniqueConstraint("message_id", name="coarse_sorting_action_message_id_key"),
        sa.UniqueConstraint("command_code", name="coarse_sorting_action_command_code_key"),
        schema=schema,
    )
    _backfill(bind, schema)
    with op.batch_alter_table(JOB, schema=schema) as batch:
        for name, condition in CHECKS.items():
            batch.create_check_constraint(op.f(f"{JOB}_{name}_check"), condition)
        for column in ("current_action_id", "allocation_action_id"):
            batch.create_foreign_key(
                op.f(f"{JOB}_{column}_fkey"),
                ACTION,
                [column],
                ["id"],
                referent_schema=schema,
                ondelete="RESTRICT",
            )
    op.create_index(f"{JOB}_state_id_idx", JOB, ["state", "id"], schema=schema)
    op.create_index(f"{JOB}_line_state_idx", JOB, ["work_line_id", "state"], schema=schema)


def _backfill(bind, schema) -> None:
    job = sa.Table(JOB, sa.MetaData(), schema=schema, autoload_with=bind)
    action = sa.Table(ACTION, sa.MetaData(), schema=schema, autoload_with=bind)
    now = datetime.now(UTC)
    for row in bind.execute(sa.select(job)).mappings():
        state = "AWAITING_VALIDATION"
        if row["wait_reason"] == "AWAITING_NG_STAGE":
            state = "AWAITING_NG"
        elif row["wait_reason"] == "AWAITING_ALLOCATION_STAGE":
            state = "AWAITING_ALLOCATION"
        elif row["command_code"] is not None or row["wait_reason"] in {
            "AWAITING_MOVE_RESULT",
            "MOVE_RECONCILIATION_REQUIRED",
        }:
            state = "AWAITING_MOVE"

        def insert(
            kind, request_data, predecessor=None, evidence=None, *, job_id=row["id"], **refs
        ):
            action_id = str(uuid7())
            history = [{**evidence, "consumed_at": now.isoformat()}] if evidence else []
            bind.execute(
                action.insert().values(
                    id=action_id,
                    job_id=job_id,
                    kind=kind,
                    round_no=1,
                    request_data=request_data,
                    predecessor_action_id=predecessor,
                    evidence_history=history,
                    created_at=now,
                    last_consumed_at=now if history else None,
                    **refs,
                )
            )
            return action_id

        current = None
        if row["validation_operation_id"] and row["validation_message_id"] is not None:
            saved = row["validation_response"]
            evidence = None
            if (
                isinstance(saved, dict)
                and saved.get("operation_id") == row["validation_operation_id"]
                and saved.get("message_id") == row["validation_message_id"]
                and saved.get("response_state") == "VALID"
                and saved.get("http_status") == 200
                and saved.get("response_code") == "DECIDED"
            ):
                evidence = {
                    "operation_id": row["validation_operation_id"],
                    "code": "DECIDED",
                    "timestamp": saved.get("response_timestamp"),
                    "data": saved.get("response_data"),
                }
            current = insert(
                "VALIDATE_BIND",
                {"workline_code": row["workline_code"], **row["payload"]},
                evidence=evidence,
                operation=VALIDATE,
                operation_id=row["validation_operation_id"],
                message_id=row["validation_message_id"],
            )
        if row["command_code"] and row["conveyor_device_code"]:
            saved = row["move_result"]
            evidence = None
            if (
                isinstance(saved, dict)
                and saved.get("command_code") == row["command_code"]
                and isinstance(result := saved.get("result"), dict)
            ):
                evidence = {
                    "command_code": row["command_code"],
                    "digest": canonical_digest(result),
                    "data": result,
                }
            current = insert(
                "MOVE_FORWARD",
                {"barcode": row["payload"].get("barcode")},
                predecessor=current,
                evidence=evidence,
                command_owner="coarse_sorting",
                call_key=f"{row['id']}:MOVE_FORWARD",
                command_code=row["command_code"],
                device_code=row["conveyor_device_code"],
                resource_snapshot={
                    "device_id": row["conveyor_device_id"],
                    "device_code": row["conveyor_device_code"],
                    "device_version": row["conveyor_device_version"],
                    "role": "CONVEYOR",
                    "work_line_version": row["work_line_version"],
                },
            )
        bind.execute(
            job.update()
            .where(job.c.id == row["id"])
            .values(
                state=state,
                current_action_id=current,
                updated_at=row["received_at"],
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    if schema:
        bind.execute(
            sa.text(f"LOCK TABLE {schema}.{JOB}, {schema}.{ACTION} IN ACCESS EXCLUSIVE MODE")
        )
    action = sa.table(ACTION, sa.column("kind"), schema=schema)
    if bind.execute(
        sa.select(sa.literal(1))
        .select_from(action)
        .where(action.c.kind.not_in(("VALIDATE_BIND", "MOVE_FORWARD")))
        .limit(1)
    ).first():
        raise CommandError("coarse direct put evidence exists; refusing destructive downgrade")
    for index in (f"{JOB}_state_id_idx", f"{JOB}_line_state_idx"):
        op.drop_index(index, table_name=JOB, schema=schema)
    with op.batch_alter_table(JOB, schema=schema) as batch:
        for name in CHECKS:
            batch.drop_constraint(op.f(f"{JOB}_{name}_check"), type_="check")
        for column in ("current_action_id", "allocation_action_id"):
            batch.drop_constraint(op.f(f"{JOB}_{column}_fkey"), type_="foreignkey")
        for name, _ in COLUMNS:
            batch.drop_column(name)
        batch.drop_column("state")
        batch.drop_column("updated_at")
    op.drop_table(ACTION, schema=schema)
    op.rename_table(JOB, SCAN, schema=schema)
