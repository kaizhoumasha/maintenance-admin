from app.core.security import hash_password, verify_password


def test_hash_and_verify_roundtrip():
    hashed = hash_password("password123")
    assert verify_password("password123", hashed)
    assert not verify_password("wrong-password", hashed)


def test_same_password_hashes_differently():
    # 每次随机盐,同一密码的哈希不同
    assert hash_password("password123") != hash_password("password123")
