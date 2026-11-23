"""Co-locate original roller responsibilities without rewriting external intents.

Revision ID: binrollercompact
Revises: smtmanualcompact
"""

import sqlalchemy as sa
from alembic import op

revision = "binrollercompact"
down_revision = "smtmanualcompact"
branch_labels = None
depends_on = None

OLD = (
    "scan_arrival",
    "scan2_release",
    "return_round",
    "supply_demand",
    "face_service",
    "feed",
    "drain",
    "departure",
)
NEW = ("scan_arrival", "face_work", "decision")


def _create_new(schema):
    op.create_table(
        "bin_roller_scan_arrival",
        sa.Column("business_key", sa.String(length=64), nullable=False),
        sa.Column("first_digest", sa.String(length=64), nullable=False),
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
            "(point = 'SCAN2' AND delegation_state IS NOT NULL AND delegation_state IN "
            "('PENDING','ACCEPTED')) OR (point != 'SCAN2' AND delegation_state IS NULL)",
            name=op.f("bin_roller_scan_arrival_delegation_check"),
        ),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "business_key",
            name=op.f("bin_roller_scan_arrival_owner_domain_key"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_scan_arrival_pkey")),
        sa.CheckConstraint(
            "(release_record_id IS NULL AND release_call_key IS NULL AND release_state IS NULL "
            "AND release_command_code IS NULL AND release_consumed_result_id IS NULL) OR (point"
            " = 'SCAN2' AND release_record_id IS NOT NULL AND release_call_key IS NOT NULL AND "
            "release_state IS NOT NULL AND (release_state = 'PENDING' OR "
            "(release_state = 'CONSUMED' AND release_command_code IS NOT NULL "
            "AND release_command_code != '' AND release_consumed_result_id IS NOT NULL "
            "AND release_consumed_result_id != '')))",
            name=op.f("bin_roller_scan_arrival_release_check"),
        ),
        sa.CheckConstraint(
            "(round_id IS NULL AND return_state IS NULL AND removed IS NULL AND "
            "first_scan4_arrival_id IS NULL) OR (round_id IS NOT NULL AND ((point = 'SCAN3' AND"
            " bin_code IS NOT NULL AND return_state IS NOT NULL AND removed IS NOT NULL) OR "
            "(point = 'SCAN4' AND return_state IS NULL AND removed IS NULL AND "
            "first_scan4_arrival_id IS NULL)))",
            name=op.f("bin_roller_scan_arrival_round_check"),
        ),
        schema=schema,
    )
    op.create_index(
        "bin_roller_point_round",
        "bin_roller_scan_arrival",
        ["owner_domain", "work_line_id", "round_id", "point"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "bin_roller_release_record",
        "bin_roller_scan_arrival",
        ["release_record_id"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text("release_record_id IS NOT NULL"),
        postgresql_where=sa.text("release_record_id IS NOT NULL"),
    )
    op.create_index(
        "bin_roller_release_work",
        "bin_roller_scan_arrival",
        ["owner_domain", "work_line_id", "release_state", "release_record_id"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "bin_roller_round_root",
        "bin_roller_scan_arrival",
        ["round_id"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text("point = 'SCAN3' AND round_id IS NOT NULL"),
        postgresql_where=sa.text("point = 'SCAN3' AND round_id IS NOT NULL"),
    )
    op.create_table(
        "bin_roller_face_work",
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
            "(feed_id IS NULL AND feed_state IS NULL AND caller_workline_id IS NULL AND "
            "station_id IS NULL AND target IS NULL AND operation_id IS NULL AND message_id IS "
            "NULL AND manifest IS NULL) OR (feed_id IS NOT NULL AND service_id IS NOT NULL AND "
            "operation_id IS NOT NULL AND caller_workline_id IS NOT NULL AND target IS NOT NULL"
            " AND feed_state IS NOT NULL AND feed_state IN "
            "('REQUESTED','ALLOCATED','REVIEW','COMPLETED'))",
            name=op.f("bin_roller_face_work_feed_check"),
        ),
        sa.PrimaryKeyConstraint("purpose", "id", name=op.f("bin_roller_face_work_pkey")),
        sa.CheckConstraint(
            "(purpose = 'SUPPLY' AND demand_id IS NOT NULL AND id = demand_id AND task_id IS "
            "NOT NULL AND plan_revision IS NOT NULL AND change_id IS NOT NULL AND demand_state "
            "IS NOT NULL AND demand_state IN ('ACTIVE','WITHDRAWN')) OR (purpose = 'DRAIN' AND "
            "service_id IS NOT NULL AND id = service_id AND demand_id IS NULL AND demand_state "
            "IS NULL AND change_id IS NULL AND feed_id IS NULL)",
            name=op.f("bin_roller_face_work_purpose_check"),
        ),
        sa.CheckConstraint(
            "(service_id IS NULL AND source_ref IS NULL AND service_state IS NULL) OR "
            "(service_id IS NOT NULL AND cycle_id IS NOT NULL AND source_ref IS NOT NULL AND "
            "(purpose != 'SUPPLY' OR source_ref = demand_id) AND service_state IS NOT NULL AND "
            "service_state IN ('PENDING','READY','COMPLETED'))",
            name=op.f("bin_roller_face_work_service_check"),
        ),
        schema=schema,
    )
    op.create_index(
        "bin_roller_cycle_service",
        "bin_roller_face_work",
        ["owner_domain", "work_line_id", "cycle_id", "source_ref"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text("service_id IS NOT NULL"),
        postgresql_where=sa.text("service_id IS NOT NULL"),
    )
    op.create_index(
        "bin_roller_demand_id",
        "bin_roller_face_work",
        ["demand_id"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text("demand_id IS NOT NULL"),
        postgresql_where=sa.text("demand_id IS NOT NULL"),
    )
    op.create_index(
        "bin_roller_feed_id",
        "bin_roller_face_work",
        ["feed_id"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text("feed_id IS NOT NULL"),
        postgresql_where=sa.text("feed_id IS NOT NULL"),
    )
    op.create_index(
        "bin_roller_service_id",
        "bin_roller_face_work",
        ["service_id"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text("service_id IS NOT NULL"),
        postgresql_where=sa.text("service_id IS NOT NULL"),
    )
    op.create_index(
        "bin_roller_supply_face",
        "bin_roller_face_work",
        ["owner_domain", "work_line_id", "task_id", "plan_revision", "rack_id", "rack_face"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text("purpose = 'SUPPLY'"),
        postgresql_where=sa.text("purpose = 'SUPPLY'"),
    )
    op.create_table(
        "bin_roller_decision",
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("cycle_id", sa.String(), nullable=True),
        sa.Column("source_ref", sa.String(), nullable=True),
        sa.Column("input_kind", sa.String(), nullable=True),
        sa.Column("input_ref", sa.String(), nullable=True),
        sa.Column("input_snapshot", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("request", sa.JSON(none_as_null=True), nullable=True),
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
        sa.UniqueConstraint("kind", "operation_id", name=op.f("bin_roller_decision_kind_key")),
        sa.PrimaryKeyConstraint("kind", "id", name=op.f("bin_roller_decision_pkey")),
        sa.CheckConstraint(
            "(kind = 'DRAIN' AND input_kind IS NOT NULL AND input_ref IS NOT NULL AND "
            "unused_racks IS NOT NULL AND cycle_id IS NULL AND source_ref IS NULL) OR (kind = "
            "'DEPARTURE' AND input_kind IS NULL AND input_ref IS NULL AND input_snapshot IS "
            "NULL AND unused_racks IS NULL AND cycle_id IS NOT NULL AND source_ref IS NOT NULL "
            "AND request IS NOT NULL)",
            name=op.f("bin_roller_decision_subject_check"),
        ),
        schema=schema,
    )
    op.create_index(
        "bin_roller_departure_source",
        "bin_roller_decision",
        ["owner_domain", "work_line_id", "cycle_id", "source_ref"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text("kind = 'DEPARTURE'"),
        postgresql_where=sa.text("kind = 'DEPARTURE'"),
    )
    op.create_index(
        "bin_roller_drain_input",
        "bin_roller_decision",
        ["owner_domain", "work_line_id", "input_kind", "input_ref"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text("kind = 'DRAIN'"),
        postgresql_where=sa.text("kind = 'DRAIN'"),
    )


def _create_old(schema):
    op.create_table(
        "bin_roller_scan_arrival",
        sa.Column("business_key", sa.String(length=64), nullable=False),
        sa.Column("first_digest", sa.String(length=64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("point", sa.String(), nullable=False),
        sa.Column("device_code", sa.String(), nullable=False),
        sa.Column("raw_bin_code", sa.String(), nullable=False),
        sa.Column("bin_code", sa.String(), nullable=True),
        sa.Column("scanned_at", sa.BigInteger(), nullable=False),
        sa.Column("previous_arrival_id", sa.String(), nullable=True),
        sa.Column("separator_command_code", sa.String(), nullable=True),
        sa.Column("return_round_id", sa.String(), nullable=True),
        sa.Column("delegated", sa.Boolean(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("command_code", sa.String(), nullable=True),
        sa.Column("consumed_result_id", sa.String(), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_scan_arrival_pkey")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "business_key",
            name=op.f("bin_roller_scan_arrival_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "bin_roller_scan2_release",
        sa.Column("arrival_id", sa.String(), nullable=False),
        sa.Column("call_key", sa.String(), nullable=False),
        sa.Column("command_code", sa.String(), nullable=True),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("consumed_result_id", sa.String(), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_scan2_release_pkey")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "arrival_id",
            name=op.f("bin_roller_scan2_release_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "bin_roller_face_service",
        sa.Column("cycle_id", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("rack_face", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("plan_revision", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_face_service_pkey")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "cycle_id",
            "source_ref",
            name=op.f("bin_roller_face_service_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "bin_roller_supply_demand",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("change_id", sa.String(), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("rack_id", sa.String(), nullable=False),
        sa.Column("rack_face", sa.String(), nullable=False),
        sa.Column("cycle_id", sa.String(), nullable=True),
        sa.Column("service_id", sa.String(), nullable=True),
        sa.Column("withdrawn", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_supply_demand_pkey")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "task_id",
            "plan_revision",
            "rack_id",
            "rack_face",
            name=op.f("bin_roller_supply_demand_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "bin_roller_feed",
        sa.Column("service_id", sa.String(), nullable=False),
        sa.Column("caller_workline_id", sa.String(), nullable=False),
        sa.Column("station_id", sa.String(), nullable=True),
        sa.Column("target", sa.JSON(), nullable=False),
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("manifest", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("feed_complete", sa.Boolean(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_feed_pkey")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "service_id",
            name=op.f("bin_roller_feed_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "bin_roller_return_round",
        sa.Column("scan3_arrival_id", sa.String(), nullable=False),
        sa.Column("scan4_arrival_id", sa.String(), nullable=True),
        sa.Column("bin_code", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("removed", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_return_round_pkey")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "scan3_arrival_id",
            name=op.f("bin_roller_return_round_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "bin_roller_drain",
        sa.Column("input_kind", sa.String(), nullable=False),
        sa.Column("input_ref", sa.String(), nullable=False),
        sa.Column("input_snapshot", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("request", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("unused_racks", sa.JSON(), nullable=False),
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("decision", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("due_at", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_drain_pkey")),
        sa.UniqueConstraint("operation_id", name=op.f("bin_roller_drain_operation_id_key")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "input_kind",
            "input_ref",
            name=op.f("bin_roller_drain_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "bin_roller_departure",
        sa.Column("cycle_id", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("decision", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("due_at", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_departure_pkey")),
        sa.UniqueConstraint("operation_id", name=op.f("bin_roller_departure_operation_id_key")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "cycle_id",
            "source_ref",
            name=op.f("bin_roller_departure_owner_domain_key"),
        ),
        schema=schema,
    )


def _table(bind, schema, key):
    table = sa.Table("bin_roller_" + key, sa.MetaData(schema=schema), autoload_with=bind)
    for col in table.c:
        if isinstance(col.type, sa.JSON):
            col.type.none_as_null = True
    return table


def _rows(bind, schema, key):
    table = _table(bind, schema, key)
    json_cols = [c for c in table.c if isinstance(c.type, sa.JSON)]
    rows = []
    for result in bind.execute(
        sa.select(table, *(c.is_(None).label("sql_null_" + c.name) for c in json_cols))
    ).mappings():
        row = dict(result)
        for col in json_cols:
            sql_null = row.pop("sql_null_" + col.name)
            if not sql_null and row[col.name] is None:
                row[col.name] = sa.JSON.NULL
        rows.append(row)
    return rows


def _require(ok, subject, identity):
    if not ok:
        raise ValueError(f"BIN_Roller migration: invalid {subject} for original ID {identity}")


def _fields(row, *keys):
    return {key: row[key] for key in keys}


def _same_scope(left, right):
    return (left["owner_domain"], left["work_line_id"]) == (
        right["owner_domain"],
        right["work_line_id"],
    )


def _parent(index, row, column):
    parent = index.get(row[column])
    _require(parent is not None and _same_scope(parent, row), column, row["id"])
    return parent


SCOPE = ("owner_domain", "work_line_id")
FEED_FIELDS = (
    "caller_workline_id",
    "station_id",
    "target",
    "operation_id",
    "message_id",
    "manifest",
)
DECISION_FIELDS = (
    "operation_id",
    "message_id",
    "request",
    "decision",
    "due_at",
    "status",
    "pending",
)
DRAIN_FIELDS = ("input_kind", "input_ref", "input_snapshot", "unused_racks")
DEPARTURE_FIELDS = ("cycle_id", "source_ref")
ARRIVAL_FIELDS = (
    "id",
    *SCOPE,
    "business_key",
    "first_digest",
    "received_at",
    "point",
    "device_code",
    "raw_bin_code",
    "bin_code",
    "scanned_at",
    "previous_arrival_id",
    "separator_command_code",
    "pending",
    "command_code",
    "consumed_result_id",
)


def _require_rooted(predecessors, subject):
    # The caller has already resolved scoped, typed edges; omitted edges are verified roots.
    rooted = set()
    for original in predecessors:
        path = set()
        current = original
        while current in predecessors and current not in rooted:
            _require(current not in path, subject, current)
            path.add(current)
            current = predecessors[current]
        rooted.update(path)


def _compact(old, related):
    new = {key: [] for key in NEW}
    arrivals = {r["id"]: r for r in old["scan_arrival"]}
    rounds = {r["id"]: r for r in old["return_round"]}
    roots, releases = {}, {}
    first_scan4 = {}
    for arrival in arrivals.values():
        if arrival["point"] == "SCAN4" and arrival["return_round_id"] is not None:
            key = (arrival["owner_domain"], arrival["work_line_id"], arrival["return_round_id"])
            order = (arrival["received_at"], arrival["id"])
            if key not in first_scan4 or order < first_scan4[key]:
                first_scan4[key] = order
    for r in rounds.values():
        root = _parent(arrivals, r, "scan3_arrival_id")
        _require(
            root["id"] not in roots
            and root["point"] == "SCAN3"
            and root["bin_code"] == r["bin_code"]
            and root["return_round_id"] == r["id"],
            "SCAN3 root",
            r["id"],
        )
        first = first_scan4.get((r["owner_domain"], r["work_line_id"], r["id"]))
        _require(r["scan4_arrival_id"] == (first[1] if first else None), "first SCAN4", r["id"])
        roots[root["id"]] = r
    for r in old["scan2_release"]:
        a = _parent(arrivals, r, "arrival_id")
        _require(a["point"] == "SCAN2" and a["id"] not in releases, "SCAN2 release", r["id"])
        _require(
            r["pending"] or (r["command_code"] and r["consumed_result_id"]),
            "SCAN2 release consumption",
            r["id"],
        )
        releases[a["id"]] = r
    facts_by_digest = {row["digest"]: row for row in related["scan_fact"]}
    for fact in facts_by_digest.values():
        a = _parent(arrivals, fact, "arrival_id")
        _require(
            all(a[k] == fact[k] for k in ("point", "device_code", "bin_code", "raw_bin_code")),
            "scan fact",
            fact["id"],
        )
    scan_predecessors = {}
    for a in arrivals.values():
        first_fact = facts_by_digest.get(a["first_digest"])
        _require(
            first_fact and _same_scope(a, first_fact) and first_fact["arrival_id"] == a["id"],
            "first scan digest",
            a["id"],
        )
        root, release = roots.get(a["id"]), releases.get(a["id"])
        if a["return_round_id"] is not None:
            linked = _parent(rounds, a, "return_round_id")
            _require(
                (a["point"] == "SCAN4" or root == linked) and a["bin_code"] == linked["bin_code"],
                "round link",
                a["id"],
            )
        if a["previous_arrival_id"] is not None:
            _require(
                _parent(arrivals, a, "previous_arrival_id")["point"] == a["point"],
                "scan predecessor",
                a["id"],
            )
            scan_predecessors[a["id"]] = a["previous_arrival_id"]
        new["scan_arrival"].append(
            dict(
                **_fields(a, *ARRIVAL_FIELDS),
                round_id=a["return_round_id"],
                return_state=root["status"] if root else None,
                removed=root["removed"] if root else None,
                first_scan4_arrival_id=root["scan4_arrival_id"] if root else None,
                delegation_state=("ACCEPTED" if a["delegated"] else "PENDING")
                if a["point"] == "SCAN2"
                else None,
                release_record_id=release["id"] if release else None,
                release_call_key=release["call_key"] if release else None,
                release_command_code=release["command_code"] if release else None,
                release_consumed_result_id=release["consumed_result_id"] if release else None,
                release_state=("PENDING" if release["pending"] else "CONSUMED")
                if release
                else None,
            )
        )
    _require_rooted(scan_predecessors, "scan predecessor")
    cycles = {r["id"]: r for r in related["rack_cycle"]}
    services = {r["id"]: r for r in old["face_service"]}
    feeds = {r["id"]: r for r in old["feed"]}
    segments = {}
    for segment in related["feed_segment"]:
        feed = _parent(feeds, segment, "feed_id")
        latest = segments.get(feed["id"])
        if latest is None or latest["segment_index"] < segment["segment_index"]:
            segments[feed["id"]] = segment
    by_service = {}
    for f in feeds.values():
        svc = _parent(services, f, "service_id")
        _require(
            svc["id"] not in by_service and f["feed_complete"] != f["pending"], "feed", f["id"]
        )
        by_service[svc["id"]] = f
    used = set()
    for d in old["supply_demand"]:
        if d["cycle_id"] is not None:
            cy = _parent(cycles, d, "cycle_id")
            _require(
                cy["purpose"] == "ORDINARY"
                and cy["task_id"] == d["task_id"]
                and cy["rack_id"] == d["rack_id"],
                "supply cycle",
                d["id"],
            )
        row = dict(
            **_fields(
                d,
                "id",
                *SCOPE,
                "task_id",
                "rack_face",
                "rack_id",
                "plan_revision",
                "change_id",
                "cycle_id",
            ),
            purpose="SUPPLY",
            demand_id=d["id"],
            demand_state="WITHDRAWN" if d["withdrawn"] else "ACTIVE",
        )
        if d["service_id"] is not None:
            svc = _parent(services, d, "service_id")
            _require(
                svc["id"] not in used
                and svc["source_ref"] == d["id"]
                and all(
                    svc[k] == d[k] for k in ("cycle_id", "rack_face", "task_id", "plan_revision")
                ),
                "supply face",
                d["id"],
            )
            used.add(svc["id"])
            row.update(
                service_id=svc["id"], service_state=svc["status"], source_ref=svc["source_ref"]
            )
            f = by_service.get(svc["id"])
            if f:
                latest = segments.get(f["id"])
                row.update(
                    _fields(f, *FEED_FIELDS),
                    feed_id=f["id"],
                    feed_state=(
                        "COMPLETED"
                        if f["feed_complete"]
                        else "REVIEW"
                        if latest and latest["status"] in {"FAILED", "UNKNOWN"}
                        else "REQUESTED"
                        if f["manifest"] in (None, sa.JSON.NULL)
                        else "ALLOCATED"
                    ),
                )
        new["face_work"].append(row)
    for svc in services.values():
        _require(
            svc["status"] in {"PENDING", "READY", "COMPLETED"}
            and svc["pending"] == (svc["status"] != "COMPLETED"),
            "face state",
            svc["id"],
        )
        if svc["id"] in used:
            continue
        cy = _parent(cycles, svc, "cycle_id")
        _require(
            cy["purpose"] == "DRAIN" and svc["id"] not in by_service,
            "unmatched face service",
            svc["id"],
        )
        new["face_work"].append(
            dict(
                **_fields(
                    svc,
                    "id",
                    *SCOPE,
                    "task_id",
                    "plan_revision",
                    "source_ref",
                    "cycle_id",
                    "rack_face",
                ),
                rack_id=cy["rack_id"],
                purpose="DRAIN",
                service_id=svc["id"],
                service_state=svc["status"],
            )
        )
    drains = {r["id"]: r for r in old["drain"]}
    departures = {r["id"]: r for r in old["departure"]}
    demands = {r["id"]: r for r in old["supply_demand"]}
    for cy in cycles.values():
        if cy["purpose"] == "DRAIN":
            _parent(drains, cy, "source_ref")
        else:
            demand = _parent(demands, cy, "source_ref")
            _require(
                cy["purpose"] == "ORDINARY"
                and cy["task_id"] == demand["task_id"]
                and cy["rack_id"] == demand["rack_id"],
                "cycle origin",
                cy["id"],
            )
    actions = {r["id"]: r for r in related["rack_action"]}
    for position in related["position_fact"]:
        if position["source_kind"] == "SCAN":
            _parent(arrivals, position, "source_id")
        elif position["source_kind"] == "RACK":
            _parent(actions, position, "source_id")
    for action in actions.values():
        cy = _parent(cycles, action, "cycle_id")
        if action["kind"] == "CTU03":
            original = _parent(departures, action, "source_ref")
        elif action["source_ref"] == cy["id"] and action["kind"] == "CTU01":
            continue
        else:
            original = _parent(services, action, "source_ref")
        _require(original["cycle_id"] == cy["id"], "rack action origin", action["id"])
    drain_predecessors = {}
    for r in drains.values():
        if r["input_kind"] == "FIFO_OBSERVATION":
            _require(r["input_ref"] == r["id"], "drain observation root", r["id"])
        else:
            _require(r["input_kind"] in {"WAIT", "COMPLETED"}, "drain origin", r["id"])
            drain_predecessors[r["id"]] = _parent(drains, r, "input_ref")["id"]
        snapshot = r["input_snapshot"]
        if isinstance(snapshot, dict) and "round_id" in snapshot:
            rid = snapshot["round_id"]
            original = rounds.get(rid) if isinstance(rid, str) else None
            _require(original and _same_scope(original, r), "drain FIFO observation", r["id"])
    _require_rooted(drain_predecessors, "drain predecessor")
    departure_predecessors = {}
    for r in departures.values():
        _parent(cycles, r, "cycle_id")
        ref = r["source_ref"]
        service = services.get(ref)
        # Initial departure roots are typed; their IDs can equal an unrelated decision ID.
        if ref == r["cycle_id"] or (
            service and _same_scope(r, service) and service["cycle_id"] == r["cycle_id"]
        ):
            continue
        previous = _parent(departures, r, "source_ref")
        _require(previous["cycle_id"] == r["cycle_id"], "departure predecessor", r["id"])
        departure_predecessors[r["id"]] = previous["id"]
    _require_rooted(departure_predecessors, "departure predecessor")
    segment_ids = {row["id"]: row for row in related["feed_segment"]}
    opportunities = {row["id"]: row for row in related["return_opportunity"]}
    for opportunity in opportunities.values():
        service = _parent(services, opportunity, "service_id")
        ref = opportunity["source_ref"]
        if opportunity["kind"] == "PURE_RETURN":
            _require(ref == service["id"], "pure return face", opportunity["id"])
        else:
            source_kind, _, source_id = ref.partition(":")
            if source_kind == "SEGMENT":
                segment = segment_ids.get(source_id)
                _require(
                    segment and _same_scope(segment, opportunity),
                    "opportunity segment",
                    opportunity["id"],
                )
                feed = feeds.get(segment["feed_id"])
            else:
                feed = feeds.get(source_id) if source_kind == "FEED" else None
            _require(
                feed and _same_scope(feed, opportunity) and feed["service_id"] == service["id"],
                "opportunity feed",
                opportunity["id"],
            )
    batches = {row["id"]: row for row in related["return_batch"]}
    batch_predecessors = {}
    for batch in batches.values():
        opportunity = _parent(opportunities, batch, "opportunity_id")
        if batch["source_ref"] != opportunity["id"]:
            predecessor = _parent(batches, batch, "source_ref")
            _require(
                predecessor["opportunity_id"] == opportunity["id"],
                "batch predecessor",
                batch["id"],
            )
            batch_predecessors[batch["id"]] = predecessor["id"]
        _require(isinstance(batch["members"], list), "batch members", batch["id"])
        for member in batch["members"]:
            rid = member.get("round_id") if isinstance(member, dict) else None
            root = rounds.get(rid) if isinstance(rid, str) else None
            _require(
                root and _same_scope(root, batch) and root["bin_code"] == member.get("bin_code"),
                "batch member",
                batch["id"],
            )
    _require_rooted(batch_predecessors, "batch predecessor")
    for key in ("drain", "departure"):
        new["decision"].extend(dict(r, kind=key.upper()) for r in old[key])
    return new


def _expand(new):
    old = {key: [] for key in OLD}
    for a in new["scan_arrival"]:
        old["scan_arrival"].append(
            dict(
                **_fields(a, *ARRIVAL_FIELDS),
                return_round_id=a["round_id"],
                delegated=a["delegation_state"] == "ACCEPTED",
            )
        )
        if a["release_record_id"] is not None:
            old["scan2_release"].append(
                dict(
                    **_fields(a, *SCOPE),
                    id=a["release_record_id"],
                    arrival_id=a["id"],
                    call_key=a["release_call_key"],
                    command_code=a["release_command_code"],
                    pending=a["release_state"] == "PENDING",
                    consumed_result_id=a["release_consumed_result_id"],
                )
            )
        if a["point"] == "SCAN3" and a["round_id"] is not None:
            old["return_round"].append(
                dict(
                    **_fields(a, *SCOPE, "bin_code", "removed"),
                    id=a["round_id"],
                    scan3_arrival_id=a["id"],
                    scan4_arrival_id=a["first_scan4_arrival_id"],
                    status=a["return_state"],
                )
            )
    for row in new["face_work"]:
        if row["purpose"] == "SUPPLY":
            old["supply_demand"].append(
                dict(
                    **_fields(
                        row,
                        *SCOPE,
                        "task_id",
                        "change_id",
                        "rack_id",
                        "rack_face",
                        "plan_revision",
                        "cycle_id",
                    ),
                    id=row["demand_id"],
                    service_id=row.get("service_id"),
                    withdrawn=row["demand_state"] == "WITHDRAWN",
                )
            )
        if row.get("service_id") is not None:
            old["face_service"].append(
                dict(
                    **_fields(
                        row,
                        *SCOPE,
                        "task_id",
                        "plan_revision",
                        "rack_face",
                        "cycle_id",
                        "source_ref",
                    ),
                    id=row["service_id"],
                    status=row["service_state"],
                    pending=row["service_state"] != "COMPLETED",
                )
            )
        if row.get("feed_id") is not None:
            old["feed"].append(
                dict(
                    **_fields(row, *SCOPE, *FEED_FIELDS, "service_id"),
                    id=row["feed_id"],
                    pending=row["feed_state"] != "COMPLETED",
                    feed_complete=row["feed_state"] == "COMPLETED",
                )
            )
    for row in new["decision"]:
        drain = row["kind"] == "DRAIN"
        old["drain" if drain else "departure"].append(
            _fields(
                row, "id", *SCOPE, *DECISION_FIELDS, *(DRAIN_FIELDS if drain else DEPARTURE_FIELDS)
            )
        )
    return old


def _verify(original, restored):
    for key, rows in original.items():
        discriminator = {"face_work": "purpose", "decision": "kind"}.get(key)
        indexed = {(r.get(discriminator), r["id"]): r for r in restored[key]}
        _require(len(rows) == len(restored[key]) == len(indexed), key + " count", key)
        for expected in rows:
            row = indexed.get((expected.get(discriminator), expected["id"]))
            _require(
                row is not None
                and all(expected.get(c) == row.get(c) for c in expected.keys() | row.keys()),
                key,
                expected["id"],
            )


def _related(bind, schema):
    return {
        k: _rows(bind, schema, k)
        for k in (
            "scan_fact",
            "position_fact",
            "rack_cycle",
            "rack_action",
            "feed_segment",
            "return_opportunity",
            "return_batch",
        )
    }


def _replace(bind, schema, removed, create, data):
    for key in removed:
        op.drop_table("bin_roller_" + key, schema=schema)
    create(schema)
    for key, rows in data.items():
        t = _table(bind, schema, key)
        if rows:
            bind.execute(
                t.insert(), [{col.name: r.get(col.name) for col in t.columns} for r in rows]
            )


def upgrade():
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    old = {key: _rows(bind, schema, key) for key in OLD}
    new = _compact(old, _related(bind, schema))
    _verify(old, _expand(new))
    _replace(bind, schema, OLD, _create_new, new)
    _verify(old, _expand({key: _rows(bind, schema, key) for key in NEW}))


def downgrade():
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    new = {key: _rows(bind, schema, key) for key in NEW}
    old = _expand(new)
    _verify(new, _compact(old, _related(bind, schema)))
    _replace(bind, schema, NEW, _create_old, old)
    _verify(old, {key: _rows(bind, schema, key) for key in OLD})
