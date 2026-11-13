"""Retire the frozen generic history in a separate, explicitly attested window."""

import argparse
import asyncio
import json
import re
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.database import create_database_engine

TABLES = ("integration_reference", "integration_record", "integration_exchange")
WRITERS = (
    "api",
    "sender",
    "worker",
    "beat",
    "background",
    "one_off",
    "manual",
    "database_transactions",
)
CHECKS = ("cleanup", "backfill", "scripts")
PROOFS = ("isolated_acceptance", "integration_acceptance", "running_version", "backup")


class RetirementRefused(RuntimeError):
    pass


def read_evidence(path: Path):
    try:
        with path.open("rb") as source:
            data = source.read(4097)
        if len(data) > 4096:
            raise ValueError
        evidence = json.loads(data, object_pairs_hook=_unique)
    except (ValueError, TypeError, OSError, RecursionError):
        raise RetirementRefused("缺少独立验收、联调验收、运行版本或备份证据") from None
    validate_evidence(evidence)
    return evidence


def _unique(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError
    return result


def validate_evidence(evidence):
    if (
        not isinstance(evidence, dict)
        or set(evidence) != set(WRITERS + CHECKS + PROOFS)
        or any(
            not isinstance(evidence[key], str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", evidence[key])
            for key in PROOFS
        )
    ):
        raise RetirementRefused("缺少独立验收、联调验收、运行版本或备份证据")
    if any(evidence[key] != "DRAINED" for key in WRITERS):
        raise RetirementRefused("未确认所有旧进程和人工写入均已排空")
    if any(evidence[key] != "RETIRED" for key in CHECKS):
        raise RetirementRefused("仍有清理、补录或人工脚本依赖未退出")


async def retire(connection, evidence, *, apply):
    validate_evidence(evidence)
    if connection.dialect.name != "postgresql":
        raise RetirementRefused("删除准入需要 PostgreSQL")
    if await connection.scalar(text("SELECT version_num FROM alembic_version")) != (
        "http_exchange_query_views"
    ):
        raise RetirementRefused("查询视图迁移尚未完成")
    names = ", ".join(f"enterprise_backend.{name}" for name in TABLES)
    await connection.exec_driver_sql(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE NOWAIT")
    if await connection.scalar(
        text("""
        SELECT EXISTS (
            SELECT 1 FROM pg_stat_activity
            WHERE datname=current_database() AND pid<>pg_backend_pid()
              AND backend_type='client backend' AND (xact_start IS NOT NULL OR state IS NULL)
        ) OR EXISTS (SELECT 1 FROM pg_prepared_xacts WHERE database=current_database())
    """)
    ):
        raise RetirementRefused("仍有其他数据库事务，或当前角色无法核查连接状态")
    fences = await connection.scalar(
        text("""
        SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='enterprise_backend' AND t.tgenabled IN ('O','A')
          AND c.relname IN ('integration_exchange','integration_record','integration_reference')
          AND t.tgname=c.relname||'_runtime_fence' AND t.tgtype=62
          AND t.tgfoid=to_regprocedure('enterprise_backend.block_retired_integration_write()')
    """)
    )
    if fences != 3:
        raise RetirementRefused("旧表写入阻断未完整生效")

    dependencies = (
        await connection.execute(
            text("""
        WITH old AS (
            SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='enterprise_backend' AND c.relname IN
                ('integration_exchange','integration_record','integration_reference')
        )
        SELECT 'view', (r.ev_class::regclass)::text FROM pg_depend d
        JOIN pg_rewrite r ON d.classid='pg_rewrite'::regclass AND d.objid=r.oid
        WHERE d.refclassid='pg_class'::regclass AND d.refobjid IN (SELECT oid FROM old)
          AND r.ev_class NOT IN (SELECT oid FROM old)
        UNION
        SELECT 'foreign_key', (c.conrelid::regclass)::text FROM pg_constraint c
        WHERE c.contype='f' AND c.confrelid IN (SELECT oid FROM old)
          AND c.conrelid NOT IN (SELECT oid FROM old)
        UNION
        SELECT 'routine', (p.oid::regprocedure)::text FROM pg_depend d
        JOIN pg_proc p ON d.classid='pg_proc'::regclass AND d.objid=p.oid
        WHERE d.refclassid='pg_class'::regclass AND d.refobjid IN (SELECT oid FROM old)
        ORDER BY 1,2
    """)
        )
    ).all()
    if dependencies:
        raise RetirementRefused(
            "仍有旧表依赖 " + ", ".join(f"{kind} {name}" for kind, name in dependencies)
        )
    for name in ("integration_exchange_summary", "integration_record_summary"):
        await connection.exec_driver_sql(f"SELECT 1 FROM enterprise_backend.{name} LIMIT 0")

    async def drop_legacy():
        await connection.exec_driver_sql(f"DROP TABLE {names} RESTRICT")
        await connection.exec_driver_sql(
            "DROP FUNCTION enterprise_backend.block_retired_integration_write() RESTRICT"
        )

    if not apply:
        savepoint = await connection.begin_nested()
        try:
            await drop_legacy()
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "2BP01":
                raise RetirementRefused("仍有旧表或围栏函数依赖") from None
            raise
        finally:
            await savepoint.rollback()
        return
    await drop_legacy()
    await connection.exec_driver_sql("""
        CREATE TABLE enterprise_backend.integration_history_retirement (
            retired_at timestamptz NOT NULL DEFAULT now(), evidence jsonb NOT NULL
        )
    """)
    await connection.execute(
        text("""
        INSERT INTO enterprise_backend.integration_history_retirement(evidence)
        VALUES (CAST(:evidence AS jsonb))
    """),
        {"evidence": json.dumps(evidence)},
    )


async def _run(args):
    evidence = read_evidence(args.evidence)
    settings = get_settings()
    if not settings.INTEGRATION_RUNTIME_AUTHORITY:
        raise RetirementRefused("独立运行证据尚未启用")
    engine = create_database_engine(settings.DATABASE_URL, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await retire(connection, evidence, apply=args.apply)
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    from sqlalchemy.exc import SQLAlchemyError

    try:
        asyncio.run(_run(args))
    except RetirementRefused as error:
        parser.exit(1, f"{error}\n")
    except SQLAlchemyError:
        parser.exit(1, "数据库锁或依赖检查未通过，删除未提交\n")
    print("旧三表已删除" if args.apply else "准入检查通过，数据库未删除任何表")


if __name__ == "__main__":
    main()
