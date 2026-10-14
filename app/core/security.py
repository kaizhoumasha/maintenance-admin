import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode

ALGORITHM = "HS256"

# hash 与 verify 必须用同一组参数,集中在这里定义
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def _scrypt(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT).hex()


def hash_password(password: str) -> str:
    """返回 `盐hex$哈希hex`。scrypt 是 CPU 密集操作,async 代码里要放线程池调用。"""
    salt = os.urandom(16)
    return f"{salt.hex()}${_scrypt(password, salt)}"


def verify_password(password: str, hashed: str) -> bool:
    salt, digest = hashed.split("$")
    return hmac.compare_digest(_scrypt(password, bytes.fromhex(salt)), digest)


def create_access_token(sub: str, expires_minutes: int | None = None) -> str:
    settings = get_settings()
    minutes = settings.ACCESS_TOKEN_EXPIRE_MINUTES if expires_minutes is None else expires_minutes
    now = datetime.now(UTC)
    payload = {
        "sub": sub,
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as e:
        raise AppError(ErrorCode.UNAUTHORIZED, "凭证已过期") from e
    except jwt.InvalidTokenError as e:
        raise AppError(ErrorCode.UNAUTHORIZED, "凭证无效") from e
