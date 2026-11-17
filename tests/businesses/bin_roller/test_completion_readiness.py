from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.businesses._components.bin_roller.completion import release_intent, supply_bypass_evidence
from app.businesses._components.bin_roller.ports import Scan2Arrival, SourceFaceRef, SupplyProgress


@pytest.mark.parametrize(
    "count,fed,production,expected",
    [
        (0, (), (), True),
        (2, ("A", "A"), ("A",), False),
        (2, ("A", "A"), ("A", "A"), True),
        (2, ("A", "B"), ("A", "C"), False),
        (2, ("A", "B"), ("B", "A", "C"), True),
        (None, (), ("A",), False),
        (1, (), ("A",), False),
    ],
)
async def test_supply_requires_matching_production_for_each_fed_box(
    count, fed, production, expected
):
    progress = (
        SupplyProgress(
            "task",
            SourceFaceRef(1, "rack", "A"),
            True,
            False,
            ("original",),
            fed_bin_count=count,
            fed_bin_codes=fed,
        ),
    )
    evidence = await supply_bypass_evidence(
        None,
        None,
        work_line_id=1,
        task_id="task",
        progress=progress,
        production_codes=production,
        cancellations={},
    )
    assert (evidence is not None) is expected


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
