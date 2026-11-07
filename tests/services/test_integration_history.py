from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from app.core.identifiers import uuid7
from app.models import IntegrationExchange

NOW = datetime(2026, 11, 7, tzinfo=UTC)


async def seed_history(factory, *, times, identity="original"):
    ids = []
    async with factory.begin() as session:
        for at in times:
            root = IntegrationExchange(
                role="OUTGOING",
                protocol="wms-facts",
                peer_system="WMS",
                direction="OUTBOUND",
                interaction_kind="REQUEST",
                identity_kind="OPERATION_ID",
                identity_value=identity,
                operation="fact@v1",
                created_at=at,
            )
            session.add(root)
            await session.flush()
            ids.append(root.id)
    return ids


async def test_history_search_keeps_original_instances_distinct(callback_store):
    from app.integrations.history import write_reference
    from app.schemas.integration_history import ExchangeListParams
    from app.services.integration_history_service import list_exchanges

    identity = str(uuid7())
    ids = await seed_history(
        callback_store, times=[NOW - timedelta(days=120), NOW], identity=identity
    )
    async with callback_store.begin() as session:
        for value in ("original-task", "other-task"):
            await write_reference(
                session,
                exchange_id=ids[0],
                reference_kind="BUSINESS_TASK",
                namespace="test",
                value=value,
                relation_role="OWNER",
                created_at=NOW,
            )
    async with callback_store() as session:
        page = await list_exchanges(
            session, ExchangeListParams(identity_kind="OPERATION_ID", identity_value=identity)
        )
        assert {row["id"] for row in page["items"]} == set(ids)
        page = await list_exchanges(
            session,
            ExchangeListParams(
                reference_kind="BUSINESS_TASK",
                reference_namespace="test",
                reference_value="original-task",
            ),
        )
        assert [row["id"] for row in page["items"]] == [ids[0]]
        assert not ({"raw_body", "effective_payload", "route", "input_raw"} & set(page["items"][0]))


async def test_time_uuid_cursor_keeps_equal_time_rows(callback_store):
    from app.schemas.integration_history import ExchangeListParams
    from app.services.integration_history_service import list_exchanges

    identity = str(uuid7())
    expected = set(await seed_history(callback_store, times=[NOW] * 7, identity=identity))
    seen, cursor = [], None
    while True:
        async with callback_store() as session:
            page = await list_exchanges(
                session, ExchangeListParams(identity_value=identity, limit=2, cursor=cursor)
            )
        seen.extend(row["id"] for row in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert len(cursor) <= 256
    assert len(seen) == len(set(seen)) == 7 and set(seen) == expected


async def test_query_range_and_default_anchor(callback_store, monkeypatch):
    from app.core.errors import AppError
    from app.schemas.integration_history import ExchangeListParams
    from app.services import integration_history_service as history

    # Only the clock moves; the original default upper/lower bounds must survive pagination.
    monkeypatch.setattr(history, "utc_now", lambda: NOW)
    await seed_history(
        callback_store,
        times=[NOW - timedelta(days=89), NOW - timedelta(days=1), NOW - timedelta(days=2)],
    )
    async with callback_store() as session:
        first = await history.list_exchanges(session, ExchangeListParams(limit=1))
    monkeypatch.setattr(history, "utc_now", lambda: NOW + timedelta(days=5))
    async with callback_store() as session:
        rest = await history.list_exchanges(
            session, ExchangeListParams(limit=100, cursor=first["next_cursor"])
        )
        assert len(rest["items"]) == 2
        for params in (
            ExchangeListParams(from_at=NOW - timedelta(days=91), until_at=NOW),
            ExchangeListParams(from_at=NOW),
            ExchangeListParams(cursor=first["next_cursor"], peer_system="ECS"),
        ):
            with pytest.raises(AppError):
                await history.list_exchanges(session, params)


async def test_record_timeline_and_body_keep_known_empty_distinct(callback_store):
    from app.core.errors import AppError
    from app.integrations.history import RecordDraft, write_record
    from app.schemas.integration_history import RecordListParams
    from app.services.integration_history_service import get_body, list_records

    (identity,) = await seed_history(callback_store, times=[NOW])
    async with callback_store.begin() as session:
        empty = await write_record(
            session,
            draft=RecordDraft(
                exchange_id=identity,
                record_kind="CALLBACK_RECEIVED",
                event_key="empty",
                direction="INBOUND",
                raw_body=b"",
            ),
        )
        missing = await write_record(
            session,
            draft=RecordDraft(
                exchange_id=identity,
                record_kind="CALLBACK_RECEIVED",
                event_key="missing",
                direction="INBOUND",
            ),
        )
    async with callback_store() as session:
        page = await list_records(session, identity, RecordListParams())
        assert {r["id"] for r in page["items"]} == {empty, missing}
        assert all("raw_body" not in r for r in page["items"])
        assert (await get_body(session, empty)).raw_body == b""
        with pytest.raises(AppError) as error:
            await get_body(session, missing)
        assert error.value.detail == "UNAVAILABLE"


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "integer_id",
        "naive_time",
        "wrong_version",
        "boolean_version",
        "reordered",
        "padding",
        "oversize",
    ],
)
async def test_cursor_rejects_noncanonical_values(callback_store, mutation):
    import base64
    import json

    from app.core.errors import AppError
    from app.schemas.integration_history import ExchangeListParams
    from app.services.integration_history_service import list_exchanges

    identity = str(uuid7())
    await seed_history(callback_store, times=[NOW] * 3, identity=identity)
    async with callback_store() as session:
        first = await list_exchanges(session, ExchangeListParams(identity_value=identity, limit=1))
        raw = first["next_cursor"]
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if mutation == "unknown":
            data["unknown"] = 1
        elif mutation == "integer_id":
            data["id"] = 1
        elif mutation == "naive_time":
            data["at"] = data["at"][:-1]
        elif mutation == "wrong_version":
            data["v"] = 2
        elif mutation == "boolean_version":
            data["v"] = True
        elif mutation == "reordered":
            data = dict(reversed(list(data.items())))
        altered = (
            base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode())
            .decode()
            .rstrip("=")
        )
        if mutation == "padding":
            altered += "="
        if mutation == "oversize":
            altered = "a" * 256
        with pytest.raises(AppError):
            await list_exchanges(
                session, ExchangeListParams(identity_value=identity, cursor=altered)
            )


async def test_filter_or_cannot_expand_the_outer_time_window(callback_store):
    import json

    from app.schemas.integration_history import ExchangeListParams
    from app.services.integration_history_service import list_exchanges

    ids = await seed_history(
        callback_store, times=[NOW - timedelta(days=91), NOW - timedelta(days=90), NOW]
    )
    filters = json.dumps(
        {
            "couple": "or",
            "conditions": [
                {"field": "peer_system", "op": "eq", "value": "WMS"},
                {"field": "role", "op": "eq", "value": "OUTGOING"},
            ],
        }
    )
    async with callback_store() as session:
        page = await list_exchanges(
            session,
            ExchangeListParams(from_at=NOW - timedelta(days=90), until_at=NOW, filter=filters),
        )
        assert {row["id"] for row in page["items"]} == set(ids[1:])


async def test_history_lists_never_select_payload_or_route_columns(callback_store):
    from sqlalchemy import event

    from app.integrations.history import RecordDraft, write_record
    from app.schemas.integration_history import ExchangeListParams, RecordListParams
    from app.services.integration_history_service import list_exchanges, list_records

    (identity,) = await seed_history(callback_store, times=[NOW])
    async with callback_store.begin() as session:
        await write_record(
            session,
            draft=RecordDraft(
                exchange_id=identity,
                record_kind="CALLBACK_RECEIVED",
                event_key="large",
                direction="INBOUND",
                raw_body=b"PRIVATE_BODY",
                details={"origin": "PRIVATE_ROUTE"},
            ),
        )
    statements = []

    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement.lower())

    engine = callback_store.kw["bind"]
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with callback_store() as session:
            page = await list_exchanges(session, ExchangeListParams(identity_value="original"))
            records = await list_records(session, identity, RecordListParams())
            assert page["items"] and records["items"]
            assert "origin" not in (records["items"][0]["metadata"] or {})
        assert not any(
            ".raw_body" in sql
            or ".effective_payload" in sql
            or ".route" in sql
            or ".input_raw" in sql
            for sql in statements
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)


async def test_applied_summary_requires_original_result_evidence(callback_store):
    from app.schemas.integration_history import ExchangeListParams
    from app.services.integration_history_service import list_exchanges

    identity = str(uuid7())
    async with callback_store.begin() as session:
        session.add(
            IntegrationExchange(
                role="ECS_COMMAND",
                protocol="ecs-command",
                peer_system="ECS",
                direction="OUTBOUND",
                interaction_kind="COMMAND",
                identity_kind="COMMAND_CODE",
                identity_value=identity,
                command_code=identity,
                owner_domain="test",
                call_key=identity,
                call_key_digest=identity.replace("-", "") + "a" * 32,
                input_raw=b"original",
                status="SUCCEEDED",
                delivery_status="DONE",
                created_at=NOW,
            )
        )
    async with callback_store() as session:
        page = await list_exchanges(session, ExchangeListParams(identity_value=identity))
        application = page["items"][0]["application"]
        assert application["state"] == "UNKNOWN"
        assert application["recorded_state"] == "SUCCEEDED"
        assert application["evidence_reason"] == "APPLIED_EVIDENCE_UNAVAILABLE"


async def test_workline_reference_is_group_search_with_default_window(callback_store, monkeypatch):
    from app.integrations.history import write_reference
    from app.schemas.integration_history import ExchangeListParams
    from app.services import integration_history_service as history

    monkeypatch.setattr(history, "utc_now", lambda: NOW)
    ids = await seed_history(
        callback_store, times=[NOW - timedelta(days=91), NOW - timedelta(days=1)]
    )
    async with callback_store.begin() as session:
        for identity in ids:
            await write_reference(
                session,
                exchange_id=identity,
                reference_kind="WORKLINE",
                namespace="id",
                value="1",
                relation_role="OWNER",
                created_at=NOW,
            )
    async with callback_store() as session:
        page = await history.list_exchanges(
            session,
            ExchangeListParams(
                reference_kind="WORKLINE", reference_namespace="id", reference_value="1"
            ),
        )
        assert [item["id"] for item in page["items"]] == [ids[1]]


async def test_history_page_count_is_independent_of_reference_fanout(callback_store):
    from sqlalchemy import event

    from app.models import IntegrationRecord, IntegrationReference
    from app.schemas.integration_history import ExchangeListParams
    from app.services.integration_history_service import list_exchanges

    identity = str(uuid7())
    ids = await seed_history(
        callback_store, times=[NOW] * 105 + [NOW - timedelta(days=120)], identity=identity
    )
    async with callback_store.begin() as session:
        session.add_all(
            [
                IntegrationReference(
                    exchange_id=root,
                    reference_kind="BUSINESS_TASK",
                    namespace="cost",
                    reference_value=f"{n}",
                    value_digest=sha256(str(n).encode()).hexdigest(),
                    relation_role="OWNER",
                    created_at=NOW,
                )
                for root in ids
                for n in range(15)
            ]
        )
        session.add_all(
            [
                IntegrationRecord(
                    exchange_id=ids[0],
                    record_kind="CALLBACK_RECEIVED",
                    evidence_class="DIAGNOSTIC",
                    event_key=f"cost:{n}",
                    direction="INBOUND",
                    raw_body=b"private" * 1000,
                    body_state="FULL",
                    body_sha256=sha256(b"private" * 1000).hexdigest(),
                    stored_bytes=7000,
                    capture_origin="LIVE",
                    recorded_at=NOW,
                )
                for n in range(1000)
            ]
        )
    calls = []

    def capture(conn, cursor, statement, parameters, context, many):
        calls.append(statement.lower())

    engine = callback_store.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        counts = []
        async with callback_store() as session:
            for limit in (20, 100):
                calls.clear()
                page = await list_exchanges(
                    session,
                    ExchangeListParams(
                        identity_value=identity,
                        from_at=NOW - timedelta(days=90),
                        until_at=NOW,
                        limit=limit,
                    ),
                )
                assert [r["id"] for r in page["items"]] == sorted(ids[:-1], reverse=True)[:limit]
                assert not any(".raw_body" in sql or ".effective_payload" in sql for sql in calls)
                counts.append(len(calls))
        assert counts[1] <= counts[0] == 4
    finally:
        event.remove(engine, "before_cursor_execute", capture)


async def test_exact_identity_search_preserves_full_long_external_value(callback_store):
    from hashlib import sha256

    from app.schemas.integration_history import ExchangeListParams
    from app.services.integration_history_service import list_exchanges

    identity = "".join(sha256(str(n).encode()).hexdigest() for n in range(130))
    ids = await seed_history(callback_store, times=[NOW], identity=identity)
    async with callback_store() as session:
        page = await list_exchanges(session, ExchangeListParams(identity_value=identity))
        assert [row["id"] for row in page["items"]] == ids
        assert page["items"][0]["identity_value"] == identity
