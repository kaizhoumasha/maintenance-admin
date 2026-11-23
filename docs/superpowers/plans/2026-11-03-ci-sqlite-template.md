# SQLite empty schema template reuse

> **For agentic workers:** Use superpowers:executing-plans inline in the current feature/ci-parallel-fixtures worktree.

**Goal:** Replace repeated creation of the fixed SQLite Base schema with isolated copies of an empty template, retaining existing tests and behavior.

**Architecture:** A session fixture returns an async initializer that lazily builds and closes an empty fixed-Base template on the first actual SQLite use. It opens the completed template read-only and uses aiosqlite's backup API to populate each fresh engine. Only the immutable path is cached; consumers keep their original database URL, pool, transactions and function lifetime.

**Tech Stack:** Existing pytest/pytest-asyncio, SQLAlchemy and aiosqlite dependencies.

**Spec:** User-approved item 3 from the test audit and explicit instruction to continue in this worktree.

## Global constraints

- Preserve all uncommitted items 1/2, all 5540 existing test IDs, test bodies and parametrizations.
- No production, migration, CI, dependency, password, workload-size, commit or push changes.
- Retain real commits, foreign-key enforcement, savepoint behavior and per-case async resources.
- Templates cover only stable Base.metadata; dynamic independent MetaData and PostgreSQL fixtures remain unchanged.
- Follow the project's rule against permanent fixture-wiring tests: retain temporary isolation/transaction checks in external evidence.

## Review focus

- No case data, extra tables, sequence state, engine or connection can enter the shared template.
- Backup runs before any destination transaction; explicit-BEGIN fixtures retain their event listeners. Source cleanup must drain the native copy before destination pool reset on cancellation.
- Read-only template connections open and close in each caller's event loop; template-building engine is disposed before publication.
- Existing fixture teardown and PostgreSQL behavior remain intact.

## Tasks

- [x] Record a paired pilot baseline for the ordinary client fixture; reproduce missing template initialization with temporary isolation/transaction checks.
- [x] Implement sqlite_tables and migrate client only. Verify constraints, commit visibility, fresh sequences, added-table isolation, failure cleanup and explicit savepoint rollback; retain only if measured faster.
- [x] Apply the proven initializer to the other fixed-Base SQLite fixtures: db, callback_db, asset_factory, ecs, WMS sessions and transport_rig.
- [x] Verify the unchanged complete collection and test bodies; run all CI groups on task-owned stores, SQLite-only checks, Ruff and fresh review.
- [x] Record timings, limits and resource cleanup; leave all changes uncommitted.

## Decisions and evidence

- Initial state preserved in /Users/kaizhou/.codex/tmp/ci-sqlite-template-7p2edks_/initial, including the 12 existing modified/untracked files.
- Ruling: use database backup into the existing destinations rather than changing memory fixtures into disk fixtures; this retains connection and transaction behavior.
- Ruling: do not cache dynamically cloned workflow metadata in this implementation; the fixed Base schema is the bounded measured target.
- Review fix: a real backup progress callback reproduced destination rollback while the native copy was still active. Opening the destination context outside the source context drains the source worker before destination cleanup; the reproduction now passes.
- Ruling: treat eager SQLite setup for PostgreSQL-only selection as a backend-independence requirement. A DDL-forbidden PostgreSQL-only check failed before lazy initialization and now skips normally when PostgreSQL configuration is absent.

## Verification and measured results

The controlled SQLite-only controller sample retained 315 passed / 34 skipped and identical test IDs throughout:

| Stage | pytest elapsed | create_all calls | CREATE statements |
| --- | ---: | ---: | ---: |
| Before item 3 | 28.99s | 335 | 26130 |
| Client pilot | 24.67s | 19 | 1482 |
| Expanded before review fixes | 24.18s | 1 | 78 |
| Final reviewed implementation | 25.79s | 1 | 78 |

Final sample improvement is 11.0%; client setup time is 4.54s -> 0.38s. Use the reviewed implementation's timing rather than the fastest intermediate run.

The final post-review run recreated six dedicated PostgreSQL 17 / Redis 8 stores and executed all three CI groups. Comparison below is with the preceding items-1/2 local run, not a randomized paired hosted-CI benchmark:

| Group | Previous local run | Final item 3 run | Results |
| --- | ---: | ---: | --- |
| Workflows | 350.78s | 329.67s | 964 passed / 6 skipped |
| Remaining | 332.77s | 281.87s | 4273 passed / 41 skipped |
| Migrations | 392.92s | 375.71s | 182 passed / 74 skipped |

- All 5540 ordered collection IDs and outcome counts are retained: 5419 passed / 121 skipped. All 1881 existing normalized test bodies and decorators remain identical.
- Remaining-group measured create_all time: 44.53s -> 2.81s; calls: 2020 -> 505. Remaining calls serve independent dynamic metadata and are deliberately retained.
- Remaining-group CREATE statements: 121974 -> 3804. Callback database setup: 22.46s -> 1.16s; client setup: 8.05s -> 0.49s.
- Existing engine URLs, pool types, fixture lifetimes, explicit-BEGIN listeners, PostgreSQL reset behavior and nested cleanup remain unchanged.
- Seven external behavior checks pass: memory/file commit, schema and sequence isolation; foreign-key setting/enforcement; outer rollback after savepoint release; no repeated table DDL; failed-copy release/retry; cancellation release; native copy completion before pool reset. PostgreSQL-only selection with SQLite DDL forbidden skips normally when unconfigured.
- Independent review's cancellation and backend-independence findings were reproduced and fixed before the final full run. Ruff check/format and diff whitespace checks pass.
- Eight pre-existing dirty files outside the four fixture files remain byte-identical. Earlier items 1/2 remain in the working tree; no commit or push was made.
- All three PostgreSQL stores had zero remaining owned workflow fixture schemas. The six task-owned containers and anonymous volumes were removed.
- Limit: the remaining group is the direct optimization target. Changes in unmodified groups include shared-host load variation; the longest local group remains migrations. Hosted CI waiting-time improvement is not established without publishing and running CI.
- Evidence: /Users/kaizhou/.codex/tmp/ci-sqlite-template-7p2edks_/comparison.json, pilot-comparison.json, stage3-final.diff and associated logs.
