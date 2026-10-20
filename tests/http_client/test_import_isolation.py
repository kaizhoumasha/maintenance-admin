import os
import subprocess
import sys
from pathlib import Path


def test_import_does_not_load_application_dependencies(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = f"""
import sys
sys.path.insert(0, {str(root)!r})
import app.http_client
app.http_client.validate_request(
    app.http_client.HttpClientConfig('http://example.test', 'isolated'),
    app.http_client.HttpRequest('POST', '/', body=b'opaque'),
)
for name in (
    'app.core', 'sqlalchemy', 'redis', 'celery', 'fastapi', 'app.models',
    'app.services', 'app.controllers', 'app.reliable',
):
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
    assert result.returncode == 0, result.stderr
