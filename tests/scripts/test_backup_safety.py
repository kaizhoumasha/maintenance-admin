import os
import subprocess
import time
from pathlib import Path

SCRIPTS = Path(__file__).parents[2] / "scripts"


def test_backup_removes_only_expired_backups(tmp_path):
    old = tmp_path / "db-old.dump"
    recent = tmp_path / "db-recent.dump"
    unrelated = tmp_path / "keep.txt"
    for path in (old, recent, unrelated):
        path.touch()
    expired = time.time() - 10 * 86400
    for path in (old, unrelated):
        os.utime(path, (expired, expired))
    result = subprocess.run(
        ["bash", str(SCRIPTS / "backup_pg.sh")],
        env={**os.environ, "BACKUP_DIR": str(tmp_path), "BACKUP_KEEP_DAYS": "7", "PG_EXEC": "true"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not old.exists()
    assert recent.exists() and unrelated.exists()
    assert Path(result.stdout.strip()).is_file()


def test_restore_without_confirmation_never_contacts_database(tmp_path):
    backup = tmp_path / "backup.dump"
    backup.touch()
    result = subprocess.run(
        ["bash", str(SCRIPTS / "restore_pg.sh"), str(backup)],
        env={**os.environ, "CONFIRM": "", "JWT_ROTATED": "yes", "PG_EXEC": "false"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "CONFIRM=yes" in result.stderr
