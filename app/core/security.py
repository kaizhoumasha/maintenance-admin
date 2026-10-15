import hashlib
import hmac
import os
import uuid
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


def _encode(sub: str, token_type: str, ttl: timedelta, **extra) -> str:
    now = datetime.now(UTC)
    payload = {"sub": sub, "type": token_type, "iat": now, "exp": now + ttl, **extra}
    return jwt.encode(payload, get_settings().JWT_SECRET_KEY, algorithm=ALGORITHM)


def create_access_token(sub: str, expires_minutes: int | None = None) -> str:
    settings = get_settings()
    minutes = settings.ACCESS_TOKEN_EXPIRE_MINUTES if expires_minutes is None else expires_minutes
    return _encode(sub, "access", timedelta(minutes=minutes))


def create_refresh_token(sub: str) -> tuple[str, str]:
    """返回 (token, jti);调用方负责把 jti 存进 Redis。"""
    jti = uuid.uuid4().hex
    ttl = timedelta(seconds=get_settings().REFRESH_TOKEN_EXPIRE_SECONDS)
    return _encode(sub, "refresh", ttl, jti=jti), jti


def decode_token(token: str, token_type: str = "access") -> dict:
    """验签 + 验 type;access/refresh 混用一律 401。"""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as e:
        raise AppError(ErrorCode.UNAUTHORIZED, "凭证已过期") from e
    except jwt.InvalidTokenError as e:
        raise AppError(ErrorCode.UNAUTHORIZED, "凭证无效") from e
    if payload.get("type") != token_type:
        raise AppError(ErrorCode.UNAUTHORIZED, "凭证类型错误")
    return payload
