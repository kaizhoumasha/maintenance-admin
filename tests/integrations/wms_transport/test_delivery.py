import asyncio
import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.http_client import AsyncHttpClient, DeliveryState, HttpClientConfig, HttpResult
from app.integrations.wms_transport import Rack, RackPosition, TransportCaller
from app.models import ReliableMessage
from app.reliable.sender import Sender
from app.reliable.store import MessageStore, cleanup_terminal
from app.reliable.testing import ScriptedHttpClient


def ack(raw, code):
    request = json.loads(raw)
    return json.dumps(
        {
            "operation_id": request["operation_id"],
            "code": code,
            "timestamp": request["timestamp"],
            "data": {"transport_task_id": request["data"]["transport_task_id"]},
        }
    ).encode()


def sender_for(rig, client):
    return Sender(
        MessageStore(rig.factory, clock=rig.clock),
        {rig.target.name: rig.target},
        rig.adapters,
        {rig.target.name: client},
        clock=rig.clock,
    )


async def deliver(sender):
    assert await sender.tick() == 1
    await asyncio.wait_for(asyncio.gather(*tuple(sender.inflight)), 5)


async def test_scripted_ack_stops_delivery_of_frozen_request(transport_storage, transport_request):
    rig = transport_storage
    async with rig.factory() as session:
        handle = await rig.service.move_rack(session, **transport_request)
        await session.commit()
        message = await session.scalar(
            select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
        )
        raw = message.request_raw
    client = ScriptedHttpClient(
        HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 202, (), ack(raw, "RECEIVED"))
    )
    sender = sender_for(rig, client)
    try:
        await deliver(sender)
        assert client.requests[0].body == raw
        assert await sender.tick() == 0
        async with rig.factory() as session:
            assert (
                await session.scalar(
                    select(ReliableMessage.status).where(ReliableMessage.target == rig.target.name)
                )
                == "DONE"
            )
        rig.clock.advance(91 * 86400)
        async with rig.factory() as session:
            await cleanup_terminal(session, now=rig.clock(), retention_days=90)
            await session.commit()
            assert await rig.service.move_rack(session, **transport_request) == handle
            await session.commit()
            assert (
                await session.scalar(
                    select(ReliableMessage.id).where(ReliableMessage.target == rig.target.name)
                )
                is None
            )
        assert await sender.tick() == 0
    finally:
        await sender.aclose()


@pytest.mark.parametrize("lose_response", [False, True])
async def test_real_http_preserves_bytes_and_recovers_lost_response(
    pg_transport_rig, transport_http_server, lose_response
):
    rig = pg_transport_rig

    async def peer(raw, writer, number):
        if lose_response and number == 1:
            writer.close()
            return
        code, status = ("DUPLICATE", 200) if lose_response else ("RECEIVED", 202)
        body = ack(raw, code)
        writer.write(f"HTTP/1.1 {status} OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()

    async with transport_http_server(peer) as (url, requests):
        rig.target = replace(rig.target, base_url=url)
        rig.outbox.targets = {rig.target.name: rig.target}
        async with rig.factory() as session:
            handle = await rig.service.move_rack(
                session,
                client_request_id="01a13b00-cabd-7390-890a-0c093e7c07a1",
                caller=TransportCaller(rig.target.name, "station-local"),
                rack_id="货架",
                source=Rack("货架"),
                target=RackPosition("目标"),
                target_face=" 北 ",
            )
            await session.commit()
            message = await session.scalar(
                select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
            )
            raw, operation_id = message.request_raw, message.operation_id

        def new_sender():
            client = AsyncHttpClient(HttpClientConfig(url, rig.target.name))
            return sender_for(rig, client)

        sender = new_sender()
        try:
            await deliver(sender)
            if lose_response:
                async with rig.factory() as session:
                    message = await session.scalar(
                        select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
                    )
                    assert message.status == "PENDING" and message.delivery_uncertain
                    assert message.not_received_count == 0
                await sender.aclose()
                rig.clock.advance(1)
                sender = new_sender()
                await deliver(sender)
            async with rig.factory() as session:
                message = await session.scalar(
                    select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
                )
                assert message.status == "DONE" and message.request_raw == raw
                assert message.operation_id == operation_id
                assert (
                    json.loads(message.response_raw)["data"]["transport_task_id"]
                    == handle.transport_task_id
                )
            rig.clock.advance(3600)
            assert await sender.tick() == 0
            assert len(requests) == (2 if lose_response else 1)
            for headers, body in requests:
                assert headers.startswith(b"POST /api/v1/wes/transport-requests HTTP/1.1\r\n")
                assert b"content-type: application/json\r\n" in headers.lower()
                assert body == raw and b"station-local" not in body
        finally:
            await sender.aclose()
