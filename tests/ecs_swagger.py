"""Owned loopback simulators for isolated Swagger validation, never a production startup."""

import json
from contextlib import AsyncExitStack, asynccontextmanager
from unittest.mock import patch

from fastapi import FastAPI, Request

from app.integrations.ecs_command.contracts import CommandEnvelope
from tests.helpers import loopback_app, simulator_client_factory


def create_app(*, history_annexes=None):
    from app.main import app, configure_local_ecs, lifespan

    @asynccontextmanager
    async def isolated(application):
        async with AsyncExitStack() as stack:
            origins = []
            hits = []
            raw_requests = {}
            for _ in range(2):
                peer = FastAPI()

                @peer.post("/api/v1/device/command")
                async def acknowledge(body: CommandEnvelope, request: Request):
                    hits.append(body.model_dump())
                    raw_requests[body.command_code] = await request.body()
                    return {"code": 200, "message": "ACK"}

                origins.append(await stack.enter_async_context(loopback_app(peer)))
            application.state.ecs_simulator_origins = tuple(origins)
            application.state.ecs_simulator_hits = hits
            application.state.ecs_simulator_raw = raw_requests
            factory = simulator_client_factory(origins)

            def assemble(application, *, environment):
                return configure_local_ecs(
                    application,
                    environment=environment,
                    history_annexes=history_annexes,
                    client_factory=factory,
                )

            print("ECS isolated simulator origins: " + json.dumps(origins), flush=True)
            with patch("app.main.configure_local_ecs", assemble):
                async with lifespan(application):
                    yield

    app.router.lifespan_context = isolated
    return app
