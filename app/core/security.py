import hashlib
import hmac
import os

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
