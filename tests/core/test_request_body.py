from types import SimpleNamespace

from app.core.request_body import read_body_capped


def _request(chunks: list[bytes], consumed: list[int] | None = None):
    async def stream():
        for chunk in chunks:
            if consumed is not None:
                consumed.append(len(chunk))
            yield chunk

    return SimpleNamespace(stream=stream)


async def test_returns_whole_body_within_limit():
    assert await read_body_capped(_request([b"ab", b"cd"]), 10) == b"abcd"


async def test_stops_reading_one_byte_past_limit():
    consumed: list[int] = []
    request = _request([b"a" * 100, b"b" * 100, b"c" * 100], consumed)

    result = await read_body_capped(request, 100)

    assert len(result) == 101
    assert len(consumed) == 2


async def test_empty_body_is_empty_bytes():
    assert await read_body_capped(_request([]), 100) == b""
