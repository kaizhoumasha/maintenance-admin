import logging

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import ProgrammingError

from app.core.logger import DevFormatter
from app.services import user_service


@pytest.mark.parametrize("kind,status", [("database", 500), ("redis", 503), ("runtime", 500)])
async def test_http_technical_errors_have_safe_diagnostics(
    client, admin, monkeypatch, caplog, kind, status
):
    driver = RuntimeError("column reliable_message.delivery_snapshot does not exist")
    driver.sqlstate = "42703"
    errors = {
        "database": ProgrammingError("private_sql", {"password": "private_parameter"}, driver),
        "redis": RedisConnectionError("private_redis_password"),
        "runtime": RuntimeError("private_runtime_message"),
    }

    async def fail(*args):
        raise errors[kind] from ValueError("private_chain")

    monkeypatch.setattr(user_service, "get_user", fail)
    with caplog.at_level(logging.ERROR):
        response = await client.get("/api/v1/users/1", headers=admin)
    assert response.status_code == status
    assert "private_" not in response.text
    records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert records and any(getattr(r, "component", None) == "http" for r in records)
    rendered = "\n".join(DevFormatter(color=False).format(r) for r in records)
    assert "origin=" in rendered and "Traceback" in rendered
    assert "private_" not in rendered and "private_" not in caplog.text
    if kind == "database":
        assert "sqlstate=42703" in rendered
        assert "missing=reliable_message.delivery_snapshot" in rendered
        assert "alembic upgrade head" in rendered


@pytest.mark.parametrize(
    "module,receiver,path",
    [
        ("ecs_result", "receive_ecs_result", "/api/v1/callback/result"),
        ("ecs_callback", "receive_ecs_event", "/api/v1/callback/event"),
        ("wms_events", "receive", "/api/v1/wms/events"),
    ],
)
async def test_callback_failures_keep_protocol_and_safe_diagnostics(
    client, monkeypatch, caplog, module, receiver, path
):
    import importlib

    from app.main import app

    controller = importlib.import_module("app.controllers." + module)

    async def fail(*args):
        raise RuntimeError("private_callback_message")

    monkeypatch.setattr(controller, receiver, fail)
    monkeypatch.setattr(app.state, "ecs_commands", object(), raising=False)
    if hasattr(controller, "allow_snapshot"):
        monkeypatch.setattr(controller, "allow_snapshot", lambda *a: False)
    response = await client.post(path, json={})
    assert response.status_code == 503
    assert "private_" not in response.text and "private_" not in caplog.text
    records = [r for r in caplog.records if getattr(r, "component", None) == "http"]
    assert records and "origin=" in records[0].getMessage()
    assert "Traceback" in records[0].getMessage()


async def test_common_diagnostic_context_only_keeps_identifiers(caplog):
    from app.diagnostics import log_exception

    logger = logging.getLogger("tests.safe_diagnostic")
    try:
        raise RuntimeError("private_error_message") from ValueError("private_chain")
    except RuntimeError as error:
        log_exception(
            logger,
            "operation failed",
            error,
            component="test",
            phase="execute",
            context={
                "task_id": "T1",
                "password": "private_password",
                "args": "private_args",
                "error_type": "private_override",
            },
        )
    record = next(r for r in caplog.records if r.name == logger.name)
    assert record.task_id == "T1" and record.error_type == "RuntimeError"
    assert "private_" not in caplog.text
    assert "password" not in record.__dict__ and "args" not in record.getMessage()
