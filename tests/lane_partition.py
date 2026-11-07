"""Deterministic whole-file lanes; costs are setup heuristics, not measured seconds."""

from collections import defaultdict
from collections.abc import Iterable


def parse_lane(value: str) -> tuple[int, int]:
    lane, total = map(int, value.split("/"))
    if not 1 <= lane <= total:
        raise ValueError("test lane must be N/T with 1 <= N <= T")
    return lane, total


def partition_files(items: Iterable[tuple[str, Iterable[str]]], total: int) -> dict[str, int]:
    costs = defaultdict(int)
    for path, fixtures in items:
        fixtures = set(fixtures)
        cost = 2
        if "state_migration" in fixtures:
            cost += 16
        elif "schema_database" in fixtures:
            cost += 12
        elif path.startswith("tests/migrations/"):
            cost += 6
        if "postgres_tables" in fixtures:
            cost += 2
        if fixtures & {
            "manual_sessions",
            "roller_store",
            "coarse_scan",
            "auto_receiver",
            "reference",
        }:
            cost += 2
        if fixtures & {"callback_db", "ecs", "transport_rig", "pg_transport_rig", "storage"}:
            cost += 1
        if "admin" in fixtures:
            cost += 1
        if path.startswith("tests/tasks/test_smt_") or path == "tests/reliable/test_postgres.py":
            cost += 4
        costs[path] += cost
    loads, assigned = [0] * total, {}
    for path in sorted(costs, key=lambda path: (-costs[path], path)):
        lane = min(range(total), key=lambda lane: (loads[lane], lane))
        assigned[path] = lane + 1
        loads[lane] += costs[path]
    return assigned
