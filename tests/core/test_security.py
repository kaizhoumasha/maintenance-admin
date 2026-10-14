import jwt
import pytest

from app.core.errors import AppError, ErrorCode
from app.core.security import (
    ALGORITHM,
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)


def test_hash_and_verify_roundtrip():
    hashed = hash_password("password123")
    assert verify_password("password123", hashed)
    assert not verify_password("wrong-password", hashed)


def test_same_password_hashes_differently():
    # 每次随机盐,同一密码的哈希不同
    assert hash_password("password123") != hash_password("password123")


def test_token_roundtrip():
    assert decode_token(create_access_token("42"))["sub"] == "42"


def test_expired_token_rejected():
    with pytest.raises(AppError) as e:
        decode_token(create_access_token("42", expires_minutes=-1))
    assert e.value.code == ErrorCode.UNAUTHORIZED and e.value.message == "凭证已过期"


def test_zero_minutes_means_expire_now_not_default():
    with pytest.raises(AppError) as e:
        decode_token(create_access_token("42", expires_minutes=0))
    assert e.value.message == "凭证已过期"


def test_token_signed_with_other_key_rejected():
    forged = jwt.encode({"sub": "42"}, "not-the-secret-key-not-the-secret-key", algorithm=ALGORITHM)
    with pytest.raises(AppError) as e:
        decode_token(forged)
    assert e.value.code == ErrorCode.UNAUTHORIZED and e.value.message == "凭证无效"
