"""Supplied factory profile, formal runtime, and loopback peer for acceptance tests."""

import json
from pathlib import Path

from tests.helpers import formal_manual_fixture

PROFILE = json.loads((Path(__file__).parent / "factory_source_profile.json").read_text())


def profile_fixture(factory, monkeypatch, evidence_dir, evidence_name):
    """Original formal runtime assembly, supplied codes, and an unlimited loopback peer."""
    return formal_manual_fixture(
        factory,
        monkeypatch,
        source_profile=PROFILE,
        evidence_path=(evidence_dir, evidence_name),
    )
