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


def _collect_lane(root, tmp_path, durations, lane):
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
