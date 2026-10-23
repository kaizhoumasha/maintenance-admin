import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import create_database_engine as create_async_engine
from app.http_client import DeliveryState, HttpRequest, HttpResult
from app.reliable import Verdict, VerdictKind
from app.reliable.config import TargetConfig
from app.reliable.delivery import send_one
from app.reliable.store import MessageStore
from app.reliable.testing import FakeClock, FaultInjectingHttpClient, ScriptedHttpClient


async def main():
    target = TargetConfig(os.environ["CRASH_TARGET"], "http://example.test", "script")
    clock = FakeClock(datetime.fromisoformat(os.environ["CRASH_NOW"]))
    evidence = Path(os.environ["CRASH_EVIDENCE"])
    phase = os.environ["CRASH_PHASE"]
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"], hide_parameters=True)
    store = MessageStore(
        async_sessionmaker(engine, expire_on_commit=False),
        clock=clock,
        uniform=lambda low, high: high,
    )
    targets = {target.name: target}

    class Adapter:
        def build_request(self, message, config):
            return HttpRequest(
                "POST",
                "/opaque",
                body=message.request_raw,
                headers=(
                    ("X-Operation", message.operation),
                    ("X-Operation-Id", message.operation_id),
                ),
            )

        def classify_response(self, message, result):
            return Verdict(VerdictKind.DELIVERED, response=result.body)

    def record(request):
        with evidence.open("a") as output:
            output.write(
                json.dumps({"headers": request.headers, "body": request.body.hex()}) + "\n"
            )
            output.flush()
            os.fsync(output.fileno())

    async def accepted(request, result):
        await asyncio.to_thread(record, request)
        if phase == "after_send":
            os._exit(73)

    client = FaultInjectingHttpClient(
        ScriptedHttpClient(
            HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), b"accepted")
        ),
        after=accepted,
    )
    try:
        if phase == "recover":
            assert await store.recover_expired(4, targets) == 1
            clock.advance(1)
        (claim,) = await store.claim(1, targets)
        if phase == "after_claim":
            os._exit(73)
        await send_one(
            claim,
            store=store,
            targets=targets,
            adapters={"script": Adapter()},
            clients={target.name: client},
            clock=clock,
            uniform=lambda low, high: high,
        )
    finally:
        await client.aclose()
        await engine.dispose()
        evidence.with_suffix(".closed").write_text("graceful")


if __name__ == "__main__":
    asyncio.run(main())
