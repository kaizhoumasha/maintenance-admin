"""Move the configured request window to positions without changing original intents."""

import json
from collections import defaultdict

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "positioncapacity"
down_revision = "sharedrackdispatch"
branch_labels = None
depends_on = None

CAPACITY_CHECK = (
    "capacity IS NULL OR (capacity BETWEEN 1 AND 2147483647 "
    "AND capacity = CAST(capacity AS INTEGER))"
)
ORDER_CHECK = (
    "(admission_order IS NULL OR admission_order > 0) "
    "AND (task_id IS NOT NULL OR admission_order IS NULL)"
)
OLD_CHECK = "work_lines_ck_work_lines_five_rack_request_limit_check"


def _tables(bind, schema):
    metadata = sa.MetaData(schema=schema)
    names = (
        "work_lines",
        "rack_dispatch_job",
        "rack_dispatch_action",
        "transport_task",
        "smt_manual_task",
        "smt_manual_task_member",
        "smt_manual_input",
        "bin_roller_face_work",
    )
    return {name: sa.Table(name, metadata, autoload_with=bind) for name in names}


def _object(value):
    return json.loads(value) if isinstance(value, str) else value or {}


def _fail(message):
    raise CommandError("capacity preflight: " + message)


def _preflight(bind, tables):
    from app.businesses import catalog

    definitions = {
        d.code: {p.slot_key: p.position_type for p in d.position_slots}
        for d in catalog.definitions()
    }
    lines = {r["id"]: r for r in bind.execute(sa.select(tables["work_lines"])).mappings()}
    positions, owners = {}, {}
    for line in lines.values():
        bindings = _object(line["position_bindings"])
        types = definitions.get(line["business_code"], {})
        racks = {v for k, v in bindings.items() if types.get(k) == "RACK_POSITION"}
        handoffs = {v for k, v in bindings.items() if types.get(k) == "HANDOFF_POSITION"}
        if racks & handoffs or bindings.keys() - types.keys():
            _fail(f"unresolved position types on line {line['id']}")
        five = bindings.get("FIVE_RACK")
        if line["five_rack_request_limit"] is not None and not five:
            _fail(f"unattributed five rack limit on line {line['id']}")
        for point in racks:
            positions[line["id"], point] = line["five_rack_request_limit"] if point == five else 1
    jobs = list(bind.execute(sa.select(tables["rack_dispatch_job"])).mappings())
    actions = defaultdict(list)
    for action in bind.execute(sa.select(tables["rack_dispatch_action"])).mappings():
        actions[action["job_id"]].append(action)
    transports = {
        r["transport_task_id"]: r
        for r in bind.execute(sa.select(tables["transport_task"])).mappings()
    }
    tasks = {r["task_id"]: r for r in bind.execute(sa.select(tables["smt_manual_task"])).mappings()}
    members = list(bind.execute(sa.select(tables["smt_manual_task_member"])).mappings())
    faces = {
        r["demand_id"]: r
        for r in bind.execute(sa.select(tables["bin_roller_face_work"])).mappings()
        if r["demand_id"]
    }
    groups = defaultdict(list)
    for job in jobs:
        task = job["task_id"]
        if (job["source_kind"] == "ROLLER_DRAIN") != (task is None):
            _fail(f"unproved task attribution for job {job['id']}")
        point = job["point"]
        if job["pending"] and point:
            if job["work_line_id"] not in lines:
                _fail(f"missing original workline for job {job['id']}")
            positions.setdefault((job["work_line_id"], point), 1)
        if not task or not point:
            continue
        original = tasks.get(task)
        if not original or original["work_line_id"] != job["work_line_id"]:
            _fail(f"unproved original task for job {job['id']}")
        entries = [a for a in actions[job["id"]] if a["kind"] == "ENTER" and a["transport_task_id"]]
        if len(entries) > 1:
            _fail(f"ambiguous original entry for job {job['id']}")
        key = None
        if entries:
            move = entries[0]
            transport = transports.get(move["transport_task_id"])
            if (
                not transport
                or transport["client_request_id"] != move["client_request_id"]
                or transport["cycle_id"] != job["original_cycle_id"]
            ):
                _fail(f"unproved original transport for job {job['id']}")
            # Original database registration sequence, never a UUID ordering.
            key = (0, transport["id"], 0)
        elif job["source_kind"] == "MANUAL_TRANSFER" and original["target_rack"]:
            key = (1, 1, -1)
        else:
            selected = [
                m for m in members if m["task_id"] == task and m["rack_work_id"] == job["id"]
            ]
            if job["source_kind"] == "ROLLER_ORDINARY":
                face = faces.get(job["source_ref"])
                if face:
                    selected = [
                        m
                        for m in members
                        if m["task_id"] == task
                        and m["kind"] == "BIN"
                        and m["plan_revision"] == face["plan_revision"]
                        and (m["rack_id"], m["rack_face"]) == (face["rack_id"], face["rack_face"])
                    ]
            if selected:
                key = (1, *min((m["plan_revision"], m["member_order"]) for m in selected))
        groups[job["work_line_id"], task, point].append((job["id"], key))
    orders = {}
    for scope, group in groups.items():
        if len(group) == 1:
            orders[group[0][0]] = 1
            continue
        keys = [key for _, key in group]
        if None in keys or len(set(keys)) != len(keys):
            _fail(f"unproved admission order for line/task/point {scope}")
        orders.update(
            {job: n for n, (job, _) in enumerate(sorted(group, key=lambda pair: pair[1]), 1)}
        )
    for line, point in positions:
        if point in owners and owners[point] != line:
            _fail(f"physical point shared by lines {owners[point]} and {line}")
        owners[point] = line
    return positions, orders


def upgrade():
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    if schema:
        bind.exec_driver_sql(
            "LOCK TABLE enterprise_backend.work_lines, "
            "enterprise_backend.rack_dispatch_job, "
            "enterprise_backend.rack_dispatch_action IN ACCESS EXCLUSIVE MODE"
        )
    tables = _tables(bind, schema)
    positions, orders = _preflight(bind, tables)
    prefix = f"{schema}." if schema else ""
    op.create_table(
        "workline_positions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "work_line_id",
            sa.Integer(),
            sa.ForeignKey(prefix + "work_lines.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("position_code", sa.String(100), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=True),
        sa.UniqueConstraint("work_line_id", "position_code"),
        sa.UniqueConstraint("position_code"),
        sa.CheckConstraint(CAPACITY_CHECK, name=op.f("workline_positions_capacity_check")),
        schema=schema,
        sqlite_autoincrement=True,
    )
    position = sa.Table("workline_positions", sa.MetaData(), autoload_with=bind, schema=schema)
    if positions:
        bind.execute(
            position.insert(),
            [
                dict(work_line_id=line, position_code=point, capacity=limit)
                for (line, point), limit in sorted(positions.items())
            ],
        )
    op.add_column(
        "rack_dispatch_job",
        sa.Column(
            "admission_order",
            sa.BigInteger(),
            sa.CheckConstraint(ORDER_CHECK, name=op.f("rack_dispatch_job_admission_order_check")),
            nullable=True,
        ),
        schema=schema,
    )
    jobs = sa.table(
        "rack_dispatch_job", sa.column("id"), sa.column("admission_order"), schema=schema
    )
    for job, order in orders.items():
        bind.execute(jobs.update().where(jobs.c.id == job).values(admission_order=order))
    predicate = sa.text("task_id IS NOT NULL AND admission_order IS NOT NULL")
    op.create_index(
        "rack_dispatch_job_admission_order",
        "rack_dispatch_job",
        ["work_line_id", "task_id", "point", "admission_order"],
        unique=True,
        schema=schema,
        sqlite_where=predicate,
        postgresql_where=predicate,
    )
    op.create_index(
        "rack_dispatch_job_admission_scan",
        "rack_dispatch_job",
        ["work_line_id", "task_id", "point", "pending", "admission_order"],
        schema=schema,
    )
    # Native DROP preserves SQLite identity high water; remove the old named column check first.
    if schema:
        op.drop_constraint(op.f(OLD_CHECK), "work_lines", type_="check", schema=schema)
        op.drop_column("work_lines", "five_rack_request_limit", schema=schema)
    else:
        sequence = bind.scalar(sa.text("SELECT seq FROM sqlite_sequence WHERE name='work_lines'"))
        with op.batch_alter_table(
            "work_lines", recreate="always", table_kwargs={"sqlite_autoincrement": True}
        ) as batch:
            batch.drop_constraint(op.f(OLD_CHECK), type_="check")
            batch.drop_column("five_rack_request_limit")
        if sequence is not None:
            bind.execute(
                sa.text("UPDATE sqlite_sequence SET seq=MAX(seq,:seq) WHERE name='work_lines'"),
                {"seq": sequence},
            )


def downgrade():
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    if schema:
        bind.exec_driver_sql(
            "LOCK TABLE enterprise_backend.work_lines, "
            "enterprise_backend.workline_positions, "
            "enterprise_backend.rack_dispatch_job IN ACCESS EXCLUSIVE MODE"
        )
    metadata = sa.MetaData(schema=schema)
    lines = sa.Table("work_lines", metadata, autoload_with=bind)
    positions = sa.Table("workline_positions", metadata, autoload_with=bind)
    jobs = sa.Table("rack_dispatch_job", metadata, autoload_with=bind)
    if bind.scalar(sa.select(jobs.c.id).where(jobs.c.admission_order.is_not(None)).limit(1)):
        _fail("admission orders exist; refusing lossy downgrade")
    original = {
        r["id"]: _object(r["position_bindings"]).get("FIVE_RACK")
        for r in bind.execute(sa.select(lines)).mappings()
    }
    limits = {}
    for row in bind.execute(sa.select(positions)).mappings():
        if row["position_code"] == original.get(row["work_line_id"]):
            limits[row["work_line_id"]] = row["capacity"]
        elif row["capacity"] != 1:
            _fail("non-five capacity cannot be represented by old model")
    op.add_column(
        "work_lines",
        sa.Column(
            "five_rack_request_limit",
            sa.Integer(),
            sa.CheckConstraint(
                "five_rack_request_limit BETWEEN 1 AND 2147483647 AND "
                "five_rack_request_limit=CAST(five_rack_request_limit AS INTEGER)",
                name=op.f(OLD_CHECK),
            ),
            nullable=True,
        ),
        schema=schema,
    )
    old = sa.table(
        "work_lines", sa.column("id"), sa.column("five_rack_request_limit"), schema=schema
    )
    for line, limit in limits.items():
        bind.execute(old.update().where(old.c.id == line).values(five_rack_request_limit=limit))
    op.drop_index("rack_dispatch_job_admission_scan", table_name="rack_dispatch_job", schema=schema)
    op.drop_index(
        "rack_dispatch_job_admission_order", table_name="rack_dispatch_job", schema=schema
    )
    if schema:
        op.drop_constraint(
            op.f("rack_dispatch_job_admission_order_check"),
            "rack_dispatch_job",
            type_="check",
            schema=schema,
        )
        op.drop_column("rack_dispatch_job", "admission_order", schema=schema)
    else:
        with op.batch_alter_table("rack_dispatch_job") as batch:
            batch.drop_constraint(op.f("rack_dispatch_job_admission_order_check"), type_="check")
            batch.drop_column("admission_order")
    op.drop_table("workline_positions", schema=schema)
