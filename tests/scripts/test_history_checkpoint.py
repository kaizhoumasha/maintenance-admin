import json

import pytest


def test_checkpoint_replace_failure_preserves_committed_progress(tmp_path, monkeypatch):
    from scripts import backfill_integration_history as history

    path = tmp_path / "state.json"
    original = {"version": 1, "sources": {}}
    history.save_state(path, original)

    def failed(*args):
        raise OSError("interrupted before checkpoint")

    monkeypatch.setattr(history.os, "replace", failed)
    with pytest.raises(OSError):
        history.save_state(path, {"version": 1, "sources": {"callback_receipt": {"cursor": 42}}})
    assert json.loads(path.read_text()) == original


@pytest.mark.parametrize(
    "value",
    [
        {"version": True, "sources": {}},
        {"version": 1, "sources": {"unknown": {"cursor": 1}}},
        {"version": 1, "sources": {"callback_receipt": {"cursor": "sensitive-untrusted-value"}}},
        {"version": 1, "sources": {"callback_receipt": {"cursor": True}}},
        {"version": 1, "sources": {}, "connection": "secret"},
    ],
)
def test_checkpoint_rejects_unknown_fields_and_opaque_cursors(tmp_path, value):
    from scripts.backfill_integration_history import load_state

    path = tmp_path / "state.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        load_state(path)


def test_cli_failure_does_not_print_original_exception(tmp_path, monkeypatch, capsys):
    from scripts import backfill_integration_history as history

    def failed(coroutine):
        coroutine.close()
        raise ValueError("private-url-password-body")

    monkeypatch.setattr(history.asyncio, "run", failed)
    assert history.main(["verify", "--state", str(tmp_path / "state.json")]) == 2
    output = capsys.readouterr().out
    assert json.loads(output) == {"error": "HISTORY_MIGRATION_FAILED", "sources_retained": True}
