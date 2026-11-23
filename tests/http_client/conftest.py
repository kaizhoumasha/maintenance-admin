import asyncio
from contextlib import asynccontextmanager, suppress

import pytest


@pytest.fixture
async def http_server():
    @asynccontextmanager
    async def serve(handler):
        tasks = set()
        writers = set()
        stats = {"connections": 0, "requests": 0}

        async def connected(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            writers.add(writer)
            stats["connections"] += 1
            try:
                while True:
                    raw = await reader.readuntil(b"\r\n\r\n")
                    length = next(
                        (
                            int(line.split(b":", 1)[1])
                            for line in raw.split(b"\r\n")
                            if line.lower().startswith(b"content-length:")
                        ),
                        0,
                    )
                    await reader.readexactly(length)
                    stats["requests"] += 1
                    await handler(raw, writer, stats)
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writers.discard(writer)
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()
                tasks.discard(task)

        server = await asyncio.start_server(connected, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}", stats
        finally:
            server.close()
            for writer in tuple(writers):
                writer.close()
            pending = tuple(tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await server.wait_closed()

    return serve
