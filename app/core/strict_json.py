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
