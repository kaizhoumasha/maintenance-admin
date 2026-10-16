import pytest
from pydantic import ValidationError

from app.core.config import Settings

BASE = {"DATABASE_URL": "sqlite+aiosqlite://", "REDIS_URL": "redis://x/0"}


def _settings(**kw):
    return Settings(_env_file=None, **BASE, **kw)


def test_weak_jwt_secret_rejected_outside_local():
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
        _settings(ENVIRONMENT="production", JWT_SECRET_KEY="secret-string")


def test_strong_jwt_secret_ok_outside_local():
    _settings(ENVIRONMENT="production", JWT_SECRET_KEY="x" * 32)


def test_weak_jwt_secret_allowed_in_local():
    _settings(ENVIRONMENT="local", JWT_SECRET_KEY="secret-string")


def test_cors_origins_parsed_from_comma_list():
    s = _settings(JWT_SECRET_KEY="k", CORS_ORIGINS=" http://a.com, http://b.com ,")
    assert s.cors_origins == ["http://a.com", "http://b.com"]
    assert _settings(JWT_SECRET_KEY="k", CORS_ORIGINS="").cors_origins == []


def test_app_name_drives_title_celery_and_logger():
    # 子进程里设置 APP_NAME:模块导入时就取名,当前进程已导入过,没法再改
    import os
    import subprocess
    import sys

    code = (
        "from app.main import app; from app.tasks.celery_app import celery_app;"
        "from app.core import logger; print(app.title, celery_app.main, logger.name)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "APP_NAME": "demo-admin"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.split() == ["demo-admin"] * 3
