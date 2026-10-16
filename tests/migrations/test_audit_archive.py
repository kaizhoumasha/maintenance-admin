import asyncio
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select, text

from app.models import AuditLog, AuditLogArchive, Permission, Role, role_permission
from app.services.audit_service import archive_old


async def _migrate(url, command, revision):
    return await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", command, revision],
        cwd=Path(__file__).parents[2],
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("has_archive", [False, True])
async def test_downgrade_requires_empty_archive(pg_url, pg_sessions, has_archive):
    marker = uuid.uuid4().hex
    async with pg_sessions() as session:
        permission = await session.scalar(
            select(Permission).where(Permission.code == "admin:audit:read_archive")
        )
        role = Role(name=marker, permissions=[permission])
        session.add(role)
        if has_archive:
            row = AuditLog(
                action="test:archive:preserve",
                trace_id=marker,
                created_at=datetime.now(UTC) - timedelta(days=100),
            )
            session.add(row)
        await session.commit()
        if has_archive:
            assert await archive_old(session, 90) == 1
        role_id, permission_id = role.id, permission.id
    async with pg_sessions() as session:
        head = await session.scalar(text("SELECT version_num FROM alembic_version"))
    try:
        result = await _migrate(pg_url, "downgrade", "d52e8a6b1f93")
        if has_archive:
            assert result.returncode != 0
            assert "非空" in result.stdout + result.stderr
            async with pg_sessions() as session:
                archived = await session.get(AuditLogArchive, row.id)
                assert archived.trace_id == marker
                assert (
                    await session.scalar(
                        select(role_permission.c.permission_id).where(
                            role_permission.c.role_id == role_id,
                            role_permission.c.permission_id == permission_id,
                        )
                    )
                    == permission_id
                )
                assert await session.scalar(text("SELECT version_num FROM alembic_version")) == head
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            async with pg_sessions() as session:
                assert (
                    await session.scalar(text("SELECT to_regclass('audit_logs_archive')")) is None
                )
                assert (
                    await session.scalar(text("SELECT version_num FROM alembic_version"))
                    == "d52e8a6b1f93"
                )
    finally:
        restored = await _migrate(pg_url, "upgrade", "head")
        assert restored.returncode == 0, restored.stdout + restored.stderr
        async with pg_sessions() as session:
            await session.execute(delete(AuditLogArchive).where(AuditLogArchive.trace_id == marker))
            await session.execute(delete(AuditLog).where(AuditLog.trace_id == marker))
            await session.execute(delete(Role).where(Role.id == role_id))
            await session.commit()
