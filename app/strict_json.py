import hashlib
import json


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


def json_object(raw: bytes) -> dict | None:
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_reject_constant
        )
    except (UnicodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def canonical_digest(value: object) -> str:
    """规范化 JSON(键排序、无空白、拒绝 NaN)的 SHA-256,收据与任务层共用同一算法。"""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()
