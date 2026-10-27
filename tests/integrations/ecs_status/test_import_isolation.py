import os
import subprocess
import sys
from pathlib import Path


def test_public_query_needs_no_application_environment(tmp_path):
    root = Path(__file__).resolve().parents[3]
    script = f"""
import sys, asyncio
sys.path.insert(0, {str(root)!r})
from app.integrations.ecs_status import query
from app.http_client import HttpResult, DeliveryState

class Client:
    async def send(self, request):
        return HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200,
                          (('content-type', 'application/json'),), b'{{"devices":[]}}')
    async def aclose(self):
        pass

async def run():
    result = await query('http://127.0.0.1', client_factory=lambda config: Client())
    assert result.devices == ()
asyncio.run(run())
for name in ('app.core', 'app.models', 'app.main', 'sqlalchemy', 'redis', 'celery',
             'fastapi', 'app.services', 'app.controllers', 'app.reliable',
             'app.integrations.registration', 'app.integrations.ecs_command'):
    assert not any(key == name or key.startswith(name + '.') for key in sys.modules), name
"""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"DATABASE_URL", "REDIS_URL", "JWT_SECRET_KEY", "PYTHONPATH"}
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
