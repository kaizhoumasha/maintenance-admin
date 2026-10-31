from dataclasses import replace

import pytest

from app.integrations.wms_transport import TransportTaskResult


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
    assert later["return_round_id"] is None


async def rack(store, *, cycle="cycle", action="action", kind="CTU01", source="source"):
    async with store.factory.begin() as session:
        await store.repo.create_cycle(
            session,
            cycle_id=cycle,
            rack_id="R1",
            purpose="ORDINARY",
            task_id="T1",
            source_ref=source,
        )
        return await store.repo.register_rack_action(
            session,
            cycle_id=cycle,
            kind=kind,
            source_ref=source,
            action_id=action,
            client_request_id="request-" + action,
            transport_task_id="transport-" + action,
            operation_id="operation-" + action,
            target={"kind": "RACK_POSITION", "location_code": "CTU01"},
            target_face="A",
            caller_workline_id="1",
        )


def result(action="action", **kwargs):
    return TransportTaskResult(
        transport_task_id="transport-" + action,
        client_request_id="request-" + action,
        workline_id="1",
        station_id=None,
        availability="FOUND",
        owner_domain="first",
        cycle_id="cycle",
        kind="RACK_MOVE",
        operation_id="operation-" + action,
        **kwargs,
    )


def outcome(revision=1, *, status="SUCCEEDED", location="CTU01"):
    return dict(
        transport_task_id="transport-action",
        kind="RACK_MOVE",
        rack_id="R1",
        outcome_revision=revision,
        status=status,
        final_position={"kind": "RACK_POSITION", "location_code": location},
        arrival_face="A",
        position_unknown=None,
    )


async def test_failed_known_position_uses_existing_outcome(roller_store):
    from app.businesses._components.bin_roller.rack_flow import apply_rack_result

    store = roller_store
    await rack(store)
    async with store.factory.begin() as session:
        applied = await apply_rack_result(
            session,
            action_id="action",
            result=result(outcome=outcome(status="FAILED", location="OTHER"), outcome_revision=1),
        )
        assert applied.action_changed and applied.projection_update == "UPDATED"
    async with store.factory() as session:
        assert (await store.repo.current_position(session, "RACK", "R1"))[
            "location_code"
        ] == "OTHER"
        assert (await store.repo.get_action(session, "action"))["status"] == "FAILED"


async def test_known_rack_mismatch_updates_position_without_fulfilling_action(roller_store):
    from app.businesses._components.bin_roller.rack_flow import apply_rack_result

    store = roller_store
    await rack(store)
    async with store.factory.begin() as session:
        applied = await apply_rack_result(
            session,
            action_id="action",
            result=result(position_observation=outcome(location="OTHER")),
            repository=store.repo,
        )
        assert not applied.action_changed and applied.projection_update == "UPDATED"
    async with store.factory() as session:
        assert (await store.repo.get_action(session, "action"))["status"] == "PENDING"
        assert (await store.repo.current_position(session, "RACK", "R1"))[
            "location_code"
        ] == "OTHER"


async def test_lower_original_revision_does_not_replace_newer_rack_fact(roller_store):
    from app.businesses._components.bin_roller.rack_flow import apply_rack_result

    store = roller_store
    await rack(store)
    async with store.factory.begin() as session:
        for revision, location in [(3, "NEW"), (2, "OLD")]:
            applied = await apply_rack_result(
                session,
                action_id="action",
                result=result(position_observation=outcome(revision, location=location)),
                repository=store.repo,
            )
        assert applied.projection_update == "HISTORY_ONLY"
    async with store.factory() as session:
        assert (await store.repo.current_position(session, "RACK", "R1"))["location_code"] == "NEW"


async def test_departure_acceptance_completes_original_cycle_without_outcome(roller_store):
    from app.businesses._components.bin_roller.rack_flow import apply_rack_result

    store = roller_store
    await rack(store, kind="CTU03")
    async with store.factory.begin() as session:
        applied = await apply_rack_result(
            session,
            action_id="action",
            result=result(
                submission_state="AVAILABLE",
                submission_response_state="VALID",
                submission_http_status=202,
                submission_response_code="RECEIVED",
            ),
            repository=store.repo,
        )
        assert applied.action_changed
    async with store.factory() as session:
        assert (await store.repo.get_cycle(session, "cycle"))["status"] == "RELEASED"
        assert not (await store.repo.get_action(session, "action"))["pending"]


async def test_late_departure_result_preserves_released_cycle(roller_store):
    from app.businesses._components.bin_roller.rack_flow import apply_rack_result

    store = roller_store
    await rack(store, kind="CTU03")
    async with store.factory.begin() as session:
        accepted = result(
            submission_state="AVAILABLE",
            submission_response_state="VALID",
            submission_http_status=200,
            submission_response_code="DUPLICATE",
        )
        await apply_rack_result(session, action_id="action", result=accepted, repository=store.repo)
        await store.repo.record_position(
            session,
            object_kind="RACK",
            object_code="R2",
            location_code="CTU01",
            rack_face="B",
            source_kind="RACK",
            source_id="new-action",
            source_revision=1,
        )
        late = await apply_rack_result(
            session,
            action_id="action",
            result=replace(accepted, outcome=outcome(status="FAILED"), outcome_revision=1),
            repository=store.repo,
        )
        assert late.projection_update == "HISTORY_ONLY"
    async with store.factory() as session:
        assert (await store.repo.get_cycle(session, "cycle"))["status"] == "RELEASED"
        assert (await store.repo.position_at(session, "RACK", "CTU01"))["object_code"] == "R2"


@pytest.mark.parametrize(
    "change",
    [
        dict(owner_domain="other"),
        dict(client_request_id="other"),
        dict(availability="INVALID"),
        dict(cycle_id="other"),
        dict(kind="BIN_MOVE"),
    ],
)
async def test_unrelated_result_cannot_change_original_action(roller_store, change):
    from app.businesses._components.bin_roller.rack_flow import apply_rack_result

    store = roller_store
    await rack(store)
    async with store.factory.begin() as session:
        applied = await apply_rack_result(
            session,
            action_id="action",
            result=replace(result(outcome=outcome(), outcome_revision=1), **change),
            repository=store.repo,
        )
        assert not applied.action_changed and applied.projection_update == "NEEDS_ASSOCIATION"
    async with store.factory() as session:
        assert await store.repo.current_position(session, "RACK", "R1") is None


@pytest.mark.parametrize(
    "http,code,response_state",
    [(200, "RECEIVED", "VALID"), (202, "RECEIVED", "INVALID"), (422, "REJECTED", "VALID")],
)
async def test_departure_requires_original_valid_acceptance(
    roller_store, http, code, response_state
):
    from app.businesses._components.bin_roller.rack_flow import apply_rack_result

    store = roller_store
    await rack(store, kind="CTU03")
    async with store.factory.begin() as session:
        applied = await apply_rack_result(
            session,
            action_id="action",
            result=result(
                submission_state="AVAILABLE",
                submission_response_state=response_state,
                submission_http_status=http,
                submission_response_code=code,
            ),
            repository=store.repo,
        )
        assert not applied.action_changed
        assert (await store.repo.get_cycle(session, "cycle"))["status"] == "ACTIVE"
        assert (await store.repo.get_action(session, "action"))["pending"]


async def test_original_caller_code_survives_rack_result_application(roller_store):
    from app.businesses._components.bin_roller.rack_flow import apply_rack_result

    store = roller_store
    async with store.factory.begin() as session:
        await store.repo.create_cycle(
            session,
            cycle_id="cycle",
            rack_id="R1",
            purpose="ORDINARY",
            task_id="T1",
            source_ref="source",
        )
        await store.repo.register_rack_action(
            session,
            cycle_id="cycle",
            kind="CTU01",
            source_ref="source",
            action_id="action",
            client_request_id="request-action",
            transport_task_id="transport-action",
            operation_id="operation-action",
            target={"kind": "RACK_POSITION", "location_code": "CTU01"},
            target_face="A",
            caller_workline_id="WL-01",
        )
        applied = await apply_rack_result(
            session,
            action_id="action",
            result=replace(result(outcome=outcome(), outcome_revision=1), workline_id="WL-01"),
            repository=store.repo,
        )
        assert applied.action_changed and applied.projection_update == "UPDATED"


@pytest.mark.parametrize("consume_unknown", [False, True])
async def test_newer_known_position_survives_original_unknown_outcome(
    roller_transport, consume_unknown
):
    from sqlalchemy import select

    from app.businesses._components.bin_roller.rack_flow import apply_rack_result
    from app.core.identifiers import uuid7

    rig, cycle_id = roller_transport, str(uuid7())
    store = rig.store
    async with store.factory.begin() as session:
        await store.repo.create_cycle(
            session,
            cycle_id=cycle_id,
            rack_id="R1",
            purpose="ORDINARY",
            task_id="T1",
            source_ref="source",
        )
        action = await rig.create_action(
            session, cycle_id=cycle_id, kind="CTU01", source_ref="source", target_face="A"
        )
    await rig.callback(action, status="FAILED", location=None, face=None)
    if consume_unknown:
        async with store.factory.begin() as session:
            view = await rig.read_result(session, action)
            initial = await apply_rack_result(
                session, action_id=action["id"], result=view, repository=store.repo
            )
            assert initial.action_changed and initial.projection_update == "HISTORY_ONLY"
    operation_id = await rig.callback(action, revision=2, location="OTHER")
    async with store.factory.begin() as session:
        view = await rig.read_result(session, action)
        assert view.outcome_revision == 1 and view.outcome["position_unknown"]
        assert view.position_observation["outcome_revision"] == 2
        observed = await apply_rack_result(
            session, action_id=action["id"], result=view, repository=store.repo
        )
        assert observed.action_changed is (not consume_unknown)
        assert observed.projection_update == "UPDATED"
        progress = await store.repo.get_action(session, action["id"])
        assert progress["status"] == "UNKNOWN" and progress["pending"]
        assert (progress["consumed_revision"], progress["position_revision"]) == (1, 2)
        assert (await store.repo.current_position(session, "RACK", "R1"))[
            "location_code"
        ] == "OTHER"
    await rig.callback(action, revision=2, location="OTHER", operation_id=operation_id)
    async with store.factory.begin() as session:
        replay = await apply_rack_result(
            session,
            action_id=action["id"],
            result=await rig.read_result(session, action),
            repository=store.repo,
        )
        assert not replay.action_changed and replay.projection_update == "HISTORY_ONLY"
        assert len((await session.execute(select(store.tables["position_fact"]))).all()) == 1
        assert (await store.repo.get_action(session, action["id"]))["pending"]
    await rig.callback(action, revision=3)
    async with store.factory.begin() as session:
        corrected = await apply_rack_result(
            session,
            action_id=action["id"],
            result=await rig.read_result(session, action),
            repository=store.repo,
        )
        assert corrected.action_changed and corrected.projection_update == "UPDATED"
        progress = await store.repo.get_action(session, action["id"])
        assert progress["status"] == "SUCCEEDED" and not progress["pending"]
        assert (progress["consumed_revision"], progress["position_revision"]) == (3, 3)
        assert (await store.repo.current_position(session, "RACK", "R1"))[
            "location_code"
        ] == "CTU01"
