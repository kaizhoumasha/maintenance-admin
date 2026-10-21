import math

import pytest

from app.core.config import Settings


def test_retry_hint_limit_env_defaults_to_3600(monkeypatch):
    from app.reliable.config import parse_reliable_config

    monkeypatch.delenv("RELIABLE_MAX_RETRY_AFTER_S", raising=False)
    settings = Settings(_env_file=None)
    assert settings.RELIABLE_MAX_RETRY_AFTER_S == 3600
    monkeypatch.setenv("RELIABLE_MAX_RETRY_AFTER_S", "1200")
    assert Settings(_env_file=None).RELIABLE_MAX_RETRY_AFTER_S == 1200
    config = parse_reliable_config(
        Settings(
            _env_file=None,
            RELIABLE_TARGETS={
                "defaults": {"max_retry_after_s": 900},
                "targets": {
                    "peer": {"base_url": "http://example.test", "protocol": "script"},
                    "other": {
                        "base_url": "http://example.test",
                        "protocol": "script",
                        "max_retry_after_s": 600,
                    },
                },
            },
        )
    )
    assert config.targets["peer"].max_retry_after_s == 900
    assert config.targets["other"].max_retry_after_s == 600


@pytest.mark.parametrize("value", [0, -1, True, math.nan, math.inf])
def test_invalid_retry_limit_rejected(value):
    from app.reliable.config import TargetConfig

    with pytest.raises((ValueError, TypeError)):
        TargetConfig("peer", "http://example.test", "script", max_retry_after_s=value)


@pytest.mark.parametrize("value", [0, -1, 1.5, True, math.nan, math.inf])
def test_invalid_maintenance_batch_rejected(value):
    from app.reliable.config import ReliableConfig

    with pytest.raises((ValueError, TypeError)):
        ReliableConfig({}, maintenance_batch_size=value)


def test_retry_policy_saturates_and_preserves_peer_hint():
    from app.reliable.config import TargetConfig
    from app.reliable.policy import retry_delay_s

    target = TargetConfig("peer", "http://example.test", "script")

    def maximum(low, high):
        return high

    assert retry_delay_s(1, target, None, maximum) == 1
    assert retry_delay_s(1000000, target, None, maximum) == 300
    assert retry_delay_s(1, target, 600000, maximum) == 600
    assert retry_delay_s(1, target, 7200000, maximum) == 3600


@pytest.mark.parametrize(
    "setting",
    ["RELIABLE_MAX_RETRY_AFTER_S", "RELIABLE_DEFAULT_TIMEOUT_MS", "RELIABLE_MAX_BODY_BYTES"],
)
def test_invalid_defaults_rejected_even_without_targets(setting):
    from app.reliable.config import parse_reliable_config

    with pytest.raises((ValueError, TypeError)):
        parse_reliable_config(Settings(_env_file=None, **{setting: 0}))


def test_retry_policy_avoids_intermediate_overflow_for_small_finite_base():
    from app.reliable.config import TargetConfig
    from app.reliable.policy import retry_delay_s

    target = TargetConfig("peer", "http://example.test", "script", retry_base_s=5e-324)
    value = retry_delay_s(1025, target, None, lambda low, high: high)
    assert 0 < value <= 300 and math.isfinite(value)


@pytest.mark.parametrize("value", [0, -1, 1.5, True, math.nan, "3"])
def test_invalid_not_received_budget_rejected(value):
    from app.reliable.config import TargetConfig

    with pytest.raises((ValueError, TypeError)):
        TargetConfig("peer", "http://example.test", "script", max_not_received_attempts=value)


@pytest.mark.parametrize("value", [0, -1, True, math.nan, math.inf, "30"])
def test_invalid_fixed_retry_interval_rejected(value):
    from app.reliable.config import TargetConfig

    with pytest.raises((ValueError, TypeError)):
        TargetConfig("peer", "http://example.test", "script", fixed_retry_interval_s=value)


def test_budget_and_fixed_interval_come_from_defaults_and_target_overrides():
    from app.reliable.config import TargetConfig, parse_reliable_config

    config = parse_reliable_config(
        Settings(
            _env_file=None,
            RELIABLE_TARGETS={
                "defaults": {"max_not_received_attempts": 3, "fixed_retry_interval_s": 30},
                "targets": {
                    "a": {"base_url": "http://example.test", "protocol": "script"},
                    "b": {
                        "base_url": "http://example.test",
                        "protocol": "script",
                        "max_not_received_attempts": 5,
                    },
                },
            },
        )
    )
    assert (
        config.targets["a"].max_not_received_attempts,
        config.targets["a"].fixed_retry_interval_s,
    ) == (3, 30)
    assert config.targets["b"].max_not_received_attempts == 5
    plain = TargetConfig("peer", "http://example.test", "script")
    assert plain.max_not_received_attempts is None and plain.fixed_retry_interval_s is None
