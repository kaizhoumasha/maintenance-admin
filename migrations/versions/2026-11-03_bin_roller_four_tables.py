"""Replace inactive BIN private checkpoints without copying old business data."""

import sqlalchemy as sa
from alembic import op

revision = "rollerfour"
down_revision = "rollerfoundation"
branch_labels = None
depends_on = None


def _schema(bind):
    return None if bind.dialect.name == "sqlite" else bind.dialect.default_schema_name


def _table(bind, name):
    return sa.Table(name, sa.MetaData(), schema=_schema(bind), autoload_with=bind)


def _reject_if(bind, table, condition):
    if (
        bind.scalar(sa.select(sa.literal(1)).select_from(table).where(condition).limit(1))
        is not None
    ):
        raise RuntimeError(
            "BIN four-table cutover refused: original active responsibility in " + table.name
        )


def _guard_old(bind):
    scans = _table(bind, "bin_roller_scan_arrival")
    roots = scans.alias("original_root")
    resolved_round = (
        sa.select(roots.c.id)
        .where(
            roots.c.point == "SCAN3",
            roots.c.round_id == scans.c.round_id,
            roots.c.owner_domain == scans.c.owner_domain,
            roots.c.work_line_id == scans.c.work_line_id,
            sa.or_(roots.c.removed.is_(True), roots.c.return_state == "COMPLETED"),
        )
        .exists()
    )
    _reject_if(
        bind,
        scans,
        sa.or_(
            scans.c.release_state == "PENDING",
            scans.c.pending.is_(True) & ~((scans.c.point == "SCAN4") & resolved_round),
            (scans.c.point == "SCAN3")
            & scans.c.removed.is_(False)
            & (scans.c.return_state != "COMPLETED"),
        ),
    )
    for name, condition in (
        ("bin_roller_feed_segment", "pending"),
        ("bin_roller_return_batch", "pending"),
        ("bin_roller_return_opportunity", "NOT ended"),
        ("bin_roller_decision", "pending"),
    ):
        _reject_if(bind, _table(bind, name), sa.text(condition))
    faces = _table(bind, "bin_roller_face_work")
    _reject_if(
        bind,
        faces,
        sa.or_(
            faces.c.service_state.in_(("PENDING", "READY")),
            faces.c.feed_state.in_(("REQUESTED", "ALLOCATED", "REVIEW")),
        ),
    )
    jobs = _table(bind, "rack_dispatch_job")
    _reject_if(
        bind,
        jobs,
        jobs.c.source_kind.in_(("ROLLER_ORDINARY", "ROLLER_DRAIN")) & jobs.c.pending.is_(True),
    )
    tasks = _table(bind, "smt_manual_task")
    members = _table(bind, "smt_manual_task_member")
    active_bin = (
        sa.select(members.c.id)
        .where(members.c.task_id == tasks.c.task_id, members.c.kind == "BIN")
        .exists()
    )
    _reject_if(bind, tasks, tasks.c.status.not_in(("COMPLETED", "CANCELLED")) & active_bin)
    bins = _table(bind, "smt_manual_bin")
    _reject_if(bind, bins, bins.c.arrival_id.in_(sa.select(scans.c.id)) & bins.c.pending.is_(True))
    commands = _table(bind, "ecs_command")
    codes = sa.union_all(sa.select(scans.c.command_code), sa.select(scans.c.release_command_code))
    _reject_if(
        bind,
        commands,
        commands.c.command_code.in_(codes)
        & sa.or_(
            commands.c.status.not_in(("SUCCEEDED", "FAILED", "REJECTED", "EXPIRED")),
            commands.c.needs_reconciliation.is_(True),
            commands.c.blocked.is_(True),
        ),
    )
    segments, batches, decisions = (
        _table(bind, name)
        for name in ("bin_roller_feed_segment", "bin_roller_return_batch", "bin_roller_decision")
    )
    transport = _table(bind, "transport_task")
    related_transport = sa.or_(
        transport.c.client_request_id.in_(
            sa.union_all(
                sa.select(segments.c.client_request_id), sa.select(batches.c.client_request_id)
            )
        ),
        transport.c.cycle_id.in_(
            sa.select(jobs.c.original_cycle_id).where(
                jobs.c.source_kind.in_(("ROLLER_ORDINARY", "ROLLER_DRAIN"))
            )
        ),
    )
    _reject_if(
        bind,
        transport,
        related_transport
        & sa.or_(
            transport.c.pending_outcome.is_not(None),
            transport.c.applied_outcome.is_(None),
            transport.c.outcome_status == "UNKNOWN",
        ),
    )
    messages = _table(bind, "reliable_message")
    referenced = sa.union_all(
        sa.select(faces.c.message_id),
        sa.select(batches.c.message_id),
        sa.select(decisions.c.message_id),
    )
    related = sa.or_(
        messages.c.id.in_(referenced),
        messages.c.business_key.in_(codes),
        messages.c.operation_id.in_(sa.select(transport.c.operation_id).where(related_transport)),
    )
    _reject_if(
        bind, messages, related & messages.c.status.in_(("PENDING", "SENDING", "STALLED", "HELD"))
    )


def _new_schema(schema):
    metadata = sa.MetaData(schema=schema)
    tables = {}
    tables["bin_passage"] = sa.Table(
        "bin_roller_bin_passage",
        metadata,
        sa.Column("scope_kind", sa.String(), nullable=False),
        sa.Column("bin_code", sa.String(), nullable=True),
        sa.Column("scan1_event_id", sa.BigInteger(), nullable=True),
        sa.Column("scan1_visit_key", sa.String(), nullable=True),
        sa.Column("scan2_event_id", sa.BigInteger(), nullable=True),
        sa.Column("scan2_visit_key", sa.String(), nullable=True),
        sa.Column("scan1_command_code", sa.String(), nullable=True),
        sa.Column("scan1_consumed_result_id", sa.String(), nullable=True),
        sa.Column("production_record_id", sa.String(), nullable=True),
        sa.Column("release_command_code", sa.String(), nullable=True),
        sa.Column("release_consumed_result_id", sa.String(), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("scope_id", sa.String(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(), nullable=True),
        sa.CheckConstraint(
            (
                "(closed_at IS NULL AND close_reason IS NULL) OR (closed_at IS NOT NULL AND "
                "close_reason IS NOT NULL)"
            ),
            name="bin_roller_bin_passage_closure_check",
        ),
        sa.CheckConstraint(
            (
                "(scan1_consumed_result_id IS NULL OR scan1_command_code IS NOT NULL) AND "
                "(release_consumed_result_id IS NULL OR release_command_code IS NOT NULL)"
            ),
            name="bin_roller_bin_passage_consumption_check",
        ),
        sa.CheckConstraint(
            (
                "(scan1_event_id IS NULL) = (scan1_visit_key IS NULL) AND (scan2_event_id IS "
                "NULL) = (scan2_visit_key IS NULL) AND (scan1_event_id IS NOT NULL OR "
                "scan2_event_id IS NOT NULL)"
            ),
            name="bin_roller_bin_passage_events_check",
        ),
        sa.CheckConstraint(
            "scope_id != '' AND revision >= 0", name="bin_roller_bin_passage_identity_check"
        ),
        sa.CheckConstraint(
            "scope_kind IN ('TASK','LINE')", name="bin_roller_bin_passage_scope_kind_check"
        ),
        sa.PrimaryKeyConstraint("id", name="bin_roller_bin_passage_pkey"),
        sa.UniqueConstraint(
            "id", "owner_domain", "work_line_id", name="bin_roller_bin_passage_scope"
        ),
    )
    sa.Index(
        "bin_roller_bin_passage_work",
        tables["bin_passage"].c["owner_domain"],
        tables["bin_passage"].c["work_line_id"],
        tables["bin_passage"].c["closed_at"],
        tables["bin_passage"].c["id"],
        unique=False,
    )
    sa.Index(
        "bin_roller_passage_predecessor",
        tables["bin_passage"].c["owner_domain"],
        tables["bin_passage"].c["work_line_id"],
        tables["bin_passage"].c["scope_id"],
        tables["bin_passage"].c["bin_code"],
        unique=False,
        sqlite_where=sa.text(
            "scope_kind='TASK' AND closed_at IS NULL AND scan1_event_id IS NOT NULL AND "
            "scan2_event_id IS NULL"
        ),
        postgresql_where=sa.text(
            "scope_kind='TASK' AND closed_at IS NULL AND scan1_event_id IS NOT NULL AND "
            "scan2_event_id IS NULL"
        ),
    )
    sa.Index(
        "bin_roller_release_command_code_key",
        tables["bin_passage"].c["owner_domain"],
        tables["bin_passage"].c["work_line_id"],
        tables["bin_passage"].c["release_command_code"],
        unique=True,
        sqlite_where=sa.text("release_command_code IS NOT NULL"),
        postgresql_where=sa.text("release_command_code IS NOT NULL"),
    )
    sa.Index(
        "bin_roller_scan1_event_id_key",
        tables["bin_passage"].c["owner_domain"],
        tables["bin_passage"].c["work_line_id"],
        tables["bin_passage"].c["scan1_event_id"],
        unique=True,
        sqlite_where=sa.text("scan1_event_id IS NOT NULL"),
        postgresql_where=sa.text("scan1_event_id IS NOT NULL"),
    )
    sa.Index(
        "bin_roller_scan1_visit_key_key",
        tables["bin_passage"].c["owner_domain"],
        tables["bin_passage"].c["work_line_id"],
        tables["bin_passage"].c["scan1_visit_key"],
        unique=True,
        sqlite_where=sa.text("scan1_visit_key IS NOT NULL"),
        postgresql_where=sa.text("scan1_visit_key IS NOT NULL"),
    )
    sa.Index(
        "bin_roller_scan2_event_id_key",
        tables["bin_passage"].c["owner_domain"],
        tables["bin_passage"].c["work_line_id"],
        tables["bin_passage"].c["scan2_event_id"],
        unique=True,
        sqlite_where=sa.text("scan2_event_id IS NOT NULL"),
        postgresql_where=sa.text("scan2_event_id IS NOT NULL"),
    )
    sa.Index(
        "bin_roller_scan2_visit_key_key",
        tables["bin_passage"].c["owner_domain"],
        tables["bin_passage"].c["work_line_id"],
        tables["bin_passage"].c["scan2_visit_key"],
        unique=True,
        sqlite_where=sa.text("scan2_visit_key IS NOT NULL"),
        postgresql_where=sa.text("scan2_visit_key IS NOT NULL"),
    )
    tables["bin_return"] = sa.Table(
        "bin_roller_bin_return",
        metadata,
        sa.Column("bin_code", sa.String(), nullable=True),
        sa.Column("scan3_event_id", sa.BigInteger(), nullable=True),
        sa.Column("scan3_visit_key", sa.String(), nullable=True),
        sa.Column("scan3_command_code", sa.String(), nullable=True),
        sa.Column("scan3_consumed_result_id", sa.String(), nullable=True),
        sa.Column("first_scan4_event_id", sa.BigInteger(), nullable=True),
        sa.Column("first_scan4_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fifo_visit_key", sa.String(), nullable=True),
        sa.Column("scan4_event_id", sa.BigInteger(), nullable=True),
        sa.Column("scan4_visit_key", sa.String(), nullable=True),
        sa.Column("scan4_command_code", sa.String(), nullable=True),
        sa.Column("scan4_consumed_result_id", sa.String(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("return_state", sa.String(), nullable=False),
        sa.Column("selected_face_work_id", sa.String(), nullable=True),
        sa.Column("selected_step_id", sa.String(), nullable=True),
        sa.Column("transport_task_id", sa.String(), nullable=True),
        sa.Column("client_request_id", sa.String(), nullable=True),
        sa.Column("caller", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("consumed_outcome_revision", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("scope_id", sa.String(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(), nullable=True),
        sa.CheckConstraint(
            (
                "(closed_at IS NULL AND close_reason IS NULL) OR (closed_at IS NOT NULL AND "
                "close_reason IS NOT NULL)"
            ),
            name="bin_roller_bin_return_closure_check",
        ),
        sa.CheckConstraint(
            (
                "(scan3_consumed_result_id IS NULL OR scan3_command_code IS NOT NULL) AND "
                "(scan4_consumed_result_id IS NULL OR scan4_command_code IS NOT NULL)"
            ),
            name="bin_roller_bin_return_consumption_check",
        ),
        sa.CheckConstraint(
            (
                "(scan3_event_id IS NULL) = (scan3_visit_key IS NULL) AND (scan4_event_id IS "
                "NULL) = (scan4_visit_key IS NULL) AND (scan3_event_id IS NOT NULL OR "
                "scan4_event_id IS NOT NULL)"
            ),
            name="bin_roller_bin_return_events_check",
        ),
        sa.CheckConstraint(
            "closed_at IS NOT NULL OR return_state != 'WAIT_FACT' OR selected_face_work_id IS NULL",
            name="bin_roller_bin_return_fact_only_check",
        ),
        sa.CheckConstraint(
            (
                "(first_scan4_event_id IS NULL AND first_scan4_received_at IS NULL AND "
                "fifo_visit_key IS NULL) OR (first_scan4_event_id IS NOT NULL AND "
                "first_scan4_received_at IS NOT NULL AND fifo_visit_key IS NOT NULL)"
            ),
            name="bin_roller_bin_return_fifo_check",
        ),
        sa.CheckConstraint(
            "scope_id != '' AND revision >= 0", name="bin_roller_bin_return_identity_check"
        ),
        sa.CheckConstraint(
            (
                "closed_at IS NOT NULL OR return_state NOT IN ('ALLOCATING','MOVING') OR "
                "selected_face_work_id IS NOT NULL"
            ),
            name="bin_roller_bin_return_selected_execution_check",
        ),
        sa.CheckConstraint(
            "(selected_face_work_id IS NULL) = (selected_step_id IS NULL)",
            name="bin_roller_bin_return_selection_check",
        ),
        sa.CheckConstraint(
            "return_state IN ('OBSERVED','WAITING','READY','ALLOCATING','MOVING','WAIT_FACT')",
            name="bin_roller_bin_return_state_check",
        ),
        sa.CheckConstraint(
            (
                "closed_at IS NOT NULL OR return_state NOT IN ('MOVING','WAIT_FACT') OR "
                "(transport_task_id IS NOT NULL AND client_request_id IS NOT NULL AND caller "
                "IS NOT NULL)"
            ),
            name="bin_roller_bin_return_transport_check",
        ),
        sa.ForeignKeyConstraint(
            ["selected_face_work_id", "selected_step_id", "owner_domain", "work_line_id"],
            [
                ((schema + ".") if schema else "") + value
                for value in [
                    "bin_roller_face_work.id",
                    "bin_roller_face_work.step_id",
                    "bin_roller_face_work.owner_domain",
                    "bin_roller_face_work.work_line_id",
                ]
            ],
            name="bin_roller_bin_return_selected_face_work_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="bin_roller_bin_return_pkey"),
        sa.UniqueConstraint(
            "id", "owner_domain", "work_line_id", name="bin_roller_bin_return_scope"
        ),
    )
    sa.Index(
        "bin_roller_bin_return_work",
        tables["bin_return"].c["owner_domain"],
        tables["bin_return"].c["work_line_id"],
        tables["bin_return"].c["closed_at"],
        tables["bin_return"].c["id"],
        unique=False,
    )
    sa.Index(
        "bin_roller_current_return_root",
        tables["bin_return"].c["owner_domain"],
        tables["bin_return"].c["work_line_id"],
        tables["bin_return"].c["bin_code"],
        unique=True,
        sqlite_where=sa.text(
            "is_current AND closed_at IS NULL AND bin_code IS NOT NULL AND "
            "scan3_event_id IS NOT NULL AND return_state != 'OBSERVED'"
        ),
        postgresql_where=sa.text(
            "is_current AND closed_at IS NULL AND bin_code IS NOT NULL AND "
            "scan3_event_id IS NOT NULL AND return_state != 'OBSERVED'"
        ),
    )
    sa.Index(
        "bin_roller_fifo",
        tables["bin_return"].c["owner_domain"],
        tables["bin_return"].c["work_line_id"],
        tables["bin_return"].c["first_scan4_received_at"],
        tables["bin_return"].c["fifo_visit_key"],
        unique=False,
    )
    sa.Index(
        "bin_roller_return_selection",
        tables["bin_return"].c["selected_face_work_id"],
        tables["bin_return"].c["selected_step_id"],
        unique=False,
    )
    sa.Index(
        "bin_roller_scan3_visit_key_key",
        tables["bin_return"].c["owner_domain"],
        tables["bin_return"].c["work_line_id"],
        tables["bin_return"].c["scan3_visit_key"],
        unique=True,
        sqlite_where=sa.text("scan3_visit_key IS NOT NULL"),
        postgresql_where=sa.text("scan3_visit_key IS NOT NULL"),
    )
    sa.Index(
        "bin_roller_scan4_visit_key_key",
        tables["bin_return"].c["owner_domain"],
        tables["bin_return"].c["work_line_id"],
        tables["bin_return"].c["scan4_visit_key"],
        unique=True,
        sqlite_where=sa.text("scan4_visit_key IS NOT NULL"),
        postgresql_where=sa.text("scan4_visit_key IS NOT NULL"),
    )
    tables["face_work"] = sa.Table(
        "bin_roller_face_work",
        metadata,
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=True),
        sa.Column("change_id", sa.String(), nullable=True),
        sa.Column("rack_id", sa.String(), nullable=False),
        sa.Column("rack_face", sa.String(), nullable=False),
        sa.Column("rack_job_id", sa.String(), nullable=True),
        sa.Column("preparation_action_id", sa.String(), nullable=True),
        sa.Column("demand_state", sa.String(), nullable=True),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("feed_operation_id", sa.String(), nullable=True),
        sa.Column("feed_message_id", sa.BigInteger(), nullable=True),
        sa.Column("feed_cursor", sa.Integer(), nullable=True),
        sa.Column("window_id", sa.String(), nullable=True),
        sa.Column("window_kind", sa.String(), nullable=True),
        sa.Column("step_id", sa.String(), nullable=True),
        sa.Column("return_operation_id", sa.String(), nullable=True),
        sa.Column("return_message_id", sa.BigInteger(), nullable=True),
        sa.Column("return_members", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("current_client_request_id", sa.String(), nullable=True),
        sa.Column("current_transport_task_id", sa.String(), nullable=True),
        sa.Column("consumed_outcome_revision", sa.BigInteger(), nullable=True),
        sa.Column("review_reason", sa.String(), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("scope_id", sa.String(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(), nullable=True),
        sa.CheckConstraint(
            (
                "(closed_at IS NULL AND close_reason IS NULL) OR (closed_at IS NOT NULL AND "
                "close_reason IS NOT NULL)"
            ),
            name="bin_roller_face_work_closure_check",
        ),
        sa.CheckConstraint(
            "feed_cursor IS NULL OR feed_cursor >= 0", name="bin_roller_face_work_cursor_check"
        ),
        sa.CheckConstraint(
            "scope_id != '' AND revision >= 0", name="bin_roller_face_work_identity_check"
        ),
        sa.CheckConstraint(
            (
                "phase IN "
                "('WAIT_RACK','WAIT_FEED_DECISION','FEED_MOVE','RETURN_WINDOW','WAIT_RETURN_D"
                "ECISION','RETURN_MOVE')"
            ),
            name="bin_roller_face_work_phase_check",
        ),
        sa.CheckConstraint(
            (
                "(purpose='SUPPLY' AND plan_revision IS NOT NULL AND change_id IS NOT NULL "
                "AND demand_state IS NOT NULL AND demand_state IN ('ACTIVE','WITHDRAWN')) OR "
                "(purpose='DRAIN' AND plan_revision IS NULL AND change_id IS NULL AND "
                "demand_state IS NULL AND feed_cursor IS NULL AND feed_operation_id IS NULL "
                "AND feed_message_id IS NULL)"
            ),
            name="bin_roller_face_work_purpose_check",
        ),
        sa.CheckConstraint(
            (
                "closed_at IS NOT NULL OR ((phase IN "
                "('FEED_MOVE','WAIT_RETURN_DECISION','RETURN_MOVE') AND step_id IS NOT NULL) "
                "OR (phase NOT IN ('FEED_MOVE','WAIT_RETURN_DECISION','RETURN_MOVE') AND "
                "step_id IS NULL))"
            ),
            name="bin_roller_face_work_step_check",
        ),
        sa.CheckConstraint(
            (
                "closed_at IS NOT NULL OR phase NOT IN ('FEED_MOVE','RETURN_MOVE') OR "
                "(current_client_request_id IS NOT NULL AND current_transport_task_id IS NOT "
                "NULL)"
            ),
            name="bin_roller_face_work_transport_check",
        ),
        sa.CheckConstraint(
            (
                "(window_id IS NULL AND window_kind IS NULL) OR (window_id IS NOT NULL AND "
                "window_kind IS NOT NULL AND window_kind IN "
                "('SEGMENT','FACE_END','PURE_RETURN'))"
            ),
            name="bin_roller_face_work_window_check",
        ),
        sa.PrimaryKeyConstraint("id", name="bin_roller_face_work_pkey"),
        sa.UniqueConstraint(
            "id", "step_id", "owner_domain", "work_line_id", name="bin_roller_face_step_scope"
        ),
        sa.UniqueConstraint(
            "id", "owner_domain", "work_line_id", name="bin_roller_face_work_scope"
        ),
    )
    sa.Index(
        "bin_roller_drain_face",
        tables["face_work"].c["rack_job_id"],
        tables["face_work"].c["rack_face"],
        unique=True,
        sqlite_where=sa.text("purpose='DRAIN'"),
        postgresql_where=sa.text("purpose='DRAIN'"),
    )
    sa.Index(
        "bin_roller_face_step",
        tables["face_work"].c["step_id"],
        unique=True,
        sqlite_where=sa.text("step_id IS NOT NULL AND closed_at IS NULL"),
        postgresql_where=sa.text("step_id IS NOT NULL AND closed_at IS NULL"),
    )
    sa.Index(
        "bin_roller_face_work_work",
        tables["face_work"].c["owner_domain"],
        tables["face_work"].c["work_line_id"],
        tables["face_work"].c["closed_at"],
        tables["face_work"].c["id"],
        unique=False,
    )
    sa.Index(
        "bin_roller_supply_face",
        tables["face_work"].c["owner_domain"],
        tables["face_work"].c["work_line_id"],
        tables["face_work"].c["scope_id"],
        tables["face_work"].c["plan_revision"],
        tables["face_work"].c["rack_id"],
        tables["face_work"].c["rack_face"],
        unique=True,
        sqlite_where=sa.text("purpose='SUPPLY'"),
        postgresql_where=sa.text("purpose='SUPPLY'"),
    )
    tables["workline_state"] = sa.Table(
        "bin_roller_workline_state",
        metadata,
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("line_run_id", sa.String(), nullable=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("drain_phase", sa.String(), nullable=False),
        sa.Column("drain_operation_id", sa.String(), nullable=True),
        sa.Column("drain_message_id", sa.BigInteger(), nullable=True),
        sa.Column("drain_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("candidate_cursor", sa.Integer(), nullable=True),
        sa.Column("drain_review_reason", sa.String(), nullable=True),
        sa.CheckConstraint(
            (
                "(drain_phase='IDLE' AND drain_operation_id IS NULL AND drain_message_id IS "
                "NULL AND drain_retry_at IS NULL AND candidate_cursor IS NULL) OR "
                "(drain_phase!='IDLE' AND line_run_id IS NOT NULL AND drain_operation_id IS "
                "NOT NULL AND drain_message_id IS NOT NULL AND candidate_cursor IS NOT NULL "
                "AND candidate_cursor >= 0 AND ((drain_phase='WAIT' AND drain_retry_at IS "
                "NOT NULL) OR (drain_phase!='WAIT' AND drain_retry_at IS NULL)))"
            ),
            name="bin_roller_workline_state_drain_check",
        ),
        sa.CheckConstraint(
            "drain_phase IN ('IDLE','REQUESTED','WAIT','USING')",
            name="bin_roller_workline_state_phase_check",
        ),
        sa.PrimaryKeyConstraint(
            "owner_domain", "work_line_id", name="bin_roller_workline_state_pkey"
        ),
    )
    for table in tables.values():
        table.c.revision.server_default = sa.DefaultClause(sa.text("0"))
        table.c.updated_at.server_default = sa.DefaultClause(sa.func.now())
        if "created_at" in table.c:
            table.c.created_at.server_default = sa.DefaultClause(sa.func.now())
    tables["face_work"].c.phase.server_default = sa.DefaultClause(sa.text("'WAIT_RACK'"))
    tables["bin_return"].c.is_current.server_default = sa.DefaultClause(sa.true())
    tables["bin_return"].c.return_state.server_default = sa.DefaultClause(sa.text("'OBSERVED'"))
    tables["workline_state"].c.drain_phase.server_default = sa.DefaultClause(sa.text("'IDLE'"))
    return metadata, tables


def _old_schema(schema):
    metadata = sa.MetaData(schema=schema)
    tables = {}
    tables["scan_fact"] = sa.Table(
        "bin_roller_scan_fact",
        metadata,
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("receipt_id", sa.Integer(), nullable=False),
        sa.Column("arrival_id", sa.String(), nullable=False),
        sa.Column("point", sa.String(), nullable=False),
        sa.Column("device_code", sa.String(), nullable=False),
        sa.Column("raw_bin_code", sa.String(), nullable=False),
        sa.Column("bin_code", sa.String(), nullable=True),
        sa.Column("scanned_at", sa.BigInteger(), nullable=False),
        sa.Column("binding_snapshot", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="bin_roller_scan_fact_pkey"),
        sa.UniqueConstraint("digest", name="bin_roller_scan_fact_digest_key"),
    )
    tables["scan_arrival"] = sa.Table(
        "bin_roller_scan_arrival",
        metadata,
        sa.Column("business_key", sa.String(64), nullable=False),
        sa.Column("first_digest", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("point", sa.String(), nullable=False),
        sa.Column("device_code", sa.String(), nullable=False),
        sa.Column("raw_bin_code", sa.String(), nullable=False),
        sa.Column("bin_code", sa.String(), nullable=True),
        sa.Column("scanned_at", sa.BigInteger(), nullable=False),
        sa.Column("previous_arrival_id", sa.String(), nullable=True),
        sa.Column("separator_command_code", sa.String(), nullable=True),
        sa.Column("round_id", sa.String(), nullable=True),
        sa.Column("return_state", sa.String(), nullable=True),
        sa.Column("removed", sa.Boolean(), nullable=True),
        sa.Column("first_scan4_arrival_id", sa.String(), nullable=True),
        sa.Column("delegation_state", sa.String(), nullable=True),
        sa.Column("received_work", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("release_record_id", sa.String(), nullable=True),
        sa.Column("release_call_key", sa.String(), nullable=True),
        sa.Column("release_command_code", sa.String(), nullable=True),
        sa.Column("release_state", sa.String(), nullable=True),
        sa.Column("release_consumed_result_id", sa.String(), nullable=True),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("command_code", sa.String(), nullable=True),
        sa.Column("consumed_result_id", sa.String(), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            (
                "(point = 'SCAN2' AND delegation_state IS NOT NULL AND delegation_state IN "
                "('PENDING','ACCEPTED')) OR (point != 'SCAN2' AND delegation_state IS NULL)"
            ),
            name="bin_roller_scan_arrival_delegation_check",
        ),
        sa.CheckConstraint(
            (
                "(release_record_id IS NULL AND release_call_key IS NULL AND release_state "
                "IS NULL AND release_command_code IS NULL AND release_consumed_result_id IS "
                "NULL) OR (point = 'SCAN2' AND release_record_id IS NOT NULL AND "
                "release_call_key IS NOT NULL AND release_state IS NOT NULL AND "
                "(release_state = 'PENDING' OR (release_state = 'CONSUMED' AND "
                "release_command_code IS NOT NULL AND release_command_code != '' AND "
                "release_consumed_result_id IS NOT NULL AND release_consumed_result_id != "
                "'')))"
            ),
            name="bin_roller_scan_arrival_release_check",
        ),
        sa.CheckConstraint(
            (
                "(round_id IS NULL AND return_state IS NULL AND removed IS NULL AND "
                "first_scan4_arrival_id IS NULL) OR (round_id IS NOT NULL AND ((point = "
                "'SCAN3' AND bin_code IS NOT NULL AND return_state IS NOT NULL AND removed "
                "IS NOT NULL) OR (point = 'SCAN4' AND return_state IS NULL AND removed IS "
                "NULL AND first_scan4_arrival_id IS NULL)))"
            ),
            name="bin_roller_scan_arrival_round_check",
        ),
        sa.PrimaryKeyConstraint("id", name="bin_roller_scan_arrival_pkey"),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "business_key",
            name="bin_roller_scan_arrival_owner_domain_key",
        ),
    )
    sa.Index(
        "bin_roller_point_round",
        tables["scan_arrival"].c["owner_domain"],
        tables["scan_arrival"].c["work_line_id"],
        tables["scan_arrival"].c["round_id"],
        tables["scan_arrival"].c["point"],
        unique=False,
    )
    sa.Index(
        "bin_roller_release_record",
        tables["scan_arrival"].c["release_record_id"],
        unique=True,
        sqlite_where=sa.text("release_record_id IS NOT NULL"),
        postgresql_where=sa.text("release_record_id IS NOT NULL"),
    )
    sa.Index(
        "bin_roller_release_work",
        tables["scan_arrival"].c["owner_domain"],
        tables["scan_arrival"].c["work_line_id"],
        tables["scan_arrival"].c["release_state"],
        tables["scan_arrival"].c["release_record_id"],
        unique=False,
    )
    sa.Index(
        "bin_roller_round_root",
        tables["scan_arrival"].c["round_id"],
        unique=True,
        sqlite_where=sa.text("point = 'SCAN3' AND round_id IS NOT NULL"),
        postgresql_where=sa.text("point = 'SCAN3' AND round_id IS NOT NULL"),
    )
    tables["position_fact"] = sa.Table(
        "bin_roller_position_fact",
        metadata,
        sa.Column("object_kind", sa.String(), nullable=False),
        sa.Column("object_code", sa.String(), nullable=False),
        sa.Column("location_code", sa.String(), nullable=False),
        sa.Column("rack_face", sa.String(), nullable=True),
        sa.Column("source_kind", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="bin_roller_position_fact_pkey"),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "source_kind",
            "source_id",
            "source_revision",
            name="bin_roller_position_fact_owner_domain_key",
        ),
    )
    sa.Index(
        "bin_roller_current_object",
        tables["position_fact"].c["owner_domain"],
        tables["position_fact"].c["work_line_id"],
        tables["position_fact"].c["object_kind"],
        tables["position_fact"].c["object_code"],
        unique=True,
        sqlite_where=sa.text("is_current"),
        postgresql_where=sa.text("is_current"),
    )
    sa.Index(
        "bin_roller_current_position",
        tables["position_fact"].c["owner_domain"],
        tables["position_fact"].c["work_line_id"],
        tables["position_fact"].c["object_kind"],
        tables["position_fact"].c["location_code"],
        unique=True,
        sqlite_where=sa.text("is_current"),
        postgresql_where=sa.text("is_current"),
    )
    tables["face_work"] = sa.Table(
        "bin_roller_face_work",
        metadata,
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("demand_id", sa.String(), nullable=True),
        sa.Column("service_id", sa.String(), nullable=True),
        sa.Column("feed_id", sa.String(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("change_id", sa.String(), nullable=True),
        sa.Column("plan_revision", sa.Integer(), nullable=True),
        sa.Column("rack_id", sa.String(), nullable=False),
        sa.Column("rack_face", sa.String(), nullable=False),
        sa.Column("cycle_id", sa.String(), nullable=True),
        sa.Column("preparation_action_id", sa.String(), nullable=True),
        sa.Column("source_ref", sa.String(), nullable=True),
        sa.Column("demand_state", sa.String(), nullable=True),
        sa.Column("service_state", sa.String(), nullable=True),
        sa.Column("feed_state", sa.String(), nullable=True),
        sa.Column("caller_workline_id", sa.String(), nullable=True),
        sa.Column("station_id", sa.String(), nullable=True),
        sa.Column("target", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("operation_id", sa.String(), nullable=True),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("manifest", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            (
                "(feed_id IS NULL AND feed_state IS NULL AND caller_workline_id IS NULL AND "
                "station_id IS NULL AND target IS NULL AND operation_id IS NULL AND "
                "message_id IS NULL AND manifest IS NULL) OR (feed_id IS NOT NULL AND "
                "service_id IS NOT NULL AND operation_id IS NOT NULL AND caller_workline_id "
                "IS NOT NULL AND target IS NOT NULL AND feed_state IS NOT NULL AND "
                "feed_state IN ('REQUESTED','ALLOCATED','REVIEW','COMPLETED'))"
            ),
            name="bin_roller_face_work_feed_check",
        ),
        sa.CheckConstraint(
            (
                "(purpose = 'SUPPLY' AND demand_id IS NOT NULL AND id = demand_id AND "
                "task_id IS NOT NULL AND plan_revision IS NOT NULL AND change_id IS NOT NULL "
                "AND demand_state IS NOT NULL AND demand_state IN ('ACTIVE','WITHDRAWN')) OR "
                "(purpose = 'DRAIN' AND service_id IS NOT NULL AND id = service_id AND "
                "demand_id IS NULL AND demand_state IS NULL AND change_id IS NULL AND "
                "feed_id IS NULL)"
            ),
            name="bin_roller_face_work_purpose_check",
        ),
        sa.CheckConstraint(
            (
                "(service_id IS NULL AND source_ref IS NULL AND service_state IS NULL) OR "
                "(service_id IS NOT NULL AND cycle_id IS NOT NULL AND source_ref IS NOT NULL "
                "AND (purpose != 'SUPPLY' OR source_ref = demand_id) AND service_state IS "
                "NOT NULL AND service_state IN ('PENDING','READY','COMPLETED','WITHDRAWN') "
                "AND (service_state != 'WITHDRAWN' OR (feed_id IS NULL AND (purpose = "
                "'DRAIN' OR demand_state = 'WITHDRAWN'))))"
            ),
            name="bin_roller_face_work_service_check",
        ),
        sa.PrimaryKeyConstraint("purpose", "id", name="bin_roller_face_work_pkey"),
    )
    sa.Index(
        "bin_roller_cycle_service",
        tables["face_work"].c["owner_domain"],
        tables["face_work"].c["work_line_id"],
        tables["face_work"].c["cycle_id"],
        tables["face_work"].c["source_ref"],
        unique=True,
        sqlite_where=sa.text("service_id IS NOT NULL"),
        postgresql_where=sa.text("service_id IS NOT NULL"),
    )
    sa.Index(
        "bin_roller_demand_id",
        tables["face_work"].c["demand_id"],
        unique=True,
        sqlite_where=sa.text("demand_id IS NOT NULL"),
        postgresql_where=sa.text("demand_id IS NOT NULL"),
    )
    sa.Index(
        "bin_roller_feed_id",
        tables["face_work"].c["feed_id"],
        unique=True,
        sqlite_where=sa.text("feed_id IS NOT NULL"),
        postgresql_where=sa.text("feed_id IS NOT NULL"),
    )
    sa.Index(
        "bin_roller_service_id",
        tables["face_work"].c["service_id"],
        unique=True,
        sqlite_where=sa.text("service_id IS NOT NULL"),
        postgresql_where=sa.text("service_id IS NOT NULL"),
    )
    sa.Index(
        "bin_roller_supply_face",
        tables["face_work"].c["owner_domain"],
        tables["face_work"].c["work_line_id"],
        tables["face_work"].c["task_id"],
        tables["face_work"].c["plan_revision"],
        tables["face_work"].c["rack_id"],
        tables["face_work"].c["rack_face"],
        unique=True,
        sqlite_where=sa.text("purpose = 'SUPPLY'"),
        postgresql_where=sa.text("purpose = 'SUPPLY'"),
    )
    tables["feed_segment"] = sa.Table(
        "bin_roller_feed_segment",
        metadata,
        sa.Column("feed_id", sa.String(), nullable=False),
        sa.Column("segment_index", sa.Integer(), nullable=False),
        sa.Column("members", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("client_request_id", sa.String(), nullable=False),
        sa.Column("transport_task_id", sa.String(), nullable=True),
        sa.Column("operation_id", sa.String(), nullable=True),
        sa.Column("consumed_revision", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="bin_roller_feed_segment_pkey"),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "feed_id",
            "segment_index",
            name="bin_roller_feed_segment_owner_domain_key",
        ),
    )
    tables["return_opportunity"] = sa.Table(
        "bin_roller_return_opportunity",
        metadata,
        sa.Column("service_id", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("ended", sa.Boolean(), nullable=False),
        sa.Column("end_reason", sa.String(), nullable=True),
        sa.Column("end_evidence", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="bin_roller_return_opportunity_pkey"),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "service_id",
            "source_ref",
            name="bin_roller_return_opportunity_owner_domain_key",
        ),
    )
    tables["return_batch"] = sa.Table(
        "bin_roller_return_batch",
        metadata,
        sa.Column("opportunity_id", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("members", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("manifest", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("workline_code", sa.String(), nullable=False),
        sa.Column("caller_workline_id", sa.String(), nullable=False),
        sa.Column("station_id", sa.String(), nullable=True),
        sa.Column("source", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("transport_operation_id", sa.String(), nullable=True),
        sa.Column("consumed_revision", sa.BigInteger(), nullable=True),
        sa.Column("client_request_id", sa.String(), nullable=True),
        sa.Column("transport_task_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="bin_roller_return_batch_pkey"),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "opportunity_id",
            "source_ref",
            name="bin_roller_return_batch_owner_domain_key",
        ),
    )
    tables["decision"] = sa.Table(
        "bin_roller_decision",
        metadata,
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("input_kind", sa.String(), nullable=True),
        sa.Column("input_ref", sa.String(), nullable=True),
        sa.Column("input_snapshot", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("request", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("received_work", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("unused_racks", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("decision", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("due_at", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            (
                "kind = 'DRAIN' AND input_kind IS NOT NULL AND input_ref IS NOT NULL AND "
                "unused_racks IS NOT NULL"
            ),
            name="bin_roller_decision_subject_check",
        ),
        sa.PrimaryKeyConstraint("kind", "id", name="bin_roller_decision_pkey"),
        sa.UniqueConstraint("kind", "operation_id", name="bin_roller_decision_kind_key"),
    )
    sa.Index(
        "bin_roller_drain_input",
        tables["decision"].c["owner_domain"],
        tables["decision"].c["work_line_id"],
        tables["decision"].c["input_kind"],
        tables["decision"].c["input_ref"],
        unique=True,
        sqlite_where=sa.text("kind = 'DRAIN'"),
        postgresql_where=sa.text("kind = 'DRAIN'"),
    )
    return metadata, tables


def upgrade():
    bind = op.get_bind()
    schema = _schema(bind)
    _guard_old(bind)
    old_metadata, _ = _old_schema(schema)
    old_metadata.drop_all(bind, checkfirst=False)
    metadata, _ = _new_schema(schema)
    metadata.create_all(bind, checkfirst=False)


def downgrade():
    bind = op.get_bind()
    schema = _schema(bind)
    metadata, tables = _new_schema(schema)
    for table in tables.values():
        _reject_if(bind, table, sa.true())
    metadata.drop_all(bind, checkfirst=False)
    old_metadata, _ = _old_schema(schema)
    old_metadata.create_all(bind, checkfirst=False)
