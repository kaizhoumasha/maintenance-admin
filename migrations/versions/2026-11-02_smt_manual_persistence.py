"""Converge SMT input and WMS attempt records without replacing original identities.

Revision ID: smtmanualcompact
Revises: 0177313880f9
"""

import sqlalchemy as sa
from alembic import op

revision = "smtmanualcompact"
down_revision = "0177313880f9"
branch_labels = None
depends_on = None

REMOVED = (
    "task_input",
    "task_plan",
    "task_confirmation",
    "admission",
    "completion",
    "rack_completion",
    "rack_report",
    "rack_departure",
)
BIN_NOTICE = "manual_bin.work_completed@v1"
RACK_NOTICES = (
    "outbound.manual_rack.direct_pick_completed@v1",
    "outbound.return_rack.task_completed@v1",
)
REQUEST_OPERATIONS = {
    "PREPARE": "sorting.task.prepare@v1",
    "COMPLETION_CONFIRM": "sorting.task.completion_confirm@v1",
    "BIN_ADMISSION": "manual_bin.work_admission_decide@v1",
    "RACK_ARRIVAL_REPORT": "outbound.return_rack.arrival_report@v1",
    "RACK_DEPARTURE": "rack.departure_decide@v1",
}


def _targets(schema):
    metadata = sa.MetaData(schema=schema)
    input = sa.Table(
        "smt_manual_input",
        metadata,
        sa.Column("operation", sa.String(), nullable=False, primary_key=True),
        sa.Column("operation_id", sa.String(), nullable=False, primary_key=True),
        sa.Column("receipt_id", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("task_id", sa.String(), nullable=False, primary_key=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("timestamp", sa.BigInteger(), nullable=False, primary_key=False),
        sa.Column("payload", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("state", sa.String(), nullable=False, primary_key=False),
        sa.Column("reason", sa.String(), nullable=True, primary_key=False),
        sa.Column("is_plan_record", sa.Boolean(), nullable=False, primary_key=False),
        sa.Column("plan_revision", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("rack_id", sa.String(), nullable=True, primary_key=False),
        sa.Column("rack_face", sa.String(), nullable=True, primary_key=False),
        sa.Column("bin_code", sa.String(), nullable=True, primary_key=False),
        sa.Column("arrival_id", sa.String(), nullable=True, primary_key=False),
        sa.Column("completed_at", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("completion_result", sa.String(), nullable=True, primary_key=False),
        sa.Column("original_operation_id", sa.String(), nullable=True, primary_key=False),
        sa.CheckConstraint(
            "(operation IN "
            "('sorting.task.issued@v1','sorting.task.queue_changed@v1','sorting.task.plan"
            "_delta@v1','sorting.task.cancel@v1') AND state IN "
            "('APPLIED','DEFERRED','REJECTED') AND rack_id IS NULL AND rack_face IS NULL "
            "AND bin_code IS NULL AND arrival_id IS NULL AND completed_at IS NULL AND "
            "completion_result IS NULL AND original_operation_id IS NULL AND ((operation "
            "= 'sorting.task.plan_delta@v1' AND plan_revision IS NOT NULL) OR (operation "
            "!= 'sorting.task.plan_delta@v1' AND plan_revision IS NULL))) OR (operation "
            "= 'manual_bin.work_completed@v1' AND receipt_id IS NOT NULL AND bin_code IS "
            "NOT NULL AND completed_at IS NOT NULL AND completion_result IS NOT NULL AND "
            "plan_revision IS NULL AND rack_id IS NULL AND rack_face IS NULL AND state "
            "IN ('PENDING','APPLIED','DUPLICATE','REVIEW')) OR (operation IN "
            "('outbound.manual_rack.direct_pick_completed@v1','outbound.return_rack.task_"
            "completed@v1') AND receipt_id IS NOT NULL AND rack_id IS NOT NULL AND "
            "plan_revision IS NOT NULL AND completed_at IS NOT NULL AND bin_code IS NULL "
            "AND arrival_id IS NULL AND completion_result IS NULL AND "
            "original_operation_id IS NULL AND reason IS NULL AND state IN "
            "('PENDING','APPLIED') AND ((operation = "
            "'outbound.manual_rack.direct_pick_completed@v1' AND rack_face IS NOT NULL) "
            "OR (operation = 'outbound.return_rack.task_completed@v1' AND rack_face IS "
            "NULL)))",
            name="smt_manual_input_smt_manual_input_shape_check",
        ),
        sa.CheckConstraint(
            "NOT is_plan_record OR (operation = 'sorting.task.plan_delta@v1' AND "
            "plan_revision IS NOT NULL AND state = 'APPLIED')",
            name="smt_manual_input_smt_manual_input_plan_record_check",
        ),
        sa.CheckConstraint(
            "state IN ('APPLIED','DEFERRED','REJECTED','PENDING','DUPLICATE','REVIEW')",
            name="smt_manual_input_smt_manual_input_state_check",
        ),
    )
    sa.Index(
        "smt_manual_original_plan",
        input.c.task_id,
        input.c.plan_revision,
        unique=True,
        postgresql_where=sa.text("is_plan_record"),
        sqlite_where=sa.text("is_plan_record"),
    )
    sa.Index(
        "smt_manual_pending_input", input.c.task_id, input.c.operation, input.c.state, unique=False
    )
    request = sa.Table(
        "smt_manual_request",
        metadata,
        sa.Column("operation", sa.String(), nullable=False, primary_key=True),
        sa.Column("operation_id", sa.String(), nullable=False, primary_key=True),
        sa.Column("kind", sa.String(), nullable=False, primary_key=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False, primary_key=False),
        sa.Column("task_id", sa.String(), nullable=False, primary_key=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("arrival_id", sa.String(), nullable=True, primary_key=False),
        sa.Column("rack_work_id", sa.String(), nullable=True, primary_key=False),
        sa.Column("action_id", sa.String(), nullable=True, primary_key=False),
        sa.Column("plan_revision", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("request_snapshot", sa.JSON(none_as_null=True), nullable=True, primary_key=False),
        sa.Column("basis", sa.JSON(none_as_null=True), nullable=True, primary_key=False),
        sa.Column("response", sa.JSON(none_as_null=True), nullable=True, primary_key=False),
        sa.Column("state", sa.String(), nullable=False, primary_key=False),
        sa.Column("decision_code", sa.String(), nullable=True, primary_key=False),
        sa.Column("saved_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("due_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("previous_operation_id", sa.String(), nullable=True, primary_key=False),
        sa.CheckConstraint(
            "kind != 'COMPLETION_CONFIRM' OR state IN ('REQUESTED','CONFLICT') "
            "OR decision_code IS NOT NULL",
            name="smt_manual_request_smt_manual_confirmation_decision_check",
        ),
        sa.CheckConstraint(
            "kind != 'RACK_DEPARTURE' OR request_snapshot IS NOT NULL",
            name="smt_manual_request_smt_manual_request_snapshot_check",
        ),
        sa.CheckConstraint(
            "(kind = 'COMPLETION_CONFIRM' AND plan_revision IS NOT NULL) OR "
            "(kind != 'COMPLETION_CONFIRM' AND plan_revision IS NULL)",
            name="smt_manual_request_smt_manual_request_revision_check",
        ),
        sa.CheckConstraint(
            "(kind IN ('COMPLETION_CONFIRM','RACK_DEPARTURE') AND basis IS NOT NULL) OR "
            "(kind NOT IN ('COMPLETION_CONFIRM','RACK_DEPARTURE') AND basis IS NULL)",
            name="smt_manual_request_smt_manual_request_basis_check",
        ),
        sa.CheckConstraint(
            "state != 'WAITING' OR due_at_ms IS NOT NULL",
            name="smt_manual_request_smt_manual_request_due_check",
        ),
        sa.CheckConstraint(
            "(kind = 'RACK_ARRIVAL_REPORT' AND action_id IS NOT NULL) OR (kind != "
            "'RACK_ARRIVAL_REPORT' AND action_id IS NULL)",
            name="smt_manual_request_smt_manual_request_action_check",
        ),
        sa.CheckConstraint(
            "state IN ('REQUESTED','WAITING','CONSUMED','CONFLICT')",
            name="smt_manual_request_smt_manual_request_state_check",
        ),
        sa.CheckConstraint(
            "(kind = 'BIN_ADMISSION' AND arrival_id IS NOT NULL AND rack_work_id IS NULL) "
            "OR (kind IN ('RACK_ARRIVAL_REPORT','RACK_DEPARTURE') AND rack_work_id IS NOT "
            "NULL AND arrival_id IS NULL) OR (kind IN ('PREPARE','COMPLETION_CONFIRM') AND "
            "rack_work_id IS NULL AND arrival_id IS NULL)",
            name="smt_manual_request_smt_manual_request_subject_check",
        ),
        sa.CheckConstraint(
            "(kind = 'PREPARE' AND operation = 'sorting.task.prepare@v1') OR (kind = "
            "'COMPLETION_CONFIRM' AND operation = 'sorting.task.completion_confirm@v1') OR "
            "(kind = 'BIN_ADMISSION' AND operation = "
            "'manual_bin.work_admission_decide@v1') OR (kind = 'RACK_ARRIVAL_REPORT' AND "
            "operation = 'outbound.return_rack.arrival_report@v1') OR (kind = "
            "'RACK_DEPARTURE' AND operation = 'rack.departure_decide@v1')",
            name="smt_manual_request_smt_manual_request_kind_check",
        ),
    )
    sa.Index(
        "smt_manual_report_action",
        request.c.action_id,
        unique=True,
        postgresql_where=sa.text("kind = 'RACK_ARRIVAL_REPORT'"),
        sqlite_where=sa.text("kind = 'RACK_ARRIVAL_REPORT'"),
    )
    sa.Index(
        "smt_manual_request_subject_state",
        request.c.kind,
        request.c.task_id,
        request.c.state,
        unique=False,
    )
    return input, request


def _create_old(schema):
    op.create_table(
        "smt_manual_task_input",
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("timestamp", sa.BigInteger(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.Column("unapplied_reason", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint(
            "operation", "operation_id", name=op.f("smt_manual_task_input_pkey")
        ),
        schema=schema,
    )
    op.create_table(
        "smt_manual_task_confirmation",
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_revision", sa.BigInteger(), nullable=False),
        sa.Column("basis", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("response", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("wait_until_ms", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("operation_id", name=op.f("smt_manual_task_confirmation_pkey")),
        schema=schema,
    )
    op.create_table(
        "smt_manual_task_plan",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("plan_revision", sa.BigInteger(), nullable=False),
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("task_id", "plan_revision", name=op.f("smt_manual_task_plan_pkey")),
        schema=schema,
    )
    op.create_table(
        "smt_manual_admission",
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("arrival_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("response", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("saved_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("wait_until_ms", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("operation_id", name=op.f("smt_manual_admission_pkey")),
        schema=schema,
    )
    op.create_table(
        "smt_manual_completion",
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("receipt_id", sa.BigInteger(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("bin_code", sa.String(), nullable=False),
        sa.Column("result", sa.String(), nullable=False),
        sa.Column("completed_at", sa.BigInteger(), nullable=False),
        sa.Column("timestamp", sa.BigInteger(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("arrival_id", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("original_operation_id", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("operation_id", name=op.f("smt_manual_completion_pkey")),
        schema=schema,
    )
    op.create_table(
        "smt_manual_rack_completion",
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("receipt_id", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("rack_id", sa.String(), nullable=False),
        sa.Column("plan_revision", sa.BigInteger(), nullable=False),
        sa.Column("rack_face", sa.String(), nullable=True),
        sa.Column("completed_at", sa.BigInteger(), nullable=False),
        sa.Column("timestamp", sa.BigInteger(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint(
            "operation_id", "operation", name=op.f("smt_manual_rack_completion_pkey")
        ),
        schema=schema,
    )
    op.create_table(
        "smt_manual_rack_report",
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("rack_work_id", sa.String(), nullable=False),
        sa.Column("action_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("recorded", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("operation_id", name=op.f("smt_manual_rack_report_pkey")),
        schema=schema,
    )
    op.create_table(
        "smt_manual_rack_departure",
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("rack_work_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("basis", sa.JSON(), nullable=False),
        sa.Column("decision", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("wait_until_ms", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("operation_id", name=op.f("smt_manual_rack_departure_pkey")),
        schema=schema,
    )


def _rows(bind, schema, name):
    table = sa.Table("smt_manual_" + name, sa.MetaData(), schema=schema, autoload_with=bind)
    json_columns = [c for c in table.c if isinstance(c.type, sa.JSON)]
    query = sa.select(table, *(c.is_(None).label("_sql_null_" + c.name) for c in json_columns))
    rows = []
    for result in bind.execute(query).mappings():
        row = dict(result)
        for column in json_columns:
            is_sql_null = row.pop("_sql_null_" + column.name)
            if row[column.name] is None and not is_sql_null:
                row[column.name] = sa.JSON.NULL
        rows.append(row)
    return rows


def _require(condition, kind, identity):
    if not condition:
        raise ValueError(f"SMT persistence: invalid {kind} reference {identity}")


def _validate_completions(notices, tasks, bins):
    subject = ("task_id", "work_line_id", "bin_code")
    rooted = set()
    for identity, row in notices.items():
        task = tasks.get(row["task_id"])
        _require(task and task["work_line_id"] == row["work_line_id"], "bin completion", identity)
        if row["arrival_id"] is not None:
            arrival = bins.get(row["arrival_id"])
            _require(
                arrival and all(arrival[k] == row[k] for k in subject),
                "completion arrival",
                identity,
            )
        path = set()
        while identity not in rooted:
            _require(identity not in path, "completion original", identity)
            path.add(identity)
            previous = notices[identity]["original_operation_id"]
            if previous is None:
                break
            parent = notices.get(previous)
            _require(
                parent and all(parent[k] == row[k] for k in subject),
                "completion original",
                identity,
            )
            identity = previous
        rooted.update(path)


CONFIRMATION_CODES = ("COMPLETED", "BUSINESS_IN_PROGRESS", "PLAN_REVISION_STALE")


def _confirmation_code(row, legacy_state):
    response = row["response"]
    data = response.get("data") if isinstance(response, dict) else None
    saved = data.get("result") if isinstance(data, dict) else None
    if legacy_state in CONFIRMATION_CODES:
        _require(
            saved is None or saved == legacy_state, "confirmation decision", row["operation_id"]
        )
        return legacy_state
    _require(legacy_state in ("PENDING", "CONFLICT"), "confirmation state", row["operation_id"])
    return saved if saved in CONFIRMATION_CODES else None


def _validate_prepares(tasks, requests):
    owners = {}
    messages = set()
    for row in requests:
        if row["kind"] != "PREPARE":
            continue
        task = tasks.get(row["task_id"])
        identity = row["operation_id"]
        _require(
            task
            and task["prepare_operation_id"] == identity
            and row["work_line_id"] == task["work_line_id"]
            and row["message_id"] == task["prepare_message_id"]
            and identity not in owners
            and row["message_id"] not in messages,
            "prepare identity",
            identity,
        )
        owners[identity] = row["task_id"]
        messages.add(row["message_id"])
        accepted = task["prepare_accepted"]
        _require(
            row["state"] == ("CONSUMED" if accepted else "REQUESTED")
            and row["decision_code"] == ("ACCEPTED" if accepted else None)
            and row["saved_at_ms"] == task["prepare_accepted_at_ms"],
            "prepare acceptance",
            identity,
        )
        snapshot = row["request_snapshot"]
        if snapshot is not None:
            _require(
                isinstance(snapshot, dict)
                and all(snapshot.get(k) == task[k] for k in ("task_id", "workline_code")),
                "prepare subject",
                identity,
            )
        if row["response"] is not None:
            response = row["response"]
            _require(
                accepted
                and isinstance(response, dict)
                and response.get("code") == "PREPARE_ACCEPTED"
                and response.get("timestamp") == task["prepare_response_timestamp"],
                "prepare response",
                identity,
            )
    for task in tasks.values():
        identity = task["prepare_operation_id"]
        if (
            identity is not None
            or task["prepare_message_id"] is not None
            or task["prepare_accepted"]
        ):
            _require(
                owners.get(identity) == task["task_id"],
                "missing prepare",
                identity or task["task_id"],
            )


def _input(operation, row, line_id, **values):
    return dict(
        operation=operation,
        operation_id=row["operation_id"],
        receipt_id=row.get("receipt_id"),
        task_id=row["task_id"],
        work_line_id=line_id,
        timestamp=row["timestamp"],
        payload=row["data"],
        state=values.pop("state"),
        reason=row.get("reason"),
        is_plan_record=False,
        plan_revision=row.get("plan_revision"),
        rack_id=row.get("rack_id"),
        rack_face=row.get("rack_face"),
        bin_code=row.get("bin_code"),
        arrival_id=row.get("arrival_id"),
        completed_at=row.get("completed_at"),
        completion_result=row.get("result"),
        original_operation_id=row.get("original_operation_id"),
        **values,
    )


def _request(kind, row, task, **values):
    return (
        dict(
            operation=REQUEST_OPERATIONS[kind],
            operation_id=row["operation_id"],
            kind=kind,
            message_id=row["message_id"],
            task_id=task["task_id"],
            work_line_id=task["work_line_id"],
            arrival_id=row.get("arrival_id"),
            rack_work_id=row.get("rack_work_id"),
            action_id=row.get("action_id"),
            plan_revision=row.get("plan_revision"),
            request_snapshot=row.get("request"),
            basis=row.get("basis"),
            response=row.get("response"),
            state="REQUESTED",
            decision_code=None,
            saved_at_ms=row.get("saved_at_ms"),
            due_at_ms=row.get("wait_until_ms"),
            previous_operation_id=None,
        )
        | values
    )


def _converted(bind, schema):
    data = {
        name: _rows(bind, schema, name)
        for name in (*REMOVED, "task", "bin", "rack", "rack_action", "task_member")
    }
    tasks = {r["task_id"]: r for r in data["task"]}
    bins = {r["arrival_id"]: r for r in data["bin"]}
    racks = {r["id"]: r for r in data["rack"]}
    actions = {r["id"]: r for r in data["rack_action"]}
    inputs, requests = [], []
    for row in data["task_input"]:
        _require(row["task_id"] in tasks, "task input", row["operation_id"])
        _require(isinstance(row["data"], dict), "task input payload", row["operation_id"])
        state = (
            "APPLIED"
            if row["applied"]
            else "DEFERRED"
            if row["unapplied_reason"]
            in {"PREPARE_PENDING", "MISSING_REVISION", "WORKLINE_DISABLED"}
            else "REJECTED"
        )
        item = _input(row["operation"], row, tasks[row["task_id"]]["work_line_id"], state=state)
        item.update(reason=row["unapplied_reason"], plan_revision=row["data"].get("plan_revision"))
        inputs.append(item)
    by_input = {(r["operation"], r["operation_id"]): r for r in inputs}
    plans = {(row["task_id"], row["plan_revision"]): row for row in data["task_plan"]}
    for row in data["task_plan"]:
        item = by_input.get(("sorting.task.plan_delta@v1", row["operation_id"]))
        _require(
            item is not None
            and item["task_id"] == row["task_id"]
            and item["payload"] == row["data"]
            and item["state"] == "APPLIED"
            and item["plan_revision"] == row["plan_revision"],
            "original plan",
            row["operation_id"],
        )
        item["is_plan_record"] = True
    for item in inputs:
        if item["operation"] == "sorting.task.plan_delta@v1" and item["state"] == "APPLIED":
            original = plans.get((item["task_id"], item["plan_revision"]))
            _require(
                original is not None and original["data"] == item["payload"],
                "applied plan baseline",
                item["operation_id"],
            )
    for task in tasks.values():
        _require(
            task["plan_revision"] == 0 or (task["task_id"], task["plan_revision"]) in plans,
            "current plan baseline",
            task["task_id"],
        )
    completions = {row["operation_id"]: row for row in data["completion"]}
    _validate_completions(completions, tasks, bins)
    for row in data["completion"]:
        inputs.append(_input(BIN_NOTICE, row, row["work_line_id"], state=row["state"]))
    for row in data["rack_completion"]:
        _require(
            row["task_id"] in tasks and row["operation"] in RACK_NOTICES,
            "rack completion",
            row["operation_id"],
        )
        inputs.append(
            _input(
                row["operation"],
                row,
                tasks[row["task_id"]]["work_line_id"],
                state="APPLIED" if row["applied"] else "PENDING",
            )
        )
    for row in data["task"]:
        _require(
            (row["prepare_operation_id"] is None) == (row["prepare_message_id"] is None),
            "prepare",
            row["task_id"],
        )
        if row["prepare_operation_id"] is not None:
            requests.append(
                _request(
                    "PREPARE",
                    dict(
                        operation_id=row["prepare_operation_id"],
                        message_id=row["prepare_message_id"],
                    ),
                    row,
                    state="CONSUMED" if row["prepare_accepted"] else "REQUESTED",
                    decision_code="ACCEPTED" if row["prepare_accepted"] else None,
                    saved_at_ms=row["prepare_accepted_at_ms"],
                )
            )
    for row in data["task_confirmation"]:
        _require(row["task_id"] in tasks, "confirmation", row["operation_id"])
        code = _confirmation_code(row, row["state"])
        state = (
            "REQUESTED"
            if row["state"] == "PENDING"
            else "CONFLICT"
            if row["state"] == "CONFLICT"
            else "WAITING"
            if row["state"] == "BUSINESS_IN_PROGRESS"
            else "CONSUMED"
        )
        requests.append(
            _request(
                "COMPLETION_CONFIRM", row, tasks[row["task_id"]], state=state, decision_code=code
            )
        )
    for row in data["admission"]:
        work = bins.get(row["arrival_id"])
        _require(work is not None and work["task_id"] in tasks, "admission", row["operation_id"])
        _require(
            work["work_line_id"] == tasks[work["task_id"]]["work_line_id"],
            "admission line",
            row["operation_id"],
        )
        code = row["response"]["data"]["result"] if isinstance(row["response"], dict) else None
        requests.append(
            _request(
                "BIN_ADMISSION",
                row,
                tasks[work["task_id"]],
                state="REQUESTED" if code is None else "WAITING" if code == "WAIT" else "CONSUMED",
                decision_code=code,
            )
        )
    for row in data["rack"]:
        _require(
            row["task_id"] in tasks
            and row["work_line_id"] == tasks[row["task_id"]]["work_line_id"],
            "rack",
            row["id"],
        )
    for row in data["rack_action"]:
        _require(row["rack_work_id"] in racks, "rack action", row["id"])
    for row in data["task_member"]:
        _require(row["task_id"] in tasks, "member task", row["id"])
        _require((row["task_id"], row["plan_revision"]) in plans, "member plan baseline", row["id"])
        if row["rack_work_id"] is not None:
            _require(
                row["rack_work_id"] in racks
                and racks[row["rack_work_id"]]["task_id"] == row["task_id"],
                "member rack",
                row["id"],
            )
    reported_actions = set()
    for row in data["rack_report"]:
        _require(
            row["action_id"] not in reported_actions,
            "arrival report action",
            row["operation_id"] + ":" + row["action_id"],
        )
        reported_actions.add(row["action_id"])
        work, action = racks.get(row["rack_work_id"]), actions.get(row["action_id"])
        _require(
            work is not None and action is not None and action["rack_work_id"] == work["id"],
            "arrival report",
            row["operation_id"],
        )
        requests.append(
            _request(
                "RACK_ARRIVAL_REPORT",
                row,
                tasks[work["task_id"]],
                state="CONSUMED" if row["recorded"] else "REQUESTED",
            )
        )
    current_departures = {}
    for row in sorted(data["rack_departure"], key=lambda r: r["operation_id"]):
        work = racks.get(row["rack_work_id"])
        _require(work is not None, "departure", row["operation_id"])
        code = row["decision"]["result"] if isinstance(row["decision"], dict) else None
        requests.append(
            _request(
                "RACK_DEPARTURE",
                row,
                tasks[work["task_id"]],
                response=row["decision"],
                state="REQUESTED" if code is None else "WAITING" if code == "WAIT" else "CONSUMED",
                decision_code=code,
            )
        )
        current_departures[work["id"]] = row["operation_id"]
    by_request = {(r["kind"], r["operation_id"]): r for r in requests}
    _validate_prepares(tasks, requests)
    _require(len(by_request) == len(requests), "duplicate request", "operation")
    _require(
        len({(r["operation"], r["operation_id"]) for r in inputs}) == len(inputs),
        "duplicate input",
        "operation",
    )
    for row in data["task"]:
        if row["confirmation_operation_id"] is not None:
            current = by_request.get(("COMPLETION_CONFIRM", row["confirmation_operation_id"]))
            _require(
                current is not None and current["task_id"] == row["task_id"],
                "current confirmation",
                row["task_id"],
            )
    for row in data["bin"]:
        if row["completed_operation_id"] is not None:
            original = completions.get(row["completed_operation_id"])
            _require(
                original is not None
                and original["state"] == "APPLIED"
                and original["arrival_id"] == row["arrival_id"]
                and original["result"] == row["completed_result"]
                and all(
                    original[key] == row[key] for key in ("task_id", "work_line_id", "bin_code")
                ),
                "bin completion original",
                row["arrival_id"],
            )
        if row["task_id"] is not None:
            _require(
                row["task_id"] in tasks
                and tasks[row["task_id"]]["work_line_id"] == row["work_line_id"],
                "bin task",
                row["arrival_id"],
            )
        if row["admission_operation_id"] is not None:
            current = by_request.get(("BIN_ADMISSION", row["admission_operation_id"]))
            _require(
                current is not None and current["arrival_id"] == row["arrival_id"],
                "current admission",
                row["arrival_id"],
            )
    return inputs, requests, current_departures


def upgrade():
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    inputs, requests, departures = _converted(bind, schema)
    input_table, request_table = _targets(schema)
    input_table.create(bind)
    request_table.create(bind)
    if inputs:
        bind.execute(input_table.insert(), inputs)
    if requests:
        bind.execute(request_table.insert(), requests)
    _require(
        bind.scalar(sa.select(sa.func.count()).select_from(input_table)) == len(inputs),
        "input count",
        "copy",
    )
    _require(
        bind.scalar(sa.select(sa.func.count()).select_from(request_table)) == len(requests),
        "request count",
        "copy",
    )
    op.add_column(
        "smt_manual_rack",
        sa.Column("departure_operation_id", sa.String(), nullable=True),
        schema=schema,
    )
    rack = sa.Table("smt_manual_rack", sa.MetaData(), schema=schema, autoload_with=bind)
    for rack_id, operation_id in departures.items():
        bind.execute(
            rack.update().where(rack.c.id == rack_id).values(departure_operation_id=operation_id)
        )
    for name in reversed(REMOVED):
        op.drop_table("smt_manual_" + name, schema=schema)


def downgrade():
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    inputs, requests = _rows(bind, schema, "input"), _rows(bind, schema, "request")
    tasks = {r["task_id"]: r for r in _rows(bind, schema, "task")}
    bins = {r["arrival_id"]: r for r in _rows(bind, schema, "bin")}
    _validate_completions(
        {r["operation_id"]: r for r in inputs if r["operation"] == BIN_NOTICE}, tasks, bins
    )
    _validate_prepares(tasks, requests)
    for row in requests:
        if row["kind"] == "COMPLETION_CONFIRM" and row["state"] in ("WAITING", "CONSUMED"):
            code = row["decision_code"]
            _require(
                code in CONFIRMATION_CODES
                and (row["state"] == "WAITING") == (code == "BUSINESS_IN_PROGRESS"),
                "confirmation state",
                row["operation_id"],
            )
            _confirmation_code(row, code)
    for rack in _rows(bind, schema, "rack"):
        departures = [
            row
            for row in requests
            if row["kind"] == "RACK_DEPARTURE" and row["rack_work_id"] == rack["id"]
        ]
        maximum = max((row["operation_id"] for row in departures), default=None)
        _require(
            rack["departure_operation_id"] == maximum
            and all(
                row["task_id"] == rack["task_id"] and row["work_line_id"] == rack["work_line_id"]
                for row in departures
            ),
            "downgrade departure pointer",
            rack["id"],
        )
    _create_old(schema)
    targets = {
        name: sa.Table("smt_manual_" + name, sa.MetaData(), schema=schema, autoload_with=bind)
        for name in REMOVED
    }
    for table in targets.values():
        for column in table.c:
            if isinstance(column.type, sa.JSON):
                column.type.none_as_null = True
    for row in inputs:
        shared = dict(
            operation_id=row["operation_id"],
            task_id=row["task_id"],
            timestamp=row["timestamp"],
            data=row["payload"],
        )
        if row["operation"] == BIN_NOTICE:
            bind.execute(
                targets["completion"]
                .insert()
                .values(
                    **shared,
                    receipt_id=row["receipt_id"],
                    work_line_id=row["work_line_id"],
                    bin_code=row["bin_code"],
                    result=row["completion_result"],
                    completed_at=row["completed_at"],
                    state=row["state"],
                    arrival_id=row["arrival_id"],
                    reason=row["reason"],
                    original_operation_id=row["original_operation_id"],
                )
            )
        elif row["operation"] in RACK_NOTICES:
            bind.execute(
                targets["rack_completion"]
                .insert()
                .values(
                    **shared,
                    operation=row["operation"],
                    receipt_id=row["receipt_id"],
                    rack_id=row["rack_id"],
                    plan_revision=row["plan_revision"],
                    rack_face=row["rack_face"],
                    completed_at=row["completed_at"],
                    applied=row["state"] == "APPLIED",
                )
            )
        else:
            bind.execute(
                targets["task_input"]
                .insert()
                .values(
                    **shared,
                    operation=row["operation"],
                    applied=row["state"] == "APPLIED",
                    unapplied_reason=row["reason"],
                )
            )
            if row["is_plan_record"]:
                bind.execute(
                    targets["task_plan"]
                    .insert()
                    .values(
                        task_id=row["task_id"],
                        plan_revision=row["plan_revision"],
                        operation_id=row["operation_id"],
                        data=row["payload"],
                    )
                )
    for row in requests:
        shared = dict(operation_id=row["operation_id"], message_id=row["message_id"])
        if row["kind"] == "COMPLETION_CONFIRM":
            bind.execute(
                targets["task_confirmation"]
                .insert()
                .values(
                    **shared,
                    task_id=row["task_id"],
                    plan_revision=row["plan_revision"],
                    basis=row["basis"],
                    state="PENDING"
                    if row["state"] == "REQUESTED"
                    else "CONFLICT"
                    if row["state"] == "CONFLICT"
                    else row["decision_code"],
                    response=row["response"],
                    wait_until_ms=row["due_at_ms"],
                )
            )
        elif row["kind"] == "BIN_ADMISSION":
            bind.execute(
                targets["admission"]
                .insert()
                .values(
                    **shared,
                    arrival_id=row["arrival_id"],
                    response=row["response"],
                    saved_at_ms=row["saved_at_ms"],
                    wait_until_ms=row["due_at_ms"],
                )
            )
        elif row["kind"] == "RACK_ARRIVAL_REPORT":
            bind.execute(
                targets["rack_report"]
                .insert()
                .values(
                    **shared,
                    rack_work_id=row["rack_work_id"],
                    action_id=row["action_id"],
                    recorded=row["state"] == "CONSUMED",
                )
            )
        elif row["kind"] == "RACK_DEPARTURE":
            bind.execute(
                targets["rack_departure"]
                .insert()
                .values(
                    **shared,
                    rack_work_id=row["rack_work_id"],
                    request=row["request_snapshot"],
                    basis=row["basis"],
                    decision=row["response"],
                    wait_until_ms=row["due_at_ms"],
                )
            )
    op.drop_column("smt_manual_rack", "departure_operation_id", schema=schema)
    op.drop_table("smt_manual_request", schema=schema)
    op.drop_table("smt_manual_input", schema=schema)
