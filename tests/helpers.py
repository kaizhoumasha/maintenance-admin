"""跨测试文件共享的辅助函数(夹具放 conftest.py,这里放普通函数)。"""

import base64
import json
from contextlib import asynccontextmanager
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.security import create_access_token

PAYLOAD = {"username": "kai", "email": "k@example.com", "password": "password123"}


def bearer(user_id: int | str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


async def make_user_with_perms(client, admin, perms):
    """建一个只拥有 perms 的用户(经 API 走一遍 角色→权限→用户),返回其 id。"""
    role = (
        await client.post("/api/v1/roles", json={"name": "r", "permissions": perms}, headers=admin)
    ).json()
    uid = (await client.post("/api/v1/users", json=PAYLOAD, headers=admin)).json()["id"]
    r = await client.put(
        f"/api/v1/users/{uid}/roles", json={"role_ids": [role["id"]]}, headers=admin
    )
    assert r.status_code == 204
    return uid


def raw_cursor(payload: Any) -> str:
    """按对外约定(base64url、无填充的 JSON)手工构造游标,用来造非法或边界输入。"""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


class TrialPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    qty: int


def ecs_history_annexes():
    from app.integrations.ecs_command.contracts import DeviceAnnex, ErrorDetail

    return {("trial", "1"): DeviceAnnex(TrialPayload, TrialPayload, ErrorDetail, 1, 60000)}


def ecs_protocol_annexes():
    from app.integrations.ecs_command.dev import PROTOCOL_ANNEX, PROTOCOL_MODELS

    return {PROTOCOL_ANNEX: PROTOCOL_MODELS}


def reset_ecs_assembly(app, monkeypatch):
    for key, value in (
        ("ecs_commands", None),
        ("reliable_lifecycles", {}),
        ("reliable_client_provider", None),
        (
            "reliable_adapters",
            {k: v for k, v in app.state.reliable_adapters.items() if k != "ecs-command"},
        ),
    ):
        monkeypatch.setattr(app.state, key, value, raising=False)


def simulator_client_factory(origins):
    from app.http_client import AsyncHttpClient
    from app.integrations.ecs_command.dev import debug_origin
    from app.reliable.client_provider import PoolUnavailable

    allowed = frozenset(debug_origin(origin) for origin in origins)

    def factory(config):
        if debug_origin(config.base_url) not in allowed:
            raise PoolUnavailable("Test ECS destination is outside this run")
        return AsyncHttpClient(config)

    return factory


@asynccontextmanager
async def simulator_sender(ecs, origins):
    from app.integrations.ecs_command.clients import DynamicClients
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import PROTOCOL
    from app.reliable.sender import Sender
    from app.reliable.store import MessageStore

    sender = Sender(
        MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)}),
        ecs.targets,
        ecs.adapters,
        {},
        provider=DynamicClients(factory=simulator_client_factory(origins)),
    )
    try:
        yield sender
    finally:
        await sender.aclose()


@asynccontextmanager
async def loopback_app(app):
    import asyncio
    import socket

    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            loop="asyncio",
            lifespan="off",
            log_level="critical",
            access_log=False,
            ws="none",
        )
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("loopback server exited before startup")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        finally:
            sock.close()
