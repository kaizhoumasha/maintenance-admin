from fastapi import Request


async def read_body_capped(request: Request, limit: int) -> bytes:
    """分块读取请求体,读到 limit + 1 字节即停;`len(result) > limit` 表示超限。"""
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk[: limit + 1 - len(raw)])
        if len(raw) > limit:
            break
    return bytes(raw)
