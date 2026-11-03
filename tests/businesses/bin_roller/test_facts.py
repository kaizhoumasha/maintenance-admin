from dataclasses import replace

import pytest


async def accept(store, digest, code, *, point="SCAN1", separator=None):
    async with store.factory.begin() as session:
        return await store.repo.accept_scan_fact(
            session,
            digest=digest,
            receipt_id=1,
            point=point,
            device_code="CONVEYOR",
            raw_bin_code=code,
            bin_code=code,
            scanned_at=100,
            separator_command_code=separator,
        )


async def test_new_arrival_replaces_stale_position(roller_store):
    store = roller_store
    await accept(store, "a", "A")
    new = await accept(store, "b", "B")
    async with store.factory() as session:
        current = await store.repo.position_at(session, "BIN", "SCAN1")
        assert current["object_code"] == "B" and current["source_id"] == new["id"]
        assert await store.repo.current_position(session, "BIN", "A") is None


async def test_same_object_has_one_current_position(roller_store):
    store = roller_store
    await accept(store, "a", "A")
    await accept(store, "b", "B")
    await accept(store, "a2", "A", point="SCAN2")
    async with store.factory() as session:
        assert (await store.repo.current_position(session, "BIN", "A"))["location_code"] == "SCAN2"
        assert (await store.repo.position_at(session, "BIN", "SCAN1"))["object_code"] == "B"


async def test_old_arrival_replay_keeps_new_position(roller_store):
    store = roller_store
    old = await accept(store, "a", "A")
    await accept(store, "a2", "A", point="SCAN2")
    assert (await accept(store, "a", "A"))["id"] == old["id"]
    async with store.factory() as session:
        assert (await store.repo.current_position(session, "BIN", "A"))["location_code"] == "SCAN2"


async def test_scan_fact_does_not_wait_for_transport_or_milestone(roller_store):
    store = roller_store
    arrival = await accept(store, "a", "A", point="SCAN2")
    async with store.factory() as session:
        assert (await store.repo.current_position(session, "BIN", "A"))["source_id"] == arrival[
            "id"
        ]


async def test_round_association_does_not_reject_scan_fact(roller_store):
    store = roller_store
    first = await accept(store, "a", "A", point="SCAN3")
    await accept(store, "b", "B", point="SCAN3")
    later = await accept(store, "a2", "A", point="SCAN3")
    assert later["id"] != first["id"]
    assert later["round_id"] is None


@pytest.mark.parametrize(
    "mismatch",
    [
        {"owner_domain": "other"},
        {"client_request_id": "other"},
        {"availability": "INVALID"},
        {"cycle_id": "other"},
        {"kind": "BIN_MOVE"},
        {"transport_task_id": "other"},
        {"operation_id": "other"},
        {"workline_id": "other"},
    ],
)
async def test_unrelated_original_result_cannot_publish_rack_position(
    roller_lifecycle, monkeypatch, mismatch
):
    flow = roller_lifecycle
    demand = await flow.demand()
    await flow.advance("FEED", demand)
    entry = (await flow.all_rows("rack_action"))[0]
    await flow.rack_result(entry)
    port = flow.roller.capabilities.transport
    original = port.get_result

    async def read(*args, **kwargs):
        return replace(await original(*args, **kwargs), **mismatch)

    monkeypatch.setattr(port, "get_result", read)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    async with flow.factory() as session:
        assert await flow.store.repo.current_position(session, "RACK", "R1") is None
    assert not await flow.all_rows("feed")


@pytest.mark.parametrize("consume_unknown", [False, True])
async def test_newer_observation_preserves_known_position_and_original_outcome(
    roller_lifecycle, consume_unknown
):
    flow = roller_lifecycle
    demand = await flow.demand()
    await flow.advance("FEED", demand)
    action = (await flow.all_rows("rack_action"))[0]
    cycle = action["cycle_id"]
    payload = dict(
        transport_task_id=action["transport_task_id"],
        kind="RACK_MOVE",
        rack_id="R1",
        arrival_face=None,
    )
    await flow.rack_callback(
        dict(
            payload,
            status="FAILED",
            outcome_revision=1,
            position_unknown=True,
            failure_code="POSITION_UNKNOWN",
        )
    )
    if consume_unknown:
        await flow.advance("RACK_CYCLE", cycle)
        assert not await flow.all_rows("position_fact")
    wrong = dict(
        payload,
        status="SUCCEEDED",
        outcome_revision=2,
        arrival_face="A",
        final_position=dict(kind="RACK_POSITION", location_code="OTHER"),
    )
    await flow.rack_callback(wrong)
    for _ in range(2):
        await flow.advance("RACK_CYCLE", cycle)
        async with flow.factory() as session:
            job = await flow.roller.rack_dispatch.progress(session, job_id=cycle)
            assert job.actions[0].outcome["position_unknown"]
            assert (job.actions[0].outcome_revision, job.actions[0].position_revision) == (1, 2)
            assert job.review_reason == "POSITION_CONFLICT"
        assert len(await flow.all_rows("position_fact")) == 1
        assert not await flow.all_rows("feed")
    await flow.rack_result(action, revision=3)
    await flow.advance("RACK_CYCLE", cycle)
    async with flow.factory() as session:
        p = await flow.store.repo.current_position(session, "RACK", "R1")
        assert (p["location_code"], p["source_id"], p["source_revision"]) == (
            "RACK-WORK",
            action["id"],
            3,
        )
    assert len(await flow.all_rows("feed")) == 1


async def test_failed_arrival_records_original_position_without_authorizing_face(roller_lifecycle):
    flow = roller_lifecycle
    demand = await flow.demand()
    await flow.advance("FEED", demand)
    entry = (await flow.all_rows("rack_action"))[0]
    await flow.rack_result(entry, status="FAILED")
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    async with flow.factory() as session:
        job = await flow.roller.rack_dispatch.progress(session, job_id=entry["cycle_id"])
        assert job.actions[0].outcome["status"] == "FAILED"
        p = await flow.store.repo.current_position(session, "RACK", "R1")
        assert (p["source_id"], p["rack_face"]) == (entry["id"], "A")
    assert not await flow.all_rows("feed")


@pytest.mark.parametrize(
    "http,code,mismatched",
    [
        (200, "RECEIVED", False),
        (202, "RECEIVED", True),
        (422, "REJECTED", False),
        (200, "DUPLICATE", False),
    ],
)
async def test_departure_requires_its_own_acceptance_and_preserves_new_rack(
    roller_lifecycle, http, code, mismatched
):
    flow = roller_lifecycle
    demand, cycle = await flow.ready_supply(count=1)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FEED", demand)
    root = (await flow.all_rows("departure"))[0]
    await flow.decision(
        "departure",
        root["id"],
        dict(result="READY", rack_destination=dict(type="ZONE", location_code="S")),
    )
    await flow.advance("RACK_CYCLE", cycle)
    depart = (await flow.all_rows("rack_action"))[-1]
    await flow.accepted(
        depart, http=http, code=code, response_task="different-task" if mismatched else None
    )
    await flow.advance("RACK_CYCLE", cycle)
    async with flow.factory() as session:
        job = await flow.roller.rack_dispatch.progress(session, job_id=cycle)
        if http != 200 or code != "DUPLICATE":
            assert job.pending and job.actions[-1].id == depart["id"]
            return
        assert not job.pending and job.closure_reason == "DEPARTURE_ACK"
        assert (
            await flow.roller.rack_dispatch.final_result(
                session, job_id=cycle, action_id=depart["id"]
            )
        ).outcome is None
    original_faces = await flow.all_rows("face_work")
    new = await flow.demand(rack="R2")
    await flow.advance("FEED", new)
    entry = (await flow.all_rows("rack_action"))[-1]
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    await flow.rack_callback(
        dict(
            transport_task_id=depart["transport_task_id"],
            kind="RACK_MOVE",
            rack_id="R1",
            status="FAILED",
            outcome_revision=1,
            final_position=dict(kind="RACK_POSITION", location_code="RACK-WORK"),
            failure_code="RCS_EXECUTION_FAILED",
        )
    )
    await flow.advance("RACK_CYCLE", cycle)
    async with flow.factory() as session:
        assert await flow.roller.rack_dispatch.progress(session, job_id=cycle) == job
        assert (await flow.store.repo.position_at(session, "RACK", "RACK-WORK"))[
            "source_id"
        ] == entry["id"]
        assert (
            await flow.roller.rack_dispatch.final_result(
                session, job_id=cycle, action_id=depart["id"]
            )
        ).outcome["status"] == "FAILED"
    assert [r for r in await flow.all_rows("face_work") if r["cycle_id"] == cycle] == original_faces


async def test_original_caller_and_observations_keep_frozen_authority(
    roller_lifecycle, monkeypatch
):
    flow = roller_lifecycle
    read = flow.roller.read_resources

    async def custom(*args):
        return dict(await read(*args), caller_workline_id="WL-01")

    monkeypatch.setattr(flow.roller, "read_resources", custom)
    demand = await flow.demand()
    await flow.advance("FEED", demand)
    entry = (await flow.all_rows("rack_action"))[0]
    for revision, location in ((3, "NEW"), (2, "OLD")):
        await flow.rack_callback(
            dict(
                transport_task_id=entry["transport_task_id"],
                kind="RACK_MOVE",
                rack_id="R1",
                status="SUCCEEDED",
                outcome_revision=revision,
                final_position=dict(kind="RACK_POSITION", location_code=location),
                arrival_face="A",
            )
        )
        await flow.advance("FEED", demand)
        async with flow.factory() as session:
            job = await flow.roller.rack_dispatch.progress(session, job_id=entry["cycle_id"])
            assert job.actions[0].caller.workline_id == "WL-01"
            assert job.actions[0].outcome is None and job.actual_position["location_code"] == "NEW"
            assert job.actions[0].position_revision == 3
        assert not await flow.all_rows("feed")
