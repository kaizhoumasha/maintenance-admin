import asyncio
import os
import subprocess
import sys

import pytest

PROBE = '''
import sqlite3
import sys
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, event

path, table, revision = sys.argv[1:]
config = Config("alembic.ini")
command.upgrade(config, "head")
if table == "transport_task":
    trigger = "DROP TABLE transport_task"
    insert = """INSERT INTO transport_task
        (client_request_id, transport_task_id, operation_id, kind, input_digest,
         workline_id, created_at)
        VALUES ('client', 'task', 'operation', 'RACK_MOVE', 'digest', 'line', CURRENT_TIMESTAMP)"""
else:
    trigger = "CREATE TABLE _alembic_tmp_reliable_message"
    insert = """INSERT INTO reliable_message
        (target, operation, operation_id, request_raw, status, attempts, next_attempt_at,
         delivery_uncertain, not_received_count, created_at, updated_at)
        VALUES ('target', 'operation', 'id', X'00', 'PENDING', 1, CURRENT_TIMESTAMP,
                1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""

@event.listens_for(Engine, "before_cursor_execute")
def competing_writer(connection, cursor, statement, parameters, context, many):
    if statement.strip().startswith(trigger):
        with sqlite3.connect(path, timeout=0.05) as caller:
            try:
                caller.execute(insert)
                caller.commit()
                print("competing-writer=committed")
            except sqlite3.OperationalError as error:
                if "locked" not in str(error):
                    raise
                print("competing-writer=blocked")

command.downgrade(config, revision)
'''


@pytest.mark.parametrize(
    "table,revision",
    [
        ("transport_task", "a019c20261022"),
        ("reliable_message", "a019b20261021"),
    ],
)
async def test_sqlite_downgrade_blocks_writes_between_safety_check_and_schema_change(
    tmp_path, table, revision
):
    path = tmp_path / "guard.db"
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", PROBE, str(path), table, revision],
        env={**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{path}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "competing-writer=blocked" in result.stdout
    assert "competing-writer=committed" not in result.stdout
