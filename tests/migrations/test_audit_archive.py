from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, text

from app.core.identifiers import uuid7
from app.models import AuditLog, AuditLogArchive, Permission, Role, role_permission
from app.services.audit_service import archive_old
from tests.helpers import run_alembic as _migrate


@pytest.mark.parametrize("has_archive", [False, True])
async def test_downgrade_requires_empty_archive(schema_database, has_archive):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database import create_database_engine

    pg_url, _ = schema_database
    initialized = await _migrate(pg_url, "upgrade", "e7a41c9b3d20")
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    engine = create_database_engine(pg_url)
    pg_sessions = async_sessionmaker(engine, expire_on_commit=False)
    marker = uuid7().hex
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
        restored = await _migrate(pg_url, "upgrade", "e7a41c9b3d20")
        assert restored.returncode == 0, restored.stdout + restored.stderr
        async with pg_sessions() as session:
            await session.execute(delete(AuditLogArchive).where(AuditLogArchive.trace_id == marker))
            await session.execute(delete(AuditLog).where(AuditLog.trace_id == marker))
            await session.execute(delete(Role).where(Role.id == role_id))
            await session.commit()
        await engine.dispose()
