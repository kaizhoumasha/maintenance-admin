import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_partition_keeps_every_file_together_and_is_order_independent():
    from tests.lane_partition import partition_files

    items = [
        (f"tests/test_future_{file}.py", ("state_migration",) if file < 2 else ())
        for file in range(9)
        for _ in range(file + 1)
    ]
    assigned = partition_files(items, 6)
    assert set(assigned) == {file for file, _ in items}
    assert all(1 <= lane <= 6 for lane in assigned.values())
    assert assigned == partition_files(reversed(items), 6)
    assert set(partition_files(items, 1).values()) == {1}
    assert partition_files([], 6) == {}


def test_partition_spreads_expensive_files_before_cheaper_cases():
    from tests.lane_partition import partition_files

    items = [(f"tests/test_heavy_{i}.py", ("state_migration",)) for i in range(2)]
    items += [("tests/test_cheap.py", ())] * 4
    assigned = partition_files(items, 2)
    assert assigned["tests/test_heavy_0.py"] != assigned["tests/test_heavy_1.py"]


@pytest.mark.parametrize("value", ["0/6", "7/6", "1/0", "-1/6", "1/-6", "1", "a/6", "1/2/3"])
def test_lane_rejects_invalid_bounds_or_shape(value):
    from tests.lane_partition import parse_lane

    with pytest.raises(ValueError):
        parse_lane(value)


def test_collection_lanes_cover_full_suite_once_and_keep_local_order(tmp_path):
    root = Path(__file__).parents[1]
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    for name in ("alpha", "beta", "gamma", "delta"):
        (tmp_path / f"test_{name}.py").write_text(
            "def test_first(): pass\ndef test_second(): pass\n"
        )

    def collect(*args):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "tests.conftest",
                "--collect-only",
                "-q",
                *args,
            ],
            cwd=tmp_path,
            env={
                **{key: value for key, value in os.environ.items() if key != "LANE_DURATIONS_FILE"},
                "PYTHONPATH": str(root),
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            },
            capture_output=True,
            text=True,
            check=True,
        )
        if args:
            assert "deselected" in result.stdout
        return [line for line in result.stdout.splitlines() if "::test_" in line]

    full = collect()
    lanes = [collect(f"--test-lane={lane}/3") for lane in range(1, 4)]
    assert len(full) == 8
    assert sorted(node for lane in lanes for node in lane) == sorted(full)
    for lane in lanes:
        assert lane == [node for node in full if node in lane]
    for file in {node.split("::")[0] for node in full}:
        assert sum(any(node.startswith(file + "::") for node in lane) for lane in lanes) == 1


def test_invalid_lane_fails_before_collecting_tests():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "--test-lane=0/6"],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 4
    assert "--test-lane" in result.stderr


def test_measured_duration_rebalances_files_and_estimates_new_files():
    from tests.lane_partition import partition_files

    items = [("tests/fast_measured.py", ())] + [("tests/slow_new.py", ("state_migration",))] * 20
    measured = {"tests/fast_measured.py": 1.0}
    assigned = partition_files(items, 2, measured)
    assert assigned == partition_files(reversed(items), 2, measured)
    assert assigned["tests/slow_new.py"] == 1
    assert assigned["tests/fast_measured.py"] == 2
    recorded_zero = partition_files(items, 2, measured | {"tests/slow_new.py": 0.0})
    assert recorded_zero["tests/fast_measured.py"] == 1
    assert recorded_zero["tests/slow_new.py"] == 2


def test_unrelated_durations_keep_the_heuristic_assignment():
    from tests.lane_partition import partition_files

    items = [(f"tests/test_future_{file}.py", ()) for file in range(4)]
    assert partition_files(items, 6, {"tests/other.py": 99.0}) == partition_files(items, 6)


def test_junit_durations_roundtrip_by_file(tmp_path, monkeypatch):
    import json

    from tests.lane_partition import durations_from_junit, load_durations, write_durations

    monkeypatch.chdir(tmp_path)
    report = tmp_path / "results.xml"
    report.write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
        <testsuite>
          <testcase file="tests/test_fast.py" time="0.25"/>
          <testcase file="tests/test_fast.py" time="0.75"/>
          <testcase file="{tmp_path / "tests" / "test_slow.py"}" time="9"/>
          <testcase time="4"/>
        </testsuite>
        """,
        encoding="utf-8",
    )
    durations = durations_from_junit([report])
    assert durations == {"tests/test_fast.py": 1.0, "tests/test_slow.py": 9.0}
    path = tmp_path / "lane-durations.json"
    write_durations(path, durations)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "tests/test_fast.py": 1.0,
        "tests/test_slow.py": 9.0,
    }
    assert load_durations(path) == durations
    assert load_durations(tmp_path / "missing.json") == {}
    path.write_text('{"tests/test_fast.py": true}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_durations(path)


def test_complete_durations_match_fixture_partition_and_cover_files(tmp_path):
    from tests.lane_partition import assignment_from_durations, partition_files, test_files

    (tmp_path / "test_keep.py").write_text("def test_keep(): pass\n", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "test_other.py").write_text("def test_other(): pass\n", encoding="utf-8")
    (tmp_path / "helper.py").write_text("x = 1\n", encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "test_hidden.py").write_text("def test_hidden(): pass\n", encoding="utf-8")
    paths = test_files(tmp_path, [tmp_path], ["test_*.py", "*_test.py"], [".*", "build"])
    assert paths == ["nested/test_other.py", "test_keep.py"]
    durations = {"nested/test_other.py": 50, "test_keep.py": 1}
    assigned = assignment_from_durations(paths, durations, 2)
    items = [(path, ("state_migration",)) for path in paths]
    assert assigned == partition_files(items, 2, durations)
    assert assignment_from_durations(paths, {"test_keep.py": 1}, 2) is None


def test_duplicate_junit_ids_are_reported_once(tmp_path):
    from tests.lane_partition import duplicate_junit_ids

    first = tmp_path / "one.xml"
    second = tmp_path / "two.xml"
    body = '<testcase classname="tests.test_a" name="test_a" file="tests/test_a.py" time="1"/>'
    first.write_text(f"<testsuite>{body}</testsuite>", encoding="utf-8")
    second.write_text(
        f"<testsuite>{body}"
        '<testcase classname="tests.test_b" name="test_b" file="tests/test_b.py" time="1"/>'
        "</testsuite>",
        encoding="utf-8",
    )
    assert duplicate_junit_ids([first, second]) == ["tests/test_a.py::tests.test_a::test_a"]


def _collect_lane(root, tmp_path, durations, lane, *args):
    env = {key: value for key, value in os.environ.items() if key != "LANE_DURATIONS_FILE"}
    env.update(
        PYTHONPATH=str(root),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        LANE_DURATIONS_FILE=str(durations),
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            "--collect-only",
            "-q",
            f"--test-lane={lane}",
            *args,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_complete_durations_do_not_import_other_lane_files(tmp_path):
    root = Path(__file__).parents[1]
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_keep.py").write_text("def test_keep(): pass\n", encoding="utf-8")
    (tmp_path / "test_other.py").write_text(
        "from pathlib import Path\n"
        "Path('other-imported').write_text('yes')\n"
        "def test_other(): pass\n",
        encoding="utf-8",
    )
    durations = tmp_path / "durations.json"
    durations.write_text(
        '{"test_other.py": 50, "test_keep.py": 1}\n',
        encoding="utf-8",
    )
    kept = _collect_lane(root, tmp_path, durations, "2/2")
    assert kept.returncode == 0, kept.stderr
    assert "test_keep.py::test_keep" in kept.stdout
    assert "test_other.py" not in kept.stdout
    assert not (tmp_path / "other-imported").exists()
    other = _collect_lane(root, tmp_path, durations, "1/2")
    assert other.returncode == 0, other.stderr
    assert "test_other.py::test_other" in other.stdout
    assert (tmp_path / "other-imported").is_file()


def test_incomplete_durations_still_import_every_collected_file(tmp_path):
    root = Path(__file__).parents[1]
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_keep.py").write_text("def test_keep(): pass\n", encoding="utf-8")
    (tmp_path / "test_other.py").write_text(
        "from pathlib import Path\n"
        "Path('other-imported').write_text('yes')\n"
        "def test_other(): pass\n",
        encoding="utf-8",
    )
    durations = tmp_path / "durations.json"
    durations.write_text('{"test_keep.py": 1}\n', encoding="utf-8")
    kept = _collect_lane(root, tmp_path, durations, "2/2")
    assert kept.returncode == 0, kept.stderr
    assert (tmp_path / "other-imported").is_file()


def test_missing_duration_override_uses_the_default_file(tmp_path, monkeypatch):
    from tests import lane_partition

    default = tmp_path / "default.json"
    default.write_text('{"tests/default.py": 2}\n', encoding="utf-8")
    monkeypatch.setattr(lane_partition, "DEFAULT_DURATIONS", default)
    monkeypatch.setenv("LANE_DURATIONS_FILE", str(tmp_path / "missing.json"))
    assert lane_partition.durations_path() == default
    assert lane_partition.load_durations() == {"tests/default.py": 2.0}
    monkeypatch.delenv("LANE_DURATIONS_FILE")
    default.unlink()
    assert lane_partition.durations_path() is None
    assert lane_partition.load_durations() == {}


def test_load_durations_rejects_invalid_documents(tmp_path):
    from tests.lane_partition import load_durations

    path = tmp_path / "durations.json"
    path.write_text("[1, 2]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        load_durations(path)
    path.write_text('{"tests/a.py": -0.01}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="negative"):
        load_durations(path)


def test_assignment_handles_empty_duplicate_and_negative_durations():
    from tests.lane_partition import assignment_from_durations

    assert assignment_from_durations([], {"a.py": 1}, 3) == {}
    assert assignment_from_durations(["a.py", "a.py"], {"a.py": 2, "b.py": 9}, 3) == {"a.py": 1}
    assert assignment_from_durations(["a.py"], {"a.py": -0.1}, 3) is None


def test_negative_measured_duration_keeps_the_heuristic_assignment():
    from tests.lane_partition import partition_files

    items = [("tests/neg.py", ()), ("tests/other.py", ())]
    assert partition_files(items, 2, {"tests/neg.py": -5}) == partition_files(items, 2)


def test_setup_heuristics_add_fixture_and_path_weights():
    from tests.lane_partition import _heuristic_costs

    costs = _heuristic_costs(
        [
            ("tests/plain.py", ()),
            ("tests/migrations/test_m.py", ()),
            ("tests/test_schema.py", ("schema_database",)),
            ("tests/migrations/test_schema.py", ("schema_database",)),
            ("tests/migrations/test_state.py", ("state_migration",)),
            ("tests/test_pg.py", ("postgres_tables", "admin")),
            ("tests/test_manual.py", ("manual_sessions", "callback_db")),
            ("tests/tasks/test_smt_case.py", ()),
            ("tests/reliable/test_postgres.py", ("storage",)),
        ]
    )
    assert costs == {
        "tests/plain.py": 2,
        "tests/migrations/test_m.py": 8,
        "tests/test_schema.py": 14,
        "tests/migrations/test_schema.py": 14,
        "tests/migrations/test_state.py": 18,
        "tests/test_pg.py": 5,
        "tests/test_manual.py": 5,
        "tests/tasks/test_smt_case.py": 6,
        "tests/reliable/test_postgres.py": 7,
    }


def test_discovery_skips_excluded_entries_and_reads_an_explicit_file(tmp_path):
    from tests.lane_partition import test_files

    root = tmp_path / "suite"
    root.mkdir()
    (root / "test_keep.py").write_text("def test_keep(): pass\n", encoding="utf-8")
    build = root / "build"
    build.mkdir()
    (build / "test_hidden.py").write_text("def test_hidden(): pass\n", encoding="utf-8")
    (root / "test_link.py").symlink_to(root / "test_keep.py")
    outside = tmp_path / "test_outside.py"
    outside.write_text("def test_outside(): pass\n", encoding="utf-8")
    patterns = ["test_*.py"]
    assert test_files(
        root,
        [root / "test_keep.py", outside, root / "missing.py"],
        patterns,
        ["build"],
    ) == ["test_keep.py"]
    assert test_files(root, [root], patterns, ["build"]) == ["test_keep.py"]
    assert test_files(root, [build], patterns, ["build"]) == ["build/test_hidden.py"]


def test_junit_skips_foreign_paths_and_defaults_missing_time(tmp_path, monkeypatch):
    from tests.lane_partition import duplicate_junit_ids, durations_from_junit

    monkeypatch.chdir(tmp_path)
    foreign = tmp_path.parent / "foreign" / "test_foreign.py"
    report = tmp_path / "results.xml"
    report.write_text(
        f"""<testsuite>
          <testcase file="{foreign}" time="3"/>
          <testcase file="tests/test_a.py" classname="tests.test_a" name="test_a"/>
          <testcase file="{foreign}" classname="tests.test_foreign" name="test_same"/>
        </testsuite>""",
        encoding="utf-8",
    )
    second = tmp_path / "two.xml"
    second.write_text(
        f'<testsuite><testcase file="{foreign}" classname="tests.test_foreign" '
        'name="test_same"/></testsuite>',
        encoding="utf-8",
    )
    assert durations_from_junit([report]) == {"tests/test_a.py": 0.0}
    assert duplicate_junit_ids([report, second]) == ["::tests.test_foreign::test_same"]


def test_write_durations_rounds_sorted_keys_and_creates_the_parent(tmp_path):
    import json

    from tests.lane_partition import write_durations

    path = tmp_path / "out" / "lane-durations.json"
    write_durations(path, {"tests/b.py": 1.2346, "tests/a.py": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "tests/a.py": 2.0,
        "tests/b.py": 1.235,
    }


def test_node_id_argument_selects_only_that_file(tmp_path):
    root = Path(__file__).parents[1]
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_keep.py").write_text(
        "def test_keep(): pass\ndef test_second(): pass\n",
        encoding="utf-8",
    )
    (tmp_path / "test_other.py").write_text("def test_other(): pass\n", encoding="utf-8")
    durations = tmp_path / "durations.json"
    durations.write_text(
        '{"test_keep.py": 1, "test_other.py": 50}\n',
        encoding="utf-8",
    )
    selected = _collect_lane(root, tmp_path, durations, "1/2", "test_keep.py::test_keep")
    assert selected.returncode == 0, selected.stderr
    assert "test_keep.py::test_keep" in selected.stdout
    assert "test_second" not in selected.stdout
    assert "test_other.py" not in selected.stdout


def test_collected_file_outside_the_duration_assignment_is_a_usage_error(tmp_path):
    root = Path(__file__).parents[1]
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_keep.py").write_text("def test_keep(): pass\n", encoding="utf-8")
    (tmp_path / "test_extra.py").write_text("def test_extra(): pass\n", encoding="utf-8")
    (tmp_path / "test_link.py").symlink_to(tmp_path / "test_extra.py")
    durations = tmp_path / "durations.json"
    durations.write_text(
        '{"test_keep.py": 1, "test_extra.py": 2}\n',
        encoding="utf-8",
    )
    result = _collect_lane(root, tmp_path, durations, "1/2")
    assert result.returncode == 4, result.stdout + result.stderr
    assert "test_link.py" in result.stderr
    assert "lane assignment did not cover collected files" in result.stderr
