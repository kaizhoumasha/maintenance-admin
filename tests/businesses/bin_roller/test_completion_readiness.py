from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.businesses._components.bin_roller.completion import release_intent, supply_accounted_for
from app.businesses._components.bin_roller.ports import Scan2Arrival, SourceFaceRef, SupplyProgress


@pytest.mark.parametrize(
    "counts,production_count,expected",
    [((), 0, True), ((0,), 0, True), ((1, 1), 1, False), ((1, 1), 2, True), ((None,), 5, False)],
)
def test_supply_requires_known_counts_across_every_source(counts, production_count, expected):
    progress = tuple(
        SupplyProgress(
            "task",
            SourceFaceRef(1, f"rack-{index}", "A"),
            True,
            False,
            ("original",),
            fed_bin_count=count,
        )
        for index, count in enumerate(counts)
    )
    assert supply_accounted_for(progress, production_count=production_count) is expected


@pytest.fixture
def original_release():
    arrival = Scan2Arrival("arrival", 1, "SCAN2", "A000000001-C", "A000000001", 100)
    scan = dict(
        asdict(arrival),
        point="SCAN2",
        scope_kind="TASK",
        task_id="task",
        production_record_id=arrival.arrival_id,
        release_command_code="command",
    )
    roller = SimpleNamespace(original_arrival=AsyncMock(return_value=scan))
    return arrival, scan, roller


@pytest.mark.parametrize("missing", ["command", "arrival"])
async def test_release_intent_waits_for_missing_original_evidence(original_release, missing):
    arrival, _, roller = original_release
    if missing == "arrival":
        roller.original_arrival.side_effect = ValueError("original arrival missing")
    assert (
        await release_intent(
            roller,
            None,
            arrival=arrival,
            task_id="task",
            command_code=None if missing == "command" else "command",
        )
        is None
    )
