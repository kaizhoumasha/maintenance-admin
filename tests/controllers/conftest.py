import pytest


@pytest.fixture
async def mock_mode(client, admin):
    """切换 Mock WMS 的响应模式,用完恢复成 accept。"""

    async def use(mode):
        r = await client.put("/api/v1/dev/mock-wms/mode", json={"mode": mode}, headers=admin)
        assert r.status_code == 200

    yield use
    await use("accept")
