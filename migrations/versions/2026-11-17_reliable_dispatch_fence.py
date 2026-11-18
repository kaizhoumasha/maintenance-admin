"""Enforce the scoped send budget across versions and retain the stopped lease."""

import sqlalchemy as sa
from alembic import op

revision = "reliable_dispatch_fence_v1"
down_revision = "reliable_settlement_evidence_v1"
branch_labels = None
depends_on = None


def _refusal_echo(schema, wire, status):
    if schema:
        body = f"{schema}reliable_dispatch_reply({wire})"
        operation = f"({body}->>'operation_id')"
        code = f"({body}->>'code')"
        timestamp = f"({body}->>'timestamp')"
        data = f"({body}->'data')"
        object_data = f"CASE WHEN json_typeof({data}) = 'object' THEN {data} ELSE '{{}}'::json END"
        wait = f"({data}->>'retry_after_ms')"
        task = f"({data}->>'transport_task_id')"
        valid = f"""
            json_typeof({body}->'operation_id') = 'string'
            AND json_typeof({body}->'timestamp') = 'number'
            AND CASE WHEN {timestamp} ~ '^(-?0|[1-9][0-9]{{0,18}})$'
                THEN {timestamp}::numeric <= 9223372036854775807 ELSE false END
            AND json_typeof({data}) = 'object'
        """
        fields = f"(SELECT count(*) FROM json_object_keys({body}))"
        data_fields = f"(SELECT count(*) FROM json_object_keys({object_data}))"
        valid_wait = f"""
            json_typeof({data}->'retry_after_ms') = 'number'
            AND CASE WHEN {wait} ~ '^[1-9][0-9]{{0,4}}$'
                THEN {wait}::integer <= 60000 ELSE false END
        """
    else:
        body = f"CASE WHEN json_valid(CAST({wire} AS TEXT)) THEN CAST({wire} AS TEXT) END"
        operation = f"json_extract({body}, '$.operation_id')"
        code = f"json_extract({body}, '$.code')"
        timestamp = f"json_extract({body}, '$.timestamp')"
        task = f"json_extract({body}, '$.data.transport_task_id')"
        wait = f"json_extract({body}, '$.data.retry_after_ms')"
        valid = f"""
            json_type({body}, '$.operation_id') = 'text'
            AND json_type({body}, '$.timestamp') = 'integer'
            AND typeof({timestamp}) = 'integer' AND {timestamp} >= 0
            AND json_type({body}, '$.data') = 'object'
            AND NOT EXISTS (
                SELECT 1 FROM json_tree({body})
                GROUP BY parent, key
                HAVING count(*) > 1
                    OR max(type = 'real' AND abs(CAST(atom AS REAL)) > 1.7976931348623157e308)
            )
        """
        fields = f"(SELECT count(*) FROM json_each({body}))"
        data_fields = f"(SELECT count(*) FROM json_each({body}, '$.data'))"
        valid_wait = f"""
            json_type({body}, '$.data.retry_after_ms') = 'integer' AND {wait} BETWEEN 1 AND 60000
        """
    return f"""
        ({valid}) AND {operation} = a.operation_id
        AND (
            (a.protocol = 'wms-transport' AND {task} = a.business_key
                AND {status} = 503 AND {code} = 'UNAVAILABLE')
            OR (a.protocol IN ('wms-decisions', 'wms-facts') AND {fields} = 4 AND (
                ({status} = 503 AND {code} = 'UNAVAILABLE' AND {data_fields} = 0)
                OR ({status} = 429 AND {code} = 'BUSY' AND {data_fields} = 1 AND ({valid_wait}))
            ))
        )
    """


def _budget_exhausted(schema, *, legacy):
    budget = "CASE WHEN p.target IS NULL THEN m.dispatch_retry_limit ELSE p.retry_limit END"
    request = "a.request_wire = m.frozen_request_wire"
    refusal = f"""
        a.response_verdict = 'NOT_RECEIVED'
        AND {_refusal_echo(schema, "a.response_wire", "a.response_http_status")}
    """
    complete = ""
    archived = ""
    if legacy:
        digest = "AND encode(sha256(r.raw_body), 'hex') = r.body_sha256" if schema else ""
        opening = f"""EXISTS (
            SELECT 1 FROM {schema}integration_record o
            WHERE o.id = a.id AND o.exchange_id = a.exchange_id AND o.attempt_id = a.lease_id
              AND o.parent_record_id = a.request_record_id AND o.occurred_at = a.started_at
              AND o.record_kind = 'SEND_ATTEMPT_OPENED' AND o.direction = 'OUTBOUND'
        )"""
        request = f"({request} OR (a.request_wire IS NULL AND {opening}))"
        refusal = f"""({refusal}) OR (a.response_wire IS NULL
          AND (a.response_verdict IS NULL OR a.response_verdict = 'NOT_RECEIVED') AND EXISTS (
            SELECT 1 FROM {schema}integration_record r
            WHERE r.id = a.response_record_id AND r.exchange_id = a.exchange_id
              AND r.attempt_id = a.lease_id AND r.record_kind = 'HTTP_RESPONSE_OBSERVED'
              AND r.direction = 'INBOUND' AND r.network_state = 'HTTP_RESPONSE_COMPLETE'
              AND r.protocol_verdict = 'NOT_RECEIVED' AND r.raw_body IS NOT NULL
              AND r.body_state IN ('FULL', 'LEGACY_REENCODED')
              AND r.stored_bytes = length(r.raw_body)
              {digest}
              AND {_refusal_echo(schema, "r.raw_body", "r.http_status")}
              AND r.body_record_id IS NULL
              AND {opening}
        ))"""
        complete = f"""AND NOT EXISTS (
            SELECT 1 FROM {schema}integration_record opening
            WHERE opening.exchange_id = m.exchange_id
              AND opening.parent_record_id = m.request_record_id
              AND opening.record_kind = 'SEND_ATTEMPT_OPENED' AND opening.direction = 'OUTBOUND'
              AND opening.attempt_id <> NEW.lease_id
              AND NOT EXISTS (
                  SELECT 1 FROM {schema}reliable_delivery_attempt original
                  WHERE original.message_id = m.id AND original.id = opening.id
                    AND original.lease_id = opening.attempt_id
              )
        )"""
        archived = f"""OR (
            NEW.request_wire IS NULL
            AND count(*) FILTER (WHERE {opening}) = count(*)
        )"""
    negative = f"""
        a.exchange_id = NEW.exchange_id AND a.request_record_id = NEW.request_record_id
        AND a.operation = NEW.operation AND a.operation_id = NEW.operation_id
        AND a.protocol = NEW.protocol AND a.finished_at IS NOT NULL
        AND a.business_key IS NOT DISTINCT FROM m.business_key
        AND ({request})
        AND (
            (a.attempt_state = 'NOT_SENT' AND a.response_state IS NULL
                AND a.response_record_id IS NULL)
            OR (a.response_state = 'RESPONSE_OBSERVED'
                AND a.response_record_id IS NOT NULL AND ({refusal}))
        )
    """
    return f"""
        EXISTS (
            SELECT 1
            FROM {schema}reliable_message m
            JOIN {schema}reliable_delivery_attempt a ON a.message_id = m.id
            LEFT JOIN {schema}reliable_retry_budget p
                ON p.target = m.target AND p.protocol = m.protocol
            WHERE m.id = NEW.message_id AND m.status = 'SENDING' AND m.lease_id = NEW.lease_id
              AND m.scope_owner IS NOT NULL AND m.delivery_snapshot IS NULL
              AND ({budget}) IS NOT NULL
              AND m.budget_checked_lease_id IS DISTINCT FROM NEW.lease_id
              {complete}
            GROUP BY m.id, p.target, p.retry_limit
            HAVING count(*) >= coalesce(m.dispatch_attempt_count, 0)
              AND (count(*) >= m.attempts - m.undispatched_attempts {archived})
              AND (m.dispatch_started_at IS NULL
                  OR count(*) FILTER (WHERE a.started_at = m.dispatch_started_at) > 0)
              AND count(*) FILTER (WHERE ({negative}) IS NOT TRUE) = 0
              AND count(*) FILTER (WHERE a.counts_toward_retry_budget) >= ({budget})
        )
    """


def _guarded_entry(schema, source):
    return f"""
        NEW.protocol = 'wms-transport' AND EXISTS (
            SELECT 1
            FROM {schema}reliable_message m
            JOIN {schema}transport_task t
                ON t.transport_task_id = m.business_key
                AND t.submission_exchange_id = NEW.exchange_id
                AND t.operation_id = NEW.operation_id
            WHERE m.id = NEW.message_id AND m.status = 'SENDING' AND m.lease_id = NEW.lease_id
              AND m.budget_checked_lease_id IS DISTINCT FROM NEW.lease_id
              AND t.owner_domain = 'xray' AND t.kind = 'RACK_MOVE' AND {source} = 'RACK'
        )
    """


def upgrade():
    from app.core import get_settings
    from app.reliable.config import parse_reliable_config, static_retry_budgets

    postgres = op.get_bind().dialect.name == "postgresql"
    schema = "enterprise_backend" if postgres else None
    policies = op.create_table(
        "reliable_retry_budget",
        sa.Column("target", sa.String(64), primary_key=True),
        sa.Column("protocol", sa.String(32), primary_key=True),
        sa.Column("retry_limit", sa.Integer()),
        schema=schema,
    )
    if configured := static_retry_budgets(parse_reliable_config(get_settings())):
        op.bulk_insert(policies, configured)
    op.add_column(
        "reliable_message", sa.Column("dispatch_retry_limit", sa.Integer()), schema=schema
    )
    op.add_column(
        "reliable_message", sa.Column("scope_stopped_lease_id", sa.String(64)), schema=schema
    )
    op.add_column(
        "reliable_message", sa.Column("budget_checked_lease_id", sa.String(64)), schema=schema
    )
    if postgres:
        op.execute(r"""
            CREATE FUNCTION enterprise_backend.reliable_dispatch_finite(number text) RETURNS boolean
            LANGUAGE plpgsql IMMUTABLE STRICT AS $$
            DECLARE parts text[];
                    digits text;
                    exponent text;
            BEGIN
                IF number !~ '[.eE]' OR pg_input_is_valid(number, 'double precision') THEN
                    RETURN true;
                END IF;
                -- The native cast also rejects underflow, which the adapter rounds to zero.
                parts := regexp_match(number, '^(-?[0-9]+)(\.([0-9]+))?([eE]([+-]?[0-9]+))?$');
                digits := ltrim(ltrim(parts[1], '-') || coalesce(parts[3], ''), '0');
                IF digits = '' THEN
                    RETURN true;
                END IF;
                exponent := coalesce(parts[5], '0');
                IF length(ltrim(exponent, '+-0')) > 9 THEN
                    RETURN left(exponent, 1) = '-';
                END IF;
                RETURN length(digits) - length(coalesce(parts[3], '')) + exponent::integer < 309;
            END;
            $$
        """)
        op.execute(r"""
            CREATE FUNCTION enterprise_backend.reliable_dispatch_reply(wire bytea) RETURNS json
            LANGUAGE plpgsql IMMUTABLE STRICT AS $function$
            DECLARE body text;
            BEGIN
                body := convert_from(wire, 'UTF8');
                IF body IS NOT JSON OBJECT THEN
                    RETURN NULL;
                END IF;
                IF EXISTS (
                    SELECT 1 FROM regexp_matches(body,
                        $p$"([^"\\]|\\.)*"|(-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?)$p$, 'gs'
                    ) AS fragment(token)
                    WHERE NOT enterprise_backend.reliable_dispatch_finite(token[2])
                ) THEN
                    RETURN NULL;
                END IF;
                IF body IS NOT JSON OBJECT WITH UNIQUE KEYS THEN
                    -- Preserve ASCII identity fields when JSON strings contain escapes
                    -- PostgreSQL cannot represent as text. Reserve the marker injectively.
                    SELECT string_agg(
                        CASE
                            WHEN token[1] ~ $p$^\\u(000[01]|[dD][89a-fA-F][0-9a-fA-F]{2})$$p$
                            THEN $marker$\u0001$marker$ || lower(substr(token[1], 3))
                            ELSE token[1] END, '' ORDER BY position
                    )
                    INTO body
                    FROM regexp_matches(body,
                        $pattern$([^\\]+|\\u[dD][89a-bA-B][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}|\\(u[0-9a-fA-F]{4}|.)|\\)$pattern$,
                        'gs'
                    ) WITH ORDINALITY AS fragment(token, position);
                    IF body IS NOT JSON OBJECT WITH UNIQUE KEYS THEN
                        RETURN NULL;
                    END IF;
                END IF;
                RETURN body::json;
            EXCEPTION WHEN data_exception THEN
                RETURN NULL;
            END;
            $function$
        """)
        op.execute(f"""
            CREATE FUNCTION enterprise_backend.fence_reliable_dispatch() RETURNS trigger
            LANGUAGE plpgsql AS $$
            DECLARE exhausted boolean;
            BEGIN
                IF {_guarded_entry("enterprise_backend.", "t.frozen->'source'->>'kind'")} THEN
                    RAISE check_violation USING MESSAGE = 'rack entry requires a guarded sender';
                END IF;
                IF has_table_privilege(
                    to_regclass('enterprise_backend.integration_record'), 'SELECT'
                ) THEN
                    exhausted := {_budget_exhausted("enterprise_backend.", legacy=True)};
                ELSE
                    exhausted := {_budget_exhausted("enterprise_backend.", legacy=False)};
                END IF;
                IF exhausted THEN
                    RAISE check_violation USING MESSAGE = 'scoped retry budget exhausted';
                END IF;
                RETURN NEW;
            END;
            $$
        """)
        op.execute("""
            CREATE TRIGGER reliable_dispatch_budget
            BEFORE INSERT ON enterprise_backend.reliable_delivery_attempt
            FOR EACH ROW EXECUTE FUNCTION enterprise_backend.fence_reliable_dispatch()
        """)
        op.execute("""
            CREATE FUNCTION enterprise_backend.capture_stopped_dispatch() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.status <> 'SENDING' THEN
                    NEW.lease_not_started := true;
                ELSIF OLD.lease_not_started IS FALSE THEN
                    NEW.scope_stopped_lease_id := OLD.lease_id;
                    NEW.delivery_uncertain := true;
                END IF;
                RETURN NEW;
            END;
            $$
        """)
        op.execute("""
            CREATE TRIGGER reliable_stopped_dispatch
            BEFORE UPDATE OF status ON enterprise_backend.reliable_message
            FOR EACH ROW WHEN (OLD.status <> 'ABANDONED'
                AND OLD.scope_owner IS NOT NULL AND NEW.status = 'ABANDONED'
                AND NEW.lease_id IS NULL)
            EXECUTE FUNCTION enterprise_backend.capture_stopped_dispatch()
        """)
    else:
        op.execute(f"""
            CREATE TRIGGER reliable_dispatch_budget
            BEFORE INSERT ON reliable_delivery_attempt
            FOR EACH ROW WHEN
                ({_guarded_entry("", "json_extract(t.frozen, '$.source.kind')")})
                OR ({_budget_exhausted("", legacy=True)})
            BEGIN
                SELECT RAISE(ABORT, 'reliable dispatch rejected');
            END
        """)
        op.execute("""
            CREATE TRIGGER reliable_stopped_dispatch
            AFTER UPDATE OF status ON reliable_message
            FOR EACH ROW WHEN OLD.status <> 'ABANDONED'
                AND OLD.scope_owner IS NOT NULL AND NEW.status = 'ABANDONED'
                AND NEW.lease_id IS NULL
            BEGIN
                UPDATE reliable_message
                SET lease_not_started = CASE WHEN OLD.status <> 'SENDING'
                        THEN true ELSE NEW.lease_not_started END,
                    scope_stopped_lease_id = CASE
                        WHEN OLD.status = 'SENDING' AND OLD.lease_not_started IS FALSE
                        THEN OLD.lease_id ELSE NEW.scope_stopped_lease_id END,
                    delivery_uncertain = CASE
                        WHEN OLD.status = 'SENDING' AND OLD.lease_not_started IS FALSE
                        THEN true ELSE NEW.delivery_uncertain END
                WHERE id = NEW.id;
            END
        """)


def downgrade():
    postgres = op.get_bind().dialect.name == "postgresql"
    schema = "enterprise_backend" if postgres else None
    if postgres:
        op.execute(
            "DROP TRIGGER reliable_dispatch_budget ON enterprise_backend.reliable_delivery_attempt"
        )
        op.execute("DROP FUNCTION enterprise_backend.fence_reliable_dispatch()")
        op.execute("DROP FUNCTION enterprise_backend.reliable_dispatch_reply(bytea)")
        op.execute("DROP FUNCTION enterprise_backend.reliable_dispatch_finite(text)")
        op.execute("DROP TRIGGER reliable_stopped_dispatch ON enterprise_backend.reliable_message")
        op.execute("DROP FUNCTION enterprise_backend.capture_stopped_dispatch()")
    else:
        op.execute("DROP TRIGGER reliable_dispatch_budget")
        op.execute("DROP TRIGGER reliable_stopped_dispatch")
    op.drop_column("reliable_message", "scope_stopped_lease_id", schema=schema)
    op.drop_column("reliable_message", "budget_checked_lease_id", schema=schema)
    op.drop_column("reliable_message", "dispatch_retry_limit", schema=schema)
    op.drop_table("reliable_retry_budget", schema=schema)
