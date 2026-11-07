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
                **os.environ,
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
