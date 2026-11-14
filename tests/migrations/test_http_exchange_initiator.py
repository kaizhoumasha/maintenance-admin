import pytest
from sqlalchemy import JSON, bindparam, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.core.identifiers import uuid7
from app.integrations.runtime_authority import require_reconciled
from tests.helpers import run_alembic

PREVIOUS = "http_exchange_query_views"
KINDS = {
    "wms_http_exchange": ("DECISION", "CALLBACK"),
    "ecs_http_exchange": ("COMMAND", "RESULT"),
    "transport_http_exchange": ("SUBMIT", "RESULT_CALLBACK"),
}


def _ids(connection, identity):
    return identity if connection.dialect.name == "postgresql" else identity.hex


async def _migrate(state, *args):
    url = state.engine.url.render_as_string(hide_password=False)
    result = await run_alembic(url, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    await state.engine.dispose()


@pytest.mark.parametrize(
    "state_migration",
    [
        ("TEST_HISTORY_MIGRATION_DATABASE_URL", "postgresql", "http_exchange_runtime_expand"),
        ("", "sqlite", "http_exchange_runtime_expand"),
    ],
    indirect=True,
    ids=["postgres", "sqlite"],
)
async def test_migration_preserves_bodies_view_semantics_and_grants(state_migration, monkeypatch):
    state = state_migration
    pg = state.backend == "postgresql"
    await _migrate(state, "-x", "fence_legacy_writes=true", "upgrade", PREVIOUS)
    settings = get_settings()
    monkeypatch.setattr(settings, "INTEGRATION_RUNTIME_AUTHORITY", True)
    factory = async_sessionmaker(state.engine)
    with pytest.raises(RuntimeError, match="successful reconciliation"):
        await require_reconciled(factory)

    role = "initiator_reader_" + uuid7().hex
    original = {}
    indexes = {
        table: snapshot["indexes"]
        for table, snapshot in (await state.schema_snapshot("")).items()
        if table in KINDS
    }
    async with state.engine.begin() as connection:
        for table, (wes_kind, peer_kind) in KINDS.items():
            columns = ",task_match_state" if table == "transport_http_exchange" else ""
            values = ",'UNKNOWN'" if columns else ""
            for direction, kind in (("OUTBOUND", wes_kind), ("INBOUND", peer_kind)):
                identity = _ids(connection, uuid7())
                await connection.execute(
                    text(f"""
                    INSERT INTO {table} (id,direction,interaction_kind,traffic_source,started_at,
                        finished_at,http_method,endpoint_path,attempt_state,http_status,
                        request_wire,request_wire_state,request_wire_bytes,request_wire_sha256,
                        response_wire,response_wire_state,response_wire_bytes,response_wire_sha256,
                        request_payload,response_payload,source_revision{columns})
                    VALUES (:id,:direction,:kind,'PEER',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'POST',
                        '/fixture','RESPONSE_PREPARED',200,:request,'FULL',3,:request_hash,
                        :response,'FULL',3,:response_hash,:request_payload,:response_payload,0{values})
                    """).bindparams(
                        bindparam("request_payload", type_=JSON),
                        bindparam("response_payload", type_=JSON),
                    ),
                    {
                        "id": identity,
                        "direction": direction,
                        "kind": kind,
                        "request": b"req",
                        "response": b"res",
                        "request_hash": "a" * 64,
                        "response_hash": "b" * 64,
                        "request_payload": {"n": 1},
                        "response_payload": {"n": 2},
                    },
                )
        if pg:
            await connection.execute(
                text("""
                CREATE TABLE integration_history_retirement (
                    retired_at timestamptz NOT NULL DEFAULT now(), evidence jsonb NOT NULL
                )
            """)
            )
            await connection.execute(
                text("INSERT INTO integration_history_retirement(evidence) VALUES ('{}')")
            )
            await connection.execute(
                text("""
                DROP TABLE integration_reference, integration_record, integration_exchange RESTRICT
            """)
            )
            await connection.execute(text(f"CREATE ROLE {role}"))
            await connection.execute(text(f"GRANT USAGE ON SCHEMA enterprise_backend TO {role}"))
            await connection.execute(
                text(f"""
                GRANT SELECT ON integration_exchange_summary, integration_record_summary TO {role}
            """)
            )
            await connection.execute(
                text("""
                CREATE VIEW fixture_history_reader AS
                SELECT id, direction FROM integration_exchange_summary
            """)
            )
        for table in KINDS:
            original[table] = (
                (await connection.execute(text(f"SELECT * FROM {table} ORDER BY id")))
                .mappings()
                .all()
            )

    try:
        await _migrate(state, "-x", "fence_legacy_writes=true", "upgrade", "head")
        await require_reconciled(factory)
        schema = await state.schema_snapshot("")
        assert {table: schema[table]["indexes"] for table in KINDS} == indexes
        async with state.engine.begin() as connection:
            for table in KINDS:
                migrated = (
                    (await connection.execute(text(f"SELECT * FROM {table} ORDER BY id")))
                    .mappings()
                    .all()
                )
                assert [dict(row) for row in migrated] == [
                    {key: value for key, value in row.items() if key != "direction"}
                    | {"initiator": {"OUTBOUND": "WES", "INBOUND": "PEER"}[row["direction"]]}
                    for row in original[table]
                ]
                for invalid in ("INBOUND", "OUTBOUND", "UNKNOWN", None):
                    async with connection.begin_nested() as savepoint:
                        with pytest.raises(IntegrityError):
                            await connection.execute(
                                text(f"UPDATE {table} SET initiator=:value"), {"value": invalid}
                            )
                        await savepoint.rollback()
            if pg:
                assert (
                    await connection.scalar(text("SELECT count(*) FROM fixture_history_reader"))
                    == 6
                )
                await connection.execute(text(f"SET LOCAL ROLE {role}"))
            views = await connection.execute(
                text("""
                SELECT h.initiator,h.response_scope,r.side,r.initiator,r.record_kind
                FROM integration_exchange_summary h JOIN integration_record_summary r
                    ON r.exchange_id=h.id
            """)
            )
            expected = {
                ("WES", "PEER_OBSERVATION", "request", "WES", "REQUEST_REGISTERED"),
                ("WES", "PEER_OBSERVATION", "response", "WES", "HTTP_RESPONSE_OBSERVED"),
                ("PEER", "PROTOCOL_PREPARED", "request", "PEER", "CALLBACK_RECEIVED"),
                ("PEER", "PROTOCOL_PREPARED", "response", "PEER", "RESPONSE_PREPARED"),
            }
            rows = views.all()
            assert len(rows) == 12 and set(rows) == expected

        await _migrate(state, "downgrade", PREVIOUS)
        async with state.engine.connect() as connection:
            for table in KINDS:
                restored = (
                    (await connection.execute(text(f"SELECT * FROM {table} ORDER BY id")))
                    .mappings()
                    .all()
                )
                assert restored == original[table]
            pairs = await connection.execute(
                text("""
                SELECT h.direction,r.side,r.direction
                FROM integration_exchange_summary h JOIN integration_record_summary r
                    ON r.exchange_id=h.id
            """)
            )
            assert set(pairs) == {
                ("OUTBOUND", "request", "OUTBOUND"),
                ("OUTBOUND", "response", "INBOUND"),
                ("INBOUND", "request", "INBOUND"),
                ("INBOUND", "response", "OUTBOUND"),
            }
    finally:
        if pg:
            async with state.engine.begin() as connection:
                await connection.execute(text(f"DROP OWNED BY {role}"))
                await connection.execute(text(f"DROP ROLE {role}"))
