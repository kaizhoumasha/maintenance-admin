import httpx
import pytest

from app.http_client import AsyncHttpClient
from tests.helpers import ecs_status_document


@pytest.fixture
def http_factory():
    def make(handler):
        configs, clients, requests = [], [], []

        async def handle(request):
            requests.append(request)
            return await handler(request)

        def factory(config):
            configs.append(config)
            client = AsyncHttpClient(config, transport=httpx.MockTransport(handle))
            clients.append(client)
            return client

        return factory, configs, clients, requests

    return make


@pytest.fixture
def status_document():
    return ecs_status_document
