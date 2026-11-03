"""Cut over original rack lifecycles in one offline-writer transaction."""

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import sqlalchemy as sa
from alembic import op

revision = "sharedrackdispatch"
down_revision = "racksubmitack"
branch_labels = None
depends_on = None


def _create_shared(schema):
    prefix = f"{schema}." if schema else ""
    op.create_table(
        "rack_dispatch_job",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("rack_id", sa.String(), nullable=False),
        sa.Column("original_cycle_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("workline_code", sa.String(), nullable=True),
        sa.Column("point", sa.String(), nullable=True),
        sa.Column("initial_face", sa.String(), nullable=True),
        sa.Column("caller", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("resources", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("current_action_id", sa.String(), nullable=True),
        sa.Column("release_requested", sa.Boolean(), nullable=False),
        sa.Column("release_basis", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("facts_revision", sa.BigInteger(), nullable=False),
        sa.Column("review_reason", sa.String(), nullable=True),
        sa.Column("actual_position", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("actual_face", sa.String(), nullable=True),
        sa.Column("position_action_id", sa.String(), nullable=True),
        sa.Column("closure_reason", sa.String(), nullable=True),
        sa.Column("closure_basis", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("completed_at_ms", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "(state IN ('ACTIVE','RELEASING') AND pending AND closure_reason IS NULL AND "
            "closure_basis IS NULL) OR (NOT pending AND closure_reason IS NOT NULL AND "
            "closure_basis IS NOT NULL AND ((state='COMPLETED' AND "
            "(closure_reason='LEGACY_CLOSED' OR (closure_reason='DEPARTURE_ACK' AND "
            "completed_at_ms IS NOT NULL))) OR (state IN ('WITHDRAWN','NOT_ARRIVED') AND "
            "closure_reason=state)))",
            name=op.f("rack_dispatch_job_closure_check"),
        ),
        sa.CheckConstraint(
            "source_kind IN ('MANUAL_RETURN','MANUAL_TRANSFER','ROLLER_ORDINARY','ROLLER_DRAIN')",
            name=op.f("rack_dispatch_job_source_kind_check"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("rack_dispatch_job_pkey")),
        sa.UniqueConstraint("id", "owner_domain", "work_line_id", name="rack_dispatch_job_scope"),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "source_kind",
            "source_ref",
            "rack_id",
            name=op.f("rack_dispatch_job_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_index(
        "rack_dispatch_job_source_work",
        "rack_dispatch_job",
        ["owner_domain", "work_line_id", "source_kind", "pending", "id"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "rack_dispatch_job_work",
        "rack_dispatch_job",
        ["owner_domain", "work_line_id", "pending", "id"],
        unique=False,
        schema=schema,
    )
    op.create_table(
        "rack_dispatch_action",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("completion_rule", sa.String(), nullable=True),
        sa.Column("previous_action_id", sa.String(), nullable=True),
        sa.Column("previous_decision_phase", sa.String(), nullable=True),
        sa.Column("caller", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("template", sa.String(), nullable=True),
        sa.Column("source", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("target", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("target_face", sa.String(), nullable=True),
        sa.Column("client_request_id", sa.String(), nullable=True),
        sa.Column("transport_task_id", sa.String(), nullable=True),
        sa.Column("submit_operation_id", sa.String(), nullable=True),
        sa.Column("submit_message_id", sa.BigInteger(), nullable=True),
        sa.Column("submit_target", sa.String(), nullable=True),
        sa.Column("outcome", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("outcome_revision", sa.BigInteger(), nullable=True),
        sa.Column("position_observation", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("observation_revision", sa.BigInteger(), nullable=True),
        sa.Column("actual_position", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("actual_face", sa.String(), nullable=True),
        sa.Column("position_revision", sa.BigInteger(), nullable=True),
        sa.Column("pending_revision", sa.BigInteger(), nullable=True),
        sa.Column("acceptance_basis", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("accepted_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("operation", sa.String(), nullable=True),
        sa.Column("decision_operation_id", sa.String(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("request", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("decision", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("decision_consumed_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("due_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("basis", sa.JSON(none_as_null=True), nullable=True),
        sa.CheckConstraint(
            "(kind='DEPARTURE_DECIDE' AND ((previous_action_id IS NULL AND "
            "previous_decision_phase IS NULL) OR (previous_action_id IS NOT NULL AND "
            "previous_decision_phase IS NOT NULL AND previous_decision_phase='WAIT'))) OR "
            "(kind='DEPART' AND ((phase='LEGACY_CLOSED' AND previous_decision_phase IS NULL) "
            "OR (previous_action_id IS NOT NULL AND previous_decision_phase IS NOT NULL AND "
            "previous_decision_phase='READY'))) OR (kind IN ('ENTER','ROTATE') AND "
            "previous_decision_phase IS NULL)",
            name=op.f("rack_dispatch_action_predecessor_check"),
        ),
        sa.CheckConstraint(
            "(kind='DEPARTURE_DECIDE' AND completion_rule IS NULL AND phase IN "
            "('REGISTERED','REVIEW','READY','WAIT')) OR (kind IN ('ENTER','ROTATE') AND "
            "completion_rule IS NOT NULL AND completion_rule='APPLIED_RESULT' AND phase IN "
            "('REGISTERED','APPLIED','REVIEW')) OR (kind='DEPART' AND completion_rule IS NOT "
            "NULL AND ((completion_rule='ACCEPTED_DEPARTURE' AND phase IN "
            "('REGISTERED','ACCEPTED','REVIEW')) OR "
            "(completion_rule='LEGACY_RECORDED_CLOSURE' AND phase='LEGACY_CLOSED' AND basis "
            "IS NOT NULL)))",
            name=op.f("rack_dispatch_action_rule_phase_check"),
        ),
        sa.CheckConstraint(
            "phase!='ACCEPTED' OR (acceptance_basis IS NOT NULL AND accepted_at_ms IS NOT NULL)",
            name=op.f("rack_dispatch_action_departure_acceptance_check"),
        ),
        sa.CheckConstraint(
            "phase!='APPLIED' OR (outcome_revision IS NOT NULL AND outcome IS NOT NULL)",
            name=op.f("rack_dispatch_action_applied_result_check"),
        ),
        sa.CheckConstraint(
            "phase!='READY' OR COALESCE(decision ->> 'result', '')='READY'",
            name=op.f("rack_dispatch_action_ready_decision_check"),
        ),
        sa.CheckConstraint(
            "phase!='WAIT' OR (COALESCE(decision ->> 'result', '')='WAIT' AND due_at_ms IS "
            "NOT NULL)",
            name=op.f("rack_dispatch_action_wait_decision_check"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "owner_domain", "work_line_id"],
            [
                f"{prefix}rack_dispatch_job.id",
                f"{prefix}rack_dispatch_job.owner_domain",
                f"{prefix}rack_dispatch_job.work_line_id",
            ],
            name=op.f("rack_dispatch_action_job_id_fkey"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "previous_action_id", "previous_decision_phase"],
            [
                f"{prefix}rack_dispatch_action.job_id",
                f"{prefix}rack_dispatch_action.id",
                f"{prefix}rack_dispatch_action.phase",
            ],
            name="rack_dispatch_action_decision_predecessor",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "previous_action_id"],
            [f"{prefix}rack_dispatch_action.job_id", f"{prefix}rack_dispatch_action.id"],
            name="rack_dispatch_action_predecessor",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("rack_dispatch_action_pkey")),
        sa.UniqueConstraint(
            "client_request_id", name=op.f("rack_dispatch_action_client_request_id_key")
        ),
        sa.UniqueConstraint("job_id", "id", "phase", name="rack_dispatch_action_decision_ref"),
        sa.UniqueConstraint("job_id", "id", name="rack_dispatch_action_ref"),
        sa.UniqueConstraint("job_id", "kind", "source_ref", name="rack_dispatch_action_origin"),
        sa.UniqueConstraint("job_id", "ordinal", name="rack_dispatch_action_order"),
        sa.UniqueConstraint(
            "operation", "decision_operation_id", name=op.f("rack_dispatch_action_operation_key")
        ),
        sa.UniqueConstraint(
            "submit_operation_id", name=op.f("rack_dispatch_action_submit_operation_id_key")
        ),
        sa.UniqueConstraint(
            "transport_task_id", name=op.f("rack_dispatch_action_transport_task_id_key")
        ),
        schema=schema,
    )
    op.create_index(
        "rack_dispatch_action_root",
        "rack_dispatch_action",
        ["job_id"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text("kind='DEPARTURE_DECIDE' AND previous_action_id IS NULL"),
        postgresql_where=sa.text("kind='DEPARTURE_DECIDE' AND previous_action_id IS NULL"),
    )
    op.create_index(
        "rack_dispatch_action_single_move",
        "rack_dispatch_action",
        ["job_id", "kind"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text("kind IN ('ENTER','DEPART')"),
        postgresql_where=sa.text("kind IN ('ENTER','DEPART')"),
    )
    op.create_index(
        "rack_dispatch_action_wait_child",
        "rack_dispatch_action",
        ["job_id", "previous_action_id"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text("kind='DEPARTURE_DECIDE' AND previous_action_id IS NOT NULL"),
        postgresql_where=sa.text("kind='DEPARTURE_DECIDE' AND previous_action_id IS NOT NULL"),
    )


def _restore_private(schema):
    op.create_table(
        "bin_roller_rack_cycle",
        sa.Column("rack_id", sa.String(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("resources", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("release_basis", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_rack_cycle_pkey")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "source_ref",
            "rack_id",
            name=op.f("bin_roller_rack_cycle_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "bin_roller_rack_action",
        sa.Column("cycle_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("client_request_id", sa.String(), nullable=False),
        sa.Column("transport_task_id", sa.String(), nullable=True),
        sa.Column("operation_id", sa.String(), nullable=True),
        sa.Column("station_id", sa.String(), nullable=True),
        sa.Column("caller_workline_id", sa.String(), nullable=False),
        sa.Column("target", sa.JSON(), nullable=False),
        sa.Column("target_face", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("consumed_revision", sa.BigInteger(), nullable=True),
        sa.Column("position_revision", sa.BigInteger(), nullable=True),
        sa.Column("acceptance_basis", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_rack_action_pkey")),
        sa.UniqueConstraint(
            "client_request_id", name=op.f("bin_roller_rack_action_client_request_id_key")
        ),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "cycle_id",
            "kind",
            "source_ref",
            name=op.f("bin_roller_rack_action_owner_domain_key"),
        ),
        sa.UniqueConstraint(
            "transport_task_id", name=op.f("bin_roller_rack_action_transport_task_id_key")
        ),
        schema=schema,
    )
    op.create_index(
        "bin_roller_cycle_entry_departure",
        "bin_roller_rack_action",
        ["owner_domain", "work_line_id", "cycle_id", "kind"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text("kind IN ('CTU01', 'CTU03')"),
        postgresql_where=sa.text("kind IN ('CTU01', 'CTU03')"),
    )
    op.create_table(
        "smt_manual_rack",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("workline_code", sa.String(), nullable=False),
        sa.Column("rack_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("location_code", sa.String(), nullable=False),
        sa.Column("initial_face", sa.String(), nullable=False),
        sa.Column("caller_workline_id", sa.String(), nullable=False),
        sa.Column("station_id", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("smt_manual_rack_pkey")),
        sa.Column("departure_operation_id", sa.String()),
        schema=schema,
    )
    op.create_table(
        "smt_manual_rack_action",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("rack_work_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("client_request_id", sa.String(), nullable=False),
        sa.Column("transport_task_id", sa.String(), nullable=False),
        sa.Column("target", sa.JSON(), nullable=False),
        sa.Column("target_face", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("outcome_revision", sa.BigInteger(), nullable=True),
        sa.Column("observation_revision", sa.BigInteger(), nullable=True),
        sa.Column("actual_position", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("actual_face", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("smt_manual_rack_action_pkey")),
        schema=schema,
    )


OLD_CHECKS = {
    "smt_manual_request_smt_manual_request_snapshot_check": (
        "kind != 'RACK_DEPARTURE' OR request_snapshot IS NOT NULL"
    ),
    "smt_manual_request_smt_manual_request_basis_check": (
        "(kind IN ('COMPLETION_CONFIRM','RACK_DEPARTURE') AND basis IS NOT NULL) OR (kind NOT "
        "IN ('COMPLETION_CONFIRM','RACK_DEPARTURE') AND basis IS NULL)"
    ),
    "smt_manual_request_smt_manual_request_subject_check": (
        "(kind = 'BIN_ADMISSION' AND arrival_id IS NOT NULL AND rack_work_id IS NULL) OR "
        "(kind IN ('RACK_ARRIVAL_REPORT','RACK_DEPARTURE') AND rack_work_id IS NOT NULL AND "
        "arrival_id IS NULL) OR (kind IN ('PREPARE','COMPLETION_CONFIRM') AND rack_work_id IS "
        "NULL AND arrival_id IS NULL)"
    ),
    "smt_manual_request_smt_manual_request_kind_check": (
        "(kind = 'PREPARE' AND operation = 'sorting.task.prepare@v1') OR (kind = "
        "'COMPLETION_CONFIRM' AND operation = 'sorting.task.completion_confirm@v1') OR (kind "
        "= 'BIN_ADMISSION' AND operation = 'manual_bin.work_admission_decide@v1') OR (kind = "
        "'RACK_ARRIVAL_REPORT' AND operation = 'outbound.return_rack.arrival_report@v1') OR "
        "(kind = 'RACK_DEPARTURE' AND operation = 'rack.departure_decide@v1')"
    ),
    "bin_roller_decision_subject_check": (
        "(kind = 'DRAIN' AND input_kind IS NOT NULL AND input_ref IS NOT NULL AND "
        "unused_racks IS NOT NULL AND cycle_id IS NULL AND source_ref IS NULL) OR (kind = "
        "'DEPARTURE' AND input_kind IS NULL AND input_ref IS NULL AND input_snapshot IS NULL "
        "AND unused_racks IS NULL AND cycle_id IS NOT NULL AND source_ref IS NOT NULL AND "
        "request IS NOT NULL)"
    ),
}
NEW_CHECKS = {
    "smt_manual_request_smt_manual_request_kind_check": (
        "(kind = 'PREPARE' AND operation = 'sorting.task.prepare@v1') OR (kind = "
        "'COMPLETION_CONFIRM' AND operation = 'sorting.task.completion_confirm@v1') OR (kind "
        "= 'BIN_ADMISSION' AND operation = 'manual_bin.work_admission_decide@v1') OR (kind = "
        "'RACK_ARRIVAL_REPORT' AND operation = 'outbound.return_rack.arrival_report@v1')"
    ),
    "smt_manual_request_smt_manual_request_subject_check": (
        "(kind = 'BIN_ADMISSION' AND arrival_id IS NOT NULL AND rack_work_id IS NULL) OR "
        "(kind = 'RACK_ARRIVAL_REPORT' AND rack_work_id IS NOT NULL AND arrival_id IS NULL) "
        "OR (kind IN ('PREPARE','COMPLETION_CONFIRM') AND rack_work_id IS NULL AND arrival_id "
        "IS NULL)"
    ),
    "smt_manual_request_smt_manual_request_basis_check": (
        "(kind = 'COMPLETION_CONFIRM' AND basis IS NOT NULL) OR (kind != 'COMPLETION_CONFIRM' "
        "AND basis IS NULL)"
    ),
    "bin_roller_decision_subject_check": (
        "kind = 'DRAIN' AND input_kind IS NOT NULL AND input_ref IS NOT NULL AND unused_racks "
        "IS NOT NULL"
    ),
}

PRIVATE = (
    "smt_manual_rack",
    "smt_manual_rack_action",
    "bin_roller_rack_cycle",
    "bin_roller_rack_action",
)
NEW_COLUMNS = {
    "bin_roller_scan_arrival": {"received_work": sa.JSON},
    "bin_roller_decision": {"received_work": sa.JSON},
    "bin_roller_face_work": {"preparation_action_id": sa.String},
    "smt_manual_task_member": {
        "preparation_action_id": sa.String,
        "arrival_report_operation_id": sa.String,
    },
    "smt_manual_input": {"target_job_id": sa.String, "target_action_id": sa.String},
    "smt_manual_bin": {"target_job_id": sa.String, "target_action_id": sa.String},
}
DECIDE = "rack.departure_decide@v1"
REPORT = "outbound.return_rack.arrival_report@v1"
SUBMIT = "transport.task.submit@v1"


def _require(ok, reason, identity=None):
    if not ok:
        # A bounded fingerprint identifies the offending row without logging its payload.
        tag = sha256(str(identity).encode()).hexdigest()[:12]
        raise RuntimeError(f"shared rack cutover: {reason} [{tag}]")


def _schema(bind):
    return "enterprise_backend" if bind.dialect.name == "postgresql" else None


def _table(bind, name):
    t = sa.Table(name, sa.MetaData(), schema=_schema(bind), autoload_with=bind)
    for c in t.c:
        if isinstance(c.type, sa.JSON):
            c.type.none_as_null = True
    return t


def _rows(bind, name):
    return list(bind.execute(sa.select(_table(bind, name))).mappings())


def _unique(rows, key, reason):
    out = {}
    for row in rows:
        i = row[key]
        _require(i is not None and i not in out, reason, i)
        out[i] = row
    return out


def _scope(a, b):
    return (
        a and b and (a["owner_domain"], a["work_line_id"]) == (b["owner_domain"], b["work_line_id"])
    )


def _point(location):
    return {"kind": "RACK_POSITION", "location_code": location}


def _place(value):
    return {"kind": value.get("kind", value.get("type")), "location_code": value["location_code"]}


def _wire(messages, op_name, op_id, msg_id, data, keys, protocol):
    candidates = messages.get((op_name, op_id), ())
    if msg_id is not None and msg_id in messages:
        _require(messages[msg_id] in candidates, "original message identity", msg_id)
    if not candidates:
        return None
    _require(len(candidates) == 1, "ambiguous original message", op_id)
    m = candidates[0]
    _require(
        (msg_id is None or m["id"] == msg_id)
        and m["business_key"] in keys
        and m["target"]
        and (m["delivery_snapshot"] or {}).get("protocol", protocol) == protocol,
        "original message association",
        op_id,
    )
    try:
        wire = json.loads(m["request_raw"])
    except (ValueError, UnicodeError):
        wire = None
    _require(
        isinstance(wire, dict)
        and set(wire) == {"operation", "operation_id", "timestamp", "data"}
        and (wire["operation"], wire["operation_id"], wire["data"]) == (op_name, op_id, data)
        and type(wire["timestamp"]) is int
        and wire["timestamp"] >= 0,
        "original wire request",
        op_id,
    )
    return m


def _group(rows, key):
    grouped = defaultdict(list)
    for r in rows:
        grouped[r[key]].append(r)
    return grouped


def _base_job(row, manual, source_ref, initial_face, resources):
    i = row["id"]
    caller = None
    if manual or resources:
        r = row if manual else resources
        caller = {"workline_id": r["caller_workline_id"], "station_id": r["station_id"]}
    return dict(
        id=i,
        original_cycle_id=i,
        task_id=row["task_id"],
        rack_id=row["rack_id"],
        work_line_id=row["work_line_id"],
        owner_domain="smt_manual" if manual else row["owner_domain"],
        source_kind=("MANUAL_" + row["role"]) if manual else ("ROLLER_" + row["purpose"]),
        source_ref=source_ref,
        initial_face=initial_face,
        caller=caller,
        resources=resources,
        workline_code=row["workline_code"] if manual else (resources or {}).get("workline_code"),
        point=row["location_code"]
        if manual
        else (resources or {}).get("positions", {}).get("FIVE_RACK"),
        state="ACTIVE",
        pending=True,
        release_requested=False,
        facts_revision=0,
        release_basis={
            "legacy_table": PRIVATE[0 if manual else 2],
            "legacy_id": i,
            "legacy_state": row["state" if manual else "status"],
            "legacy_pending": row["pending"],
            "legacy_release_basis": row.get("release_basis"),
        },
    )


def _move(job, r, manual, tasks, messages, positions):
    i = r["id"]
    kind = (
        r["kind"]
        if manual
        else {"CTU01": "ENTER", "CTU02": "ROTATE", "CTU03": "DEPART"}.get(r["kind"])
    )
    _require(kind in ("ENTER", "ROTATE", "DEPART"), "unknown movement", i)
    caller = (
        job["caller"]
        if manual
        else dict(workline_id=r["caller_workline_id"], station_id=r["station_id"])
    )
    target = _place(r["target"])
    source = (
        {"kind": "RACK", "location_code": job["rack_id"]}
        if kind == "ENTER"
        else _point(job["point"])
    )
    template = (
        {"ENTER": "F01", "ROTATE": "CK04", "DEPART": "F01"}
        if manual
        else {"ENTER": "CTU01", "ROTATE": "CTU02", "DEPART": "CTU03"}
    )[kind]
    state = r["state" if manual else "status"]
    _require(
        caller
        and caller == job["caller"]
        and job["point"]
        and (
            target == _point(job["point"])
            if kind != "DEPART"
            else target != source and target["kind"] in ("ZONE", "RACK_POSITION")
        ),
        "movement resource/target",
        i,
    )
    a = dict(
        id=i,
        job_id=job["id"],
        owner_domain=job["owner_domain"],
        work_line_id=job["work_line_id"],
        kind=kind,
        source_ref=r["source_ref"],
        phase="REGISTERED",
        caller=caller,
        source=source,
        target=target,
        target_face=r["target_face"],
        template=template,
        client_request_id=r["client_request_id"],
        transport_task_id=r["transport_task_id"],
        completion_rule="ACCEPTED_DEPARTURE" if kind == "DEPART" else "APPLIED_RESULT",
        basis={
            "legacy_table": PRIVATE[1 if manual else 3],
            "legacy_id": i,
            "legacy_state": state,
            "legacy_pending": r.get("pending"),
            "legacy_acceptance_basis": r.get("acceptance_basis"),
        },
    )
    t = tasks.get(a["client_request_id"])
    if t is None and a["transport_task_id"] is None:
        _require(
            not manual
            and r["operation_id"] is None
            and r["consumed_revision"] is None
            and r["position_revision"] is None
            and state == "PENDING"
            and r["pending"],
            "unbound movement",
            i,
        )
        return a
    _require(
        t
        and (
            t["transport_task_id"] == a["transport_task_id"]
            or not manual
            and a["transport_task_id"] is None
        )
        and t["owner_domain"] == job["owner_domain"]
        and t["cycle_id"] == job["id"]
        and t["workline_id"] == caller["workline_id"]
        and t["station_id"] == caller["station_id"]
        and (
            manual
            or r["operation_id"] == t["operation_id"]
            or r["operation_id"] is None
            and not a["transport_task_id"]
        ),
        "transport owner or identity",
        i,
    )
    a["transport_task_id"] = t["transport_task_id"]
    f = t["frozen"]
    k = "RACK_ROTATE" if kind == "ROTATE" else "RACK_MOVE"
    _require(
        isinstance(f, dict)
        and t["kind"] == f.get("kind") == k
        and f.get("rack_id") == job["rack_id"]
        and (f.get("source"), f.get("target"), f.get("target_face"), f.get("rcs_template_id"))
        == (source, target, a["target_face"], template),
        "frozen movement",
        i,
    )
    m = _wire(
        messages,
        SUBMIT,
        t["operation_id"],
        None,
        {"transport_task_id": t["transport_task_id"], **f},
        (t["transport_task_id"],),
        "wms-transport",
    )
    frozen_args = dict(
        method="rotate_rack" if kind == "ROTATE" else "move_rack", caller=caller, data=f
    )
    digest = sha256(
        json.dumps(frozen_args, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    _require(digest == t["input_digest"], "frozen transport digest", i)
    timestamp = (
        t["created_at"].replace(tzinfo=UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    ) // timedelta(milliseconds=1)
    raw = json.dumps(
        dict(
            operation_id=t["operation_id"],
            operation=SUBMIT,
            timestamp=timestamp,
            data={"transport_task_id": t["transport_task_id"], **f},
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    _require(
        m is None or (m["request_raw"] == raw and m["delivery_snapshot"] is None),
        "frozen submit bytes",
        i,
    )
    saved = t["submission_acceptance"]
    if saved is not None:
        _require(saved["request_raw"].encode() == raw, "original retained submit bytes", i)
        snapshot = dict(saved)
        digest = snapshot.pop("evidence_sha256")
        _require(
            sha256(
                json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                    "ascii"
                )
            ).hexdigest()
            == digest
            and sha256(saved["request_raw"].encode()).hexdigest() == saved["request_sha256"]
            and json.loads(saved["request_raw"])
            == {
                "operation": SUBMIT,
                "operation_id": t["operation_id"],
                "timestamp": saved["request_timestamp"],
                "data": {"transport_task_id": t["transport_task_id"], **f},
            }
            and (saved["http_status"], saved["response_code"])
            in ((202, "RECEIVED"), (200, "DUPLICATE"))
            and saved["business_key"] == t["transport_task_id"],
            "retained original ACK",
            i,
        )
        _require(
            saved["operation_id"] == t["operation_id"]
            and saved["transport_task_id"] == t["transport_task_id"]
            and saved["client_request_id"] == a["client_request_id"]
            and saved["owner_domain"] == job["owner_domain"]
            and saved["cycle_id"] == job["id"]
            and saved["caller"] == caller
            and (
                not m
                or (m["id"], m["target"], m["request_raw"].decode())
                == (saved["message_id"], saved["target"], saved["request_raw"])
            ),
            "retained submit evidence",
            i,
        )
    a.update(
        submit_operation_id=t["operation_id"],
        submit_message_id=m["id"] if m else (saved or {}).get("message_id"),
        submit_target=m["target"] if m else (saved or {}).get("target"),
    )
    legacy = r.get("acceptance_basis")
    if legacy:
        _require(
            (legacy["transport_task_id"], legacy["client_request_id"], legacy["operation_id"])
            == (a["transport_task_id"], a["client_request_id"], a["submit_operation_id"]),
            "legacy release identity",
            i,
        )
        for src, dst in (
            ("submission_message_id", "submit_message_id"),
            ("submission_target", "submit_target"),
        ):
            if legacy.get(src) is not None:
                _require(a[dst] in (None, legacy[src]), "conflicting original submission", i)
                a[dst] = legacy[src]
    rev = r["outcome_revision" if manual else "consumed_revision"]
    obs = t["position_observation"]
    orev = r.get("observation_revision")
    prev = r.get("position_revision")
    if not manual and obs and prev == obs["outcome_revision"]:
        orev = prev
    outcome = t["applied_outcome"]
    if rev is not None:
        _require(
            outcome
            and t["applied_outcome_revision"] == rev == outcome.get("outcome_revision")
            and (outcome.get("rack_id"), outcome.get("transport_task_id"), outcome.get("kind"))
            == (job["rack_id"], a["transport_task_id"], k),
            "unavailable consumed outcome revision",
            i,
        )
        a.update(outcome=outcome, outcome_revision=rev)
        _require(
            state not in ("SUCCEEDED", "CANCELLED") or state == outcome.get("status"),
            "consumed status mismatch",
            i,
        )
    if orev is not None:
        _require(
            obs
            and orev == obs.get("outcome_revision")
            and (obs.get("rack_id"), obs.get("transport_task_id"), obs.get("kind"))
            == (job["rack_id"], a["transport_task_id"], k),
            "unavailable observed revision",
            i,
        )
        a.update(position_observation=obs, observation_revision=orev)
    if manual:
        a.update(
            actual_position=r["actual_position"],
            actual_face=r["actual_face"],
            position_revision=max((v for v in (orev, rev) if v is not None), default=None),
        )
    elif prev is not None:
        matches = [p for p in positions if p["source_revision"] == prev]
        _require(
            len(matches) == 1
            and _scope(job, matches[0])
            and matches[0]["object_code"] == job["rack_id"],
            "position revision",
            i,
        )
        a.update(
            actual_position=_point(matches[0]["location_code"]),
            actual_face=matches[0]["rack_face"],
            position_revision=prev,
        )
    a["pending_revision"] = (t["pending_outcome"] or {}).get("revision")
    if manual:
        current = (
            obs if orev is not None and orev > (rev or 0) else outcome if rev is not None else None
        )
        _require(
            current is None
            or (current["final_position"], current["arrival_face"])
            == (r["actual_position"], r["actual_face"]),
            "manual position evidence",
            i,
        )
    if kind != "DEPART":
        a["phase"] = (
            "REVIEW"
            if state in {"POSITION_CONFLICT", "UNKNOWN", "POSITION_UNKNOWN", "REVIEW"}
            or a["pending_revision"] is not None
            else "APPLIED"
            if rev is not None
            else "REGISTERED"
        )
    return a


def _decisions(job, rows, manual, services, pointer, messages):
    result = []
    children = {}
    for r in rows:
        i = r["operation_id"] if manual else r["id"]
        if manual:
            _require(
                (r["task_id"], r["work_line_id"]) == (job["task_id"], job["work_line_id"]),
                "manual decision owner",
                i,
            )
            previous = r["previous_operation_id"]
            ref = previous or job.get("position_action_id") or job["id"]
            phase = {
                "REQUESTED": "REGISTERED",
                "WAITING": "WAIT",
                "CONSUMED": "READY",
                "CONFLICT": "REVIEW",
            }[r["state"]]
        else:
            _require(_scope(r, job), "roller decision owner", i)
            ref = r["source_ref"]
            service = services.get(ref)
            root = (
                ref == job["id"]
                or service
                and _scope(job, service)
                and service["cycle_id"] == job["id"]
            )
            _require(
                not root or ref not in {row["id"] for row in rows},
                "ambiguous typed decision root",
                i,
            )
            previous = None if root else ref
            phase = {
                "PENDING": "REGISTERED",
                "WAIT": "WAIT",
                "READY": "READY",
                "REVIEW": "REVIEW",
            }.get(r["status"])
        request = r["request_snapshot" if manual else "request"]
        decision = r["response" if manual else "decision"]
        due = r["due_at_ms" if manual else "due_at"]
        _require(
            phase
            and isinstance(request, dict)
            and request.get("rack_id") == job["rack_id"]
            and request.get("workline_code") == job["workline_code"]
            and request.get("current_location")
            == {"type": "RACK_POSITION", "location_code": job["point"]},
            "decision request",
            i,
        )
        _require(
            set(request) == {"rack_id", "workline_code", "current_location", "current_face"}
            and (
                request["current_face"] is None
                or isinstance(request["current_face"], str)
                and 0 < len(request["current_face"]) <= 10
            )
            and (phase != "REGISTERED" or decision is None),
            "original decision shape",
            i,
        )
        if phase in ("WAIT", "READY"):
            _require(
                isinstance(decision, dict)
                and decision.get("result") == phase
                and (not manual or r["decision_code"] == phase)
                and (
                    due is not None
                    and type(decision.get("retry_after_ms")) is int
                    and 1 <= decision["retry_after_ms"] <= 60000
                    and set(decision) == {"result", "retry_after_ms"}
                    if phase == "WAIT"
                    else due is None
                    and set(decision) == {"result", "rack_destination"}
                    and isinstance(decision.get("rack_destination"), dict)
                    and decision["rack_destination"].get("type") in ("ZONE", "RACK_POSITION")
                    and bool(decision["rack_destination"].get("location_code"))
                    and decision["rack_destination"] != request["current_location"]
                ),
                "consumed decision",
                i,
            )
        _wire(
            messages,
            DECIDE,
            r["operation_id"],
            r["message_id"],
            request,
            ("business:" + job["owner_domain"],),
            "wms-decisions",
        )
        _require(previous not in children, "decision roots or fork", i)
        children[previous] = dict(
            id=i,
            job_id=job["id"],
            owner_domain=job["owner_domain"],
            work_line_id=job["work_line_id"],
            kind="DEPARTURE_DECIDE",
            source_ref=ref,
            phase=phase,
            previous_action_id=previous,
            previous_decision_phase="WAIT" if previous else None,
            caller=job["caller"],
            operation=DECIDE,
            decision_operation_id=r["operation_id"],
            message_id=r["message_id"],
            request=request,
            decision=decision,
            decision_consumed_at_ms=r.get("saved_at_ms"),
            due_at_ms=due,
            basis={
                **(r.get("basis") or {}),
                "legacy_table": "smt_manual_request" if manual else "bin_roller_decision",
                "legacy_id": i,
                "legacy_state": r["state" if manual else "status"],
                "legacy_basis": r.get("basis"),
                "legacy_pending": r.get("pending"),
                "legacy_decision_code": r.get("decision_code"),
            },
        )
    previous = None
    while previous in children:
        _require(not result or result[-1]["phase"] == "WAIT", "non-WAIT predecessor", previous)
        action = children.pop(previous)
        result.append(action)
        previous = action["id"]
    _require(
        not children and (not manual or pointer == previous),
        "disconnected decision chain or current pointer",
        job["id"],
    )
    return result


def _validate_roller_graph(data, cycles, transports):
    faces = data["bin_roller_face_work"]
    services = {r["service_id"]: r for r in faces if r["service_id"]}
    feeds = {r["feed_id"]: r for r in faces if r["feed_id"]}
    demands = {r["demand_id"]: r for r in faces if r["demand_id"]}
    drains = {r["id"]: r for r in data["bin_roller_decision"] if r["kind"] == "DRAIN"}
    segments = _unique(data["bin_roller_feed_segment"], "id", "feed segments")
    opportunities = _unique(data["bin_roller_return_opportunity"], "id", "opportunities")
    batches = _unique(data["bin_roller_return_batch"], "id", "return batches")
    rounds = {
        a["round_id"]: a
        for a in data["bin_roller_scan_arrival"]
        if a["point"] == "SCAN3" and a["round_id"]
    }
    for f in faces:
        if f["cycle_id"]:
            c = cycles.get(f["cycle_id"])
            _require(
                _scope(c, f)
                and c["rack_id"] == f["rack_id"]
                and c["task_id"] == f["task_id"]
                and (c["purpose"] == "ORDINARY") == (f["purpose"] == "SUPPLY"),
                "face work owner",
                f["id"],
            )
    for d in drains.values():
        ids = [x["rack_id"] for x in (d["decision"] or {}).get("racks", [])]
        unused = d["unused_racks"]
        _require(
            len(set(ids)) == len(ids)
            and len(set(unused)) == len(unused)
            and set(unused) <= set(ids),
            "drain candidates",
            d["id"],
        )
        head = d
        seen = set()
        while head["input_kind"] != "FIFO_OBSERVATION":
            seen.add(head["id"])
            p = drains.get(head["input_ref"])
            _require(
                head["input_kind"] in ("WAIT", "COMPLETED")
                and _scope(p, d)
                and p["id"] not in seen,
                "drain predecessor",
                d["id"],
            )
            head = p
        _require(head["input_ref"] == head["id"], "drain root", d["id"])
        rid = (d["input_snapshot"] or {}).get("round_id")
        _require(not rid or _scope(d, rounds.get(rid)), "drain FIFO", d["id"])
    rack_clients = {r["client_request_id"] for name in (PRIVATE[1], PRIVATE[3]) for r in data[name]}
    original_clients = set(rack_clients)

    def check_box_transport(row, face, caller, operation):
        client = row["client_request_id"]
        if client is None:
            _require(
                row["transport_task_id"] is None and operation is None,
                "box transport identity",
                row["id"],
            )
            return
        _require(client not in original_clients, "colliding transport client", row["id"])
        original_clients.add(client)
        t = transports.get(client)
        if row["transport_task_id"] or t:
            _require(
                t
                and t["transport_task_id"] == row["transport_task_id"]
                and t["operation_id"] == operation
                and t["owner_domain"] == row["owner_domain"]
                and t["kind"] == "BIN_MOVE"
                and (t["workline_id"], t["station_id"])
                == (caller["caller_workline_id"], caller["station_id"])
                and t["cycle_id"] == face["cycle_id"],
                "box transport owner or identity",
                row["id"],
            )

    for seg in segments.values():
        _require(_scope(seg, feeds.get(seg["feed_id"])), "feed segment owner", seg["id"])
        check_box_transport(seg, feeds[seg["feed_id"]], feeds[seg["feed_id"]], seg["operation_id"])
    for o in opportunities.values():
        f = services.get(o["service_id"])
        _require(_scope(o, f), "return opportunity owner", o["id"])
        if o["kind"] == "PURE_RETURN":
            _require(o["source_ref"] == o["service_id"], "pure return origin", o["id"])
        else:
            kind, _, ref = o["source_ref"].partition(":")
            seg = segments.get(ref) if kind == "SEGMENT" else None
            _require(
                (kind == "FEED" and f["feed_id"] == ref)
                or (_scope(o, seg) and seg["feed_id"] == f["feed_id"]),
                "return opportunity origin",
                o["id"],
            )
    for b in batches.values():
        o = opportunities.get(b["opportunity_id"])
        _require(_scope(b, o), "batch opportunity", b["id"])
        check_box_transport(b, services[o["service_id"]], b, b["transport_operation_id"])
        head = b
        seen = set()
        while head["source_ref"] != o["id"]:
            seen.add(head["id"])
            p = batches.get(head["source_ref"])
            _require(
                _scope(b, p) and p["id"] not in seen and p["opportunity_id"] == o["id"],
                "batch predecessor",
                b["id"],
            )
            head = p
        for member in b["members"]:
            root = rounds.get(member.get("round_id"))
            _require(
                _scope(b, root) and root["bin_code"] == member.get("bin_code"),
                "batch FIFO member",
                b["id"],
            )
    _require(transports.keys() <= original_clients, "unassociated transport on original cycle")
    return demands, services, drains


def _prepare_associations(job, moves, rows, manual, updates):
    table = "smt_manual_task_member" if manual else "bin_roller_face_work"
    preparations = [a for a in moves if a["kind"] in ("ENTER", "ROTATE")]
    for row in rows:
        face = row["rack_face"]
        state = row["status" if manual else "service_state"]
        if not manual and not row["service_id"]:
            continue
        i = row["id"] if manual else row["service_id"]
        candidates = [a for a in preparations if a["target_face"] == face]
        direct = [a for a in candidates if a["source_ref"] == i]
        if direct:
            candidates = direct
        if len(candidates) == 1:
            a = candidates[0]
            if state in ("STARTED", "READY", "COMPLETED"):
                _require(
                    a.get("outcome", {}).get("status") == "SUCCEEDED"
                    and a.get("actual_face") == face
                    and a.get("actual_position") == _point(job["point"])
                    and a["phase"] == "APPLIED",
                    "unproved prepared face",
                    i,
                )
            filters = {"id": row["id"]}
            if not manual:
                filters["purpose"] = row["purpose"]
            updates.append((table, filters, {"preparation_action_id": a["id"]}))
        else:
            _require(
                state in ("PENDING", "CANCELLED", "WITHDRAWN", None), "ambiguous prepared face", i
            )


def _report(job, entry, trigger, records, messages, report_wires, updates):
    i = entry["id"]
    _require(len(records) <= 1, "ambiguous arrival report", i)
    history = report_wires.get(entry["transport_task_id"], [])
    _require(len(history) <= 1, "duplicate arrival report wire", i)
    if not records and not history:
        state = entry["basis"]["legacy_state"]
        _require(
            state in ("REGISTERED", "PENDING")
            and trigger is not None
            and entry.get("outcome_revision") is None
            and entry.get("observation_revision") is None
            or state in ("CANCELLED", "FAILED")
            and entry.get("outcome_revision") == 1
            and not entry.get("observation_revision"),
            "missing processed arrival report identity",
            i,
        )
        return
    _require(
        trigger and trigger["rack_face"] == entry["target_face"] and len(history) == 1,
        "unproved original report trigger or wire",
        i,
    )
    m = history[0]
    payload = json.loads(m["request_raw"])["data"]
    rev = payload.get("outcome_revision")
    _require(
        type(rev) is int
        and 0 < rev <= (entry.get("outcome_revision") or 0)
        and payload
        == {
            "task_id": job["task_id"],
            "rack_id": job["rack_id"],
            "transport_task_id": entry["transport_task_id"],
            "outcome_revision": rev,
            "arrival_face": entry["target_face"],
            "final_position": {"type": "RACK_POSITION", "location_code": job["point"]},
        },
        "report outcome",
        i,
    )
    if records:
        r = records[0]
        _require(r["request_snapshot"] in (None, payload), "conflicting arrival report snapshot", i)
        if r["state"] == "CONSUMED":
            ack = json.loads(m["response_raw"] or b"null")
            _require(
                isinstance(ack, dict)
                and m["status"] == "DONE"
                and m["last_http_status"] == 200
                and m["last_verdict"] == "DELIVERED"
                and ack.get("operation_id") == r["operation_id"]
                and ack.get("code") in ("RECORDED", "DUPLICATE")
                and type(ack.get("timestamp")) is int
                and ack.get("data") == {}
                and r["response"] in (None, {k: ack[k] for k in ("timestamp", "code", "data")}),
                "original recorded report response",
                i,
            )
        if r["request_snapshot"] is None:
            updates.append(
                (
                    "smt_manual_request",
                    {"operation": REPORT, "operation_id": r["operation_id"]},
                    {"request_snapshot": payload},
                )
            )
        _require(
            (r["task_id"], r["work_line_id"], r["rack_work_id"], r["operation_id"], r["message_id"])
            == (job["task_id"], job["work_line_id"], job["id"], m["operation_id"], m["id"]),
            "report registration",
            i,
        )
    _wire(
        messages,
        REPORT,
        m["operation_id"],
        m["id"],
        payload,
        (None, "business:smt_manual"),
        "wms-facts",
    )
    updates.append(
        (
            "smt_manual_task_member",
            {"id": trigger["id"]},
            {"arrival_report_operation_id": m["operation_id"]},
        )
    )


def _plan(data, transports, wire_rows):
    tasks = _unique(data["smt_manual_task"], "task_id", "manual tasks")
    plans = {
        (r["task_id"], r["plan_revision"]): r
        for r in data["smt_manual_input"]
        if r["is_plan_record"]
    }
    manual_jobs = _unique(data[PRIVATE[0]], "id", "manual jobs")
    rollers = _unique(data[PRIVATE[2]], "id", "roller jobs")
    _require(not manual_jobs.keys() & rollers.keys(), "colliding job identities")
    transport = _unique(transports, "client_request_id", "duplicate transport")
    demands, services, drains = _validate_roller_graph(data, rollers, transport)
    # Retention can remove the message, but two different originals cannot own the same ID.
    _unique(
        (
            r
            for r in (*data["smt_manual_request"], *data["bin_roller_decision"])
            if r["message_id"] is not None
        ),
        "message_id",
        "conflicting original WMS message identities",
    )
    member_groups = _group(
        (r for r in data["smt_manual_task_member"] if r["rack_work_id"]), "rack_work_id"
    )
    _require(member_groups.keys() <= manual_jobs.keys(), "orphan manual member")
    faces = _group(data["bin_roller_face_work"], "cycle_id")
    reqs = data["smt_manual_request"]
    reports = _group((r for r in reqs if r["kind"] == "RACK_ARRIVAL_REPORT"), "action_id")
    inputs = (r for r in reqs if r["kind"] == "RACK_DEPARTURE")
    decisions = (
        _group(inputs, "rack_work_id"),
        _group((r for r in data["bin_roller_decision"] if r["kind"] == "DEPARTURE"), "cycle_id"),
    )
    grouped_moves = (_group(data[PRIVATE[1]], "rack_work_id"), _group(data[PRIVATE[3]], "cycle_id"))
    positions = _group(
        (p for p in data["bin_roller_position_fact"] if p["source_kind"] == "RACK"), "source_id"
    )
    orig_moves = _unique(data[PRIVATE[1]] + data[PRIVATE[3]], "id", "colliding movement identities")
    _require(positions.keys() <= {r["id"] for r in data[PRIVATE[3]]}, "orphan rack position")
    messages = defaultdict(list)
    report_wires = defaultdict(list)
    for m in wire_rows:
        messages[m["id"]] = m
        messages[m["operation"], m["operation_id"]].append(m)
        if m["operation"] == SUBMIT:
            expected = next((t for t in transports if t["operation_id"] == m["operation_id"]), None)
            _require(
                expected and expected["transport_task_id"] == m["business_key"],
                "orphan transport submission",
                m["id"],
            )
        if m["operation"] == REPORT:
            wire = json.loads(m["request_raw"])
            report_wires[wire["data"]["transport_task_id"]].append(m)
    jobs = []
    actions = []
    updates = []
    for idx, parents in enumerate((manual_jobs, rollers)):
        manual = idx == 0
        _require(
            grouped_moves[idx].keys() <= parents.keys() and decisions[idx].keys() <= parents.keys(),
            "orphan action or decision",
        )
        for i, r in parents.items():
            state = r["state" if manual else "status"]
            trigger = plan = None
            if manual:
                task = tasks.get(r["task_id"])
                _require(
                    task
                    and (task["work_line_id"], task["workline_code"])
                    == (r["work_line_id"], r["workline_code"])
                    and r["role"] in ("TRANSFER", "RETURN")
                    and state in ("ACTIVE", "ENTRY_CANCELLED", "DEPARTED"),
                    "manual rack owner/state",
                    i,
                )
                rows = sorted(
                    member_groups[i], key=lambda m: (m["plan_revision"], m["member_order"], m["id"])
                )
                for m in rows:
                    grant = plans.get((m["task_id"], m["plan_revision"]))
                    _require(
                        grant
                        and grant["work_line_id"] == r["work_line_id"]
                        and (m["task_id"], m["kind"], m["rack_id"], r["role"])
                        == (r["task_id"], "DIRECT", r["rack_id"], "RETURN")
                        and any(
                            (p.get("source_locator") or {})
                            == {
                                "type": "RACK_SLOT",
                                "rack_id": m["rack_id"],
                                "rack_face": m["rack_face"],
                                "slot_id": m["slot_id"],
                            }
                            for p in grant["payload"].get("added_direct_picks") or []
                        ),
                        "manual member original plan",
                        m["id"],
                    )
                source = "legacy/manual/" + i
                initial = r["initial_face"]
                resources = {r["role"] + "_RACK": r["location_code"]}
                if rows:
                    trigger = rows[0]
                    _require(trigger["rack_face"] == initial, "manual triggering face", i)
                    source = trigger["id"]
                if r["role"] == "TRANSFER":
                    plan = plans.get((r["task_id"], 1))
                    if plan and plan["payload"].get("target_rack") == task["target_rack"] == {
                        "rack_id": r["rack_id"],
                        "rack_face": initial,
                    }:
                        _require(plan["work_line_id"] == r["work_line_id"], "target plan owner", i)
                        source = r["task_id"]
                        resources["target_plan"] = [plan["operation"], plan["operation_id"], 1]
                    else:
                        plan = None
            else:
                rows = faces[i]
                source = r["source_ref"]
                resources = r["resources"]
                _require(state in ("ACTIVE", "WITHDRAWN", "RELEASED"), "roller job state", i)
                if r["purpose"] == "ORDINARY":
                    origin = demands.get(source)
                    _require(
                        _scope(r, origin)
                        and (origin["rack_id"], origin["task_id"], origin["cycle_id"])
                        == (r["rack_id"], r["task_id"], i),
                        "ordinary cycle origin",
                        i,
                    )
                    initial = origin["rack_face"]
                else:
                    drain = drains.get(source)
                    _require(
                        r["purpose"] == "DRAIN" and r["task_id"] is None and _scope(r, drain),
                        "drain owner",
                        i,
                    )
                    candidates = [
                        x
                        for x in (drain["decision"] or {}).get("racks", [])
                        if x["rack_id"] == r["rack_id"]
                    ]
                    _require(
                        len(candidates) == 1
                        and candidates[0]["rack_face"]
                        and (r["rack_id"] not in drain["unused_racks"] or state == "WITHDRAWN"),
                        "drain candidate",
                        i,
                    )
                    face_order = candidates[0]["rack_face"]
                    _require(
                        len(face_order) == len(set(face_order))
                        and all(
                            x["rack_face"] in face_order
                            and x["source_ref"] == source + ":" + x["rack_face"]
                            for x in rows
                        ),
                        "drain service original face grant",
                        i,
                    )
                    initial = face_order[0]
            job = _base_job(r, manual, source, initial, resources)
            jobs.append(job)
            moves = []
            for raw in grouped_moves[idx][i]:
                _require(manual or _scope(r, raw), "roller move owner", raw["id"])
                move = _move(job, raw, manual, transport, messages, positions[raw["id"]])
                for p in positions[raw["id"]]:
                    _require(
                        _scope(r, p)
                        and p["object_kind"] == "RACK"
                        and p["object_code"] == r["rack_id"]
                        and p["source_revision"] <= (move.get("position_revision") or 0),
                        "rack position owner or cursor",
                        p["id"],
                    )
                ref = move["source_ref"]
                if move["kind"] != "DEPART":
                    _require(
                        (move["kind"] == "ENTER" and ref == i)
                        or any(
                            (x["id"] if manual else x["service_id"]) == ref
                            and x["rack_face"] == move["target_face"]
                            for x in rows
                        ),
                        "preparation origin",
                        raw["id"],
                    )
                moves.append(move)
            bykind = _group(moves, "kind")
            _require(
                len(bykind["ENTER"]) <= 1 and len(bykind["DEPART"]) <= 1,
                "duplicate singleton movement",
                i,
            )
            rotations = bykind["ROTATE"]
            if len(rotations) > 1:
                _require(not manual and r["purpose"] == "DRAIN", "unprovable move order", i)
                rotations.sort(key=lambda a: face_order.index(a["target_face"]))
                _require(
                    [a["target_face"] for a in rotations] == face_order[1 : len(rotations) + 1],
                    "ambiguous drain rotation order",
                    i,
                )
            enter = next(iter(bykind["ENTER"]), None)
            depart = next(iter(bykind["DEPART"]), None)
            ordered = bykind["ENTER"] + bykind["ROTATE"]
            _require(enter or not (moves or decisions[idx][i]), "missing original entry", i)
            if enter:
                _require(enter["target_face"] == initial, "entry face", i)
            for prior, current in zip(ordered, ordered[1:], strict=False):
                current["previous_action_id"] = prior["id"]
            if len(ordered) > 1:
                for prior in ordered[:-1]:
                    _require(
                        prior["phase"] == "APPLIED"
                        and (prior.get("outcome") or {}).get("status") == "SUCCEEDED"
                        and prior.get("actual_position") == prior["target"],
                        "unproved preparation predecessor",
                        prior["id"],
                    )
            for a in ordered:
                if a.get("actual_position"):
                    job.update(
                        actual_position=a["actual_position"],
                        actual_face=a.get("actual_face"),
                        position_action_id=a["id"],
                    )
            chain = _decisions(
                job, decisions[idx][i], manual, services, r.get("departure_operation_id"), messages
            )
            if chain:
                _require(
                    job.get("position_action_id")
                    and job.get("actual_position") == _point(job["point"]),
                    "unproved original decision position",
                    i,
                )
                for d in chain:
                    old = d["basis"].get("legacy_basis") or {}
                    if "action_id" in old:
                        actual = next((a for a in ordered if a["id"] == old["action_id"]), None)
                        _require(
                            actual
                            and actual["id"] == job.get("position_action_id")
                            and old.get("revision") is not None
                            and old["revision"] <= (actual.get("outcome_revision") or 0),
                            "decision original position",
                            d["id"],
                        )
                        d["basis"].update(
                            position_action_id=actual["id"], outcome_revision=old["revision"]
                        )
                job.update(state="RELEASING", release_requested=True)
                ordered += chain
            if depart:
                _require(
                    chain
                    and chain[-1]["id"] == depart["source_ref"]
                    and chain[-1]["phase"] == "READY"
                    and _place(chain[-1]["decision"]["rack_destination"]) == depart["target"],
                    "departure READY origin",
                    i,
                )
                depart.update(previous_action_id=chain[-1]["id"], previous_decision_phase="READY")
                ordered.append(depart)
            if state in ("DEPARTED", "RELEASED"):
                _require(
                    depart
                    and (
                        depart["basis"]["legacy_state"] == "SUCCEEDED"
                        if manual
                        else depart["basis"]["legacy_state"] == "ACCEPTED"
                        and r["release_basis"] == depart["basis"]["legacy_acceptance_basis"]
                    ),
                    "original recorded closure",
                    i,
                )
                depart.update(completion_rule="LEGACY_RECORDED_CLOSURE", phase="LEGACY_CLOSED")
                job.update(
                    state="COMPLETED",
                    pending=False,
                    closure_reason="LEGACY_CLOSED",
                    closure_basis={
                        **job["release_basis"],
                        "action_id": depart["id"],
                        "outcome_revision": depart.get("outcome_revision"),
                        "action_basis": depart["basis"],
                    },
                )
            if state == "WITHDRAWN":
                face_ids = {x["service_id"] for x in rows if x["service_id"]}
                holders = {
                    o["id"]
                    for o in data["bin_roller_return_opportunity"]
                    if o["service_id"] in face_ids
                }
                _require(
                    not any(
                        not o["ended"]
                        for o in data["bin_roller_return_opportunity"]
                        if o["id"] in holders
                    )
                    and not any(
                        b["pending"]
                        for b in data["bin_roller_return_batch"]
                        if b["opportunity_id"] in holders
                    ),
                    "withdrawn return responsibility",
                    i,
                )
                _require(
                    not moves
                    and not chain
                    and all(
                        x["service_state"] in (None, "WITHDRAWN")
                        and x["demand_state"] in (None, "WITHDRAWN")
                        and not x["feed_id"]
                        for x in rows
                    ),
                    "withdrawn job still held",
                    i,
                )
                job.update(
                    state=state,
                    pending=False,
                    closure_reason=state,
                    closure_basis=job["release_basis"],
                )
            if state == "ENTRY_CANCELLED":
                _require(enter and len(ordered) == 1, "cancelled entry with successor", i)
                # A historical zone is outside managed rack positions. Never resolve a position
                # conflict by consulting today's mutable WorkLine binding.
                if (
                    enter["phase"] == "APPLIED"
                    and (enter.get("outcome") or {}).get("status") == "CANCELLED"
                    and (enter.get("actual_position") or {}).get("kind") == "ZONE"
                    and not enter["outcome"].get("position_unknown")
                    and not enter.get("pending_revision")
                ):
                    job.update(
                        state="NOT_ARRIVED",
                        pending=False,
                        closure_reason="NOT_ARRIVED",
                        closure_basis=job["release_basis"],
                    )
                else:
                    job["review_reason"] = "LEGACY_ENTRY_CANCELLED"
            for n, a in enumerate(ordered, 1):
                a["ordinal"] = n
            if ordered:
                job["current_action_id"] = ordered[-1]["id"]
            actions.extend(ordered)
            _prepare_associations(job, moves, rows, manual, updates)
            if manual and plan and enter:
                updates.append(
                    (
                        "smt_manual_input",
                        {"operation": plan["operation"], "operation_id": plan["operation_id"]},
                        {"target_job_id": i, "target_action_id": enter["id"]},
                    )
                )
            if manual and r["role"] == "RETURN" and enter:
                _report(
                    job,
                    enter,
                    trigger,
                    reports.pop(enter["id"], []),
                    messages,
                    report_wires,
                    updates,
                )
                report_wires.pop(enter["transport_task_id"], None)
    _require(not reports and not report_wires, "orphan arrival report")
    _unique(actions, "id", "colliding shared action identities")
    _require(
        {a["id"] for a in actions if a["kind"] != "DEPARTURE_DECIDE"} == set(orig_moves),
        "migration move count",
    )
    _require(
        len(actions) == len(orig_moves) + sum(len(v) for g in decisions for v in g.values()),
        "migration decision count",
    )
    return jobs, actions, updates


def _change_contract(schema, forward):
    before, after = (OLD_CHECKS, NEW_CHECKS) if forward else (NEW_CHECKS, OLD_CHECKS)
    for table in ("smt_manual_request", "bin_roller_decision"):
        with op.batch_alter_table(table, schema=schema) as batch:
            for name in before:
                if name.startswith(table + "_"):
                    batch.drop_constraint(op.f(name), type_="check")
            if table == "bin_roller_decision":
                if forward:
                    batch.drop_index("bin_roller_departure_source")
                    batch.drop_column("cycle_id")
                    batch.drop_column("source_ref")
                else:
                    for key in ("cycle_id", "source_ref"):
                        batch.add_column(sa.Column(key, sa.String(), nullable=True))
                    condition = sa.text("kind = 'DEPARTURE'")
                    batch.create_index(
                        "bin_roller_departure_source",
                        ["owner_domain", "work_line_id", "cycle_id", "source_ref"],
                        unique=True,
                        postgresql_where=condition,
                        sqlite_where=condition,
                    )
            for name, expr in after.items():
                if name.startswith(table + "_"):
                    batch.create_check_constraint(op.f(name), expr)


def _checked_insert(bind, name, rows):
    t = _table(bind, name)
    for r in rows:
        bind.execute(t.insert().values(**r))
    saved = {r["id"]: r for r in bind.execute(sa.select(t)).mappings()}
    _require(
        len(saved) == len(rows)
        and all(all(saved[r["id"]][k] == v for k, v in r.items()) for r in rows),
        "backfill comparison",
        name,
    )


def upgrade():
    bind = op.get_bind()
    schema = _schema(bind)
    names = (
        *PRIVATE,
        *NEW_COLUMNS,
        "smt_manual_task",
        "smt_manual_request",
        "bin_roller_position_fact",
        "bin_roller_feed_segment",
        "bin_roller_return_opportunity",
        "bin_roller_return_batch",
    )
    data = {name: _rows(bind, name) for name in names}

    def original_column(names, column):
        return sa.union_all(*(sa.select(_table(bind, n).c[column]) for n in names))

    t = _table(bind, "transport_task")
    # Keep filtering in the database; histories may exceed the driver's bind-parameter limit.
    cycles = original_column((PRIVATE[0], PRIVATE[2]), "id")
    registered = original_column(
        (PRIVATE[1], PRIVATE[3], "bin_roller_feed_segment", "bin_roller_return_batch"),
        "client_request_id",
    )
    selected = sa.select(t).where(t.c.client_request_id.in_(registered) | t.c.cycle_id.in_(cycles))
    ts = list(bind.execute(selected).mappings())
    m = _table(bind, "reliable_message")
    refs = sa.union_all(
        *(
            sa.select(table.c.message_id).where(table.c.kind.in_(kinds))
            for table, kinds in (
                (_table(bind, "smt_manual_request"), ("RACK_ARRIVAL_REPORT", "RACK_DEPARTURE")),
                (_table(bind, "bin_roller_decision"), ("DEPARTURE",)),
            )
        )
    )
    matched = selected.subquery()
    wire = list(
        bind.execute(
            sa.select(m).where(
                m.c.id.in_(refs)
                | m.c.operation.in_((REPORT, DECIDE))
                | (m.c.operation == SUBMIT)
                & (
                    m.c.operation_id.in_(sa.select(matched.c.operation_id))
                    | m.c.business_key.in_(sa.select(matched.c.transport_task_id))
                )
            )
        ).mappings()
    )
    try:
        jobs, actions, updates = _plan(data, ts, wire)
        _create_shared(schema)
        for name, columns in NEW_COLUMNS.items():
            for key, typ in columns.items():
                op.add_column(name, sa.Column(key, typ(), nullable=True), schema=schema)
        _checked_insert(bind, "rack_dispatch_job", jobs)
        _checked_insert(bind, "rack_dispatch_action", actions)
        tables = {name: _table(bind, name) for name in (*NEW_COLUMNS, "smt_manual_request")}
        for name, keys, values in updates:
            table = tables[name]
            result = bind.execute(
                table.update().where(*(table.c[k] == v for k, v in keys.items())).values(**values)
            )
            _require(result.rowcount == 1, "host association update", name)
            if name == "smt_manual_request":
                data[name] = [
                    dict(r, **values) if all(r[k] == v for k, v in keys.items()) else r
                    for r in data[name]
                ]
    except (sa.exc.IntegrityError, KeyError, ValueError, TypeError, IndexError, AttributeError):
        raise RuntimeError(
            "shared rack cutover: conflicting identity or invalid original evidence"
        ) from None
    for name, kind in (
        ("smt_manual_request", "RACK_DEPARTURE"),
        ("bin_roller_decision", "DEPARTURE"),
    ):
        table = _table(bind, name)
        result = bind.execute(table.delete().where(table.c.kind == kind))
        _require(
            result.rowcount == sum(r["kind"] == kind for r in data[name]),
            "translated decision count",
            name,
        )
    _change_contract(schema, True)
    # All retained columns, including frozen manifests, report identities and FIFO facts, survive.
    for name, rows in data.items():
        if name in PRIVATE:
            continue
        retained = [
            r
            for r in rows
            if not (
                name == "smt_manual_request"
                and r["kind"] == "RACK_DEPARTURE"
                or name == "bin_roller_decision"
                and r["kind"] == "DEPARTURE"
            )
        ]
        current = _table(bind, name)
        keys = [col.name for col in current.primary_key]
        saved = {tuple(r[k] for k in keys): r for r in bind.execute(sa.select(current)).mappings()}
        _require(
            len(saved) == len(retained)
            and all(
                all(
                    saved[tuple(r[k] for k in keys)][c] == v for c, v in r.items() if c in current.c
                )
                for r in retained
            ),
            "retained history comparison",
            name,
        )
    for name in reversed(PRIVATE):
        op.drop_table(name, schema=schema)


def downgrade():
    bind = op.get_bind()
    schema = _schema(bind)
    for name, cols in {
        "rack_dispatch_job": (),
        "rack_dispatch_action": (),
        "transport_task": ("submission_acceptance",),
        **NEW_COLUMNS,
    }.items():
        table = _table(bind, name)
        query = sa.select(sa.literal(1)).select_from(table).limit(1)
        if cols:
            query = query.where(sa.or_(*(table.c[c].is_not(None) for c in cols)))
        _require(
            bind.scalar(query) is None,
            "downgrade would discard recorded evidence; restore backup",
            name,
        )
    _change_contract(schema, False)
    for name, cols in NEW_COLUMNS.items():
        with op.batch_alter_table(name, schema=schema) as batch:
            for key in cols:
                batch.drop_column(key)
    for name in ("rack_dispatch_action", "rack_dispatch_job"):
        op.drop_table(name, schema=schema)
    _restore_private(schema)
