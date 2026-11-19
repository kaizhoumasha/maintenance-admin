"""Whole-file lanes. Measured seconds override setup-cost estimates."""

import json
import os
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping
from fnmatch import fnmatch
from pathlib import Path
from xml.etree import ElementTree

DEFAULT_DURATIONS = Path(__file__).with_name("lane_durations.json")


def parse_lane(value: str) -> tuple[int, int]:
    lane, total = map(int, value.split("/"))
    if not 1 <= lane <= total:
        raise ValueError("test lane must be N/T with 1 <= N <= T")
    return lane, total


def durations_path() -> Path | None:
    override = os.environ.get("LANE_DURATIONS_FILE")
    if override and Path(override).is_file():
        return Path(override)
    return DEFAULT_DURATIONS if DEFAULT_DURATIONS.is_file() else None


def load_durations(path: Path | None = None) -> dict[str, float]:
    selected = durations_path() if path is None else path
    if selected is None or not Path(selected).is_file():
        return {}
    data = json.loads(Path(selected).read_text())
    if not isinstance(data, dict):
        raise ValueError("lane durations must be a JSON object")
    loaded = {}
    for key, value in data.items():
        if (
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, int | float)
        ):
            raise ValueError("lane durations must map file paths to numbers")
        if value < 0:
            raise ValueError("lane durations must not be negative")
        loaded[key] = float(value)
    return loaded


def durations_from_junit(reports: Iterable[Path]) -> dict[str, float]:
    totals = defaultdict(float)
    for report in reports:
        for case in ElementTree.parse(report).getroot().iter("testcase"):
            file = _junit_file(case.get("file"), case.get("classname"))
            if file is not None:
                totals[file] += float(case.get("time", "0"))
    return dict(totals)


def write_durations(path: Path, durations: Mapping[str, float]) -> None:
    payload = {key: round(float(durations[key]), 3) for key in sorted(durations)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def assignment_from_durations(
    paths: Iterable[str], durations: Mapping[str, float], total: int
) -> dict[str, int] | None:
    """Return lanes when every path has a duration. Missing data keeps collection unchanged."""
    unique = list(dict.fromkeys(paths))
    if not unique:
        return {}
    if any(path not in durations or durations[path] < 0 for path in unique):
        return None
    return _assign({path: round(float(durations[path]), 3) for path in unique}, total)


def test_files(
    root: Path,
    starts: Iterable[Path],
    patterns: Iterable[str],
    norecurse: Iterable[str],
) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        if path.suffix != ".py" or not any(fnmatch(path.name, pattern) for pattern in patterns):
            return
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            return
        if relative not in seen:
            seen.add(relative)
            found.append(relative)

    def walk(directory: Path, *, include_self: bool) -> None:
        skipped = directory.name == "__pycache__" or any(
            fnmatch(directory.name, pattern) for pattern in norecurse
        )
        if not include_self and skipped:
            return
        if directory.is_file():
            add(directory)
            return
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            if child.is_symlink():
                continue
            if child.is_dir():
                walk(child, include_self=False)
            else:
                add(child)

    for start in starts:
        if start.exists():
            walk(start, include_self=True)
    return found


def duplicate_junit_ids(reports: Iterable[Path]) -> list[str]:
    seen: set[tuple[str, str, str]] = set()
    duplicates: list[str] = []
    for report in reports:
        for case in ElementTree.parse(report).getroot().iter("testcase"):
            identity = (
                _junit_file(case.get("file"), case.get("classname")) or "",
                case.get("classname") or "",
                case.get("name") or "",
            )
            if identity in seen:
                duplicates.append("::".join(identity))
            else:
                seen.add(identity)
    return duplicates


def partition_files(
    items: Iterable[tuple[str, Iterable[str]]],
    total: int,
    durations: Mapping[str, float] | None = None,
) -> dict[str, int]:
    costs = _balanced_costs(_heuristic_costs(items), durations or {})
    return _assign(costs, total)


def _assign(costs: Mapping[str, float], total: int) -> dict[str, int]:
    loads, assigned = [0.0] * total, {}
    for path in sorted(costs, key=lambda path: (-costs[path], path)):
        lane = min(range(total), key=lambda lane: (loads[lane], lane))
        assigned[path] = lane + 1
        loads[lane] += costs[path]
    return assigned


def _junit_file(file: str | None, classname: str | None = None) -> str | None:
    if not file:
        if not classname:
            return None
        candidate = Path(classname.replace(".", "/") + ".py")
        if not (Path.cwd() / candidate).is_file():
            return None
        return candidate.as_posix()
    path = Path(file)
    if path.is_absolute():
        try:
            path = path.relative_to(Path.cwd())
        except ValueError:
            return None
    return path.as_posix()


def _heuristic_costs(items: Iterable[tuple[str, Iterable[str]]]) -> dict[str, int]:
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
    return dict(costs)


def _balanced_costs(
    heuristic: Mapping[str, int], durations: Mapping[str, float]
) -> Mapping[str, float]:
    if not durations:
        return heuristic
    ratios = [
        durations[path] / base
        for path, base in heuristic.items()
        if path in durations and base > 0 and durations[path] >= 0
    ]
    scale = statistics.median(ratios) if ratios else 1.0
    costs = {}
    for path, base in heuristic.items():
        if path in durations and durations[path] >= 0:
            costs[path] = round(float(durations[path]), 3)
        else:
            costs[path] = round(base * scale, 3)
    return costs
