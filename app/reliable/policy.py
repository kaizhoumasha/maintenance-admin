from collections.abc import Callable
from math import ldexp, log2

from .config import TargetConfig


def retry_delay_s(
    attempt_no: int,
    target: TargetConfig,
    retry_after_ms: float | None,
    uniform: Callable[[float, float], float],
) -> float:
    exponent = max(0, attempt_no - 1)
    ceiling = target.retry_cap_s
    if target.retry_base_s < ceiling and exponent < log2(ceiling) - log2(target.retry_base_s):
        ceiling = min(ceiling, ldexp(target.retry_base_s, exponent))
    local = uniform(0, ceiling)
    return (
        max(local, min(retry_after_ms / 1000, target.max_retry_after_s))
        if retry_after_ms is not None
        else local
    )
