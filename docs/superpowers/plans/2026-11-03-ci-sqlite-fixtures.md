# SQLite fixture and callback client optimization

> **For agentic workers:** Use superpowers:executing-plans in the existing feature/ci-parallel-fixtures worktree.

**Goal:** Reduce repeated test database setup without changing existing cases or behavior coverage.

**Architecture:** Skip existence probes only when creating a known-empty SQLite database. Dispose outermost owned databases without redundant final DDL. Share HTTP client assembly and explicitly use the existing callback factory for public callback tests.

**Tech Stack:** pytest, pytest-asyncio, SQLAlchemy, HTTPX, SQLite, PostgreSQL 17, Redis 8.

**Spec:** User-approved items 1 and 2 from this chat's test audit, with execution explicitly requested in this worktree.

## Global constraints

- Preserve every existing test name, parameter combination, assertion and fixture scope; baseline collection is 5540 cases.
- Do not change production code, migrations, workflow configuration, password costs or stress-test sizes.
- Keep authenticated callback tests' independent authorization storage; never run seed_admin on the shared PostgreSQL callback database.
- Keep function-owned engines, real commits, PostgreSQL isolation and nested fixture cleanup.
- Do not commit, push or modify the main checkout.

## Review focus

- Only known-empty databases may use checkfirst=False; shared or incrementally extended metadata must keep probes.
- Removing outermost SQLite drops must not remove nested cleanup or foreign-key ordering.
- Public HTTP callbacks must still use real receiver transactions, snapshots and the fake Redis guard.
- Client teardown must restore dependency overrides and the audit factory even after failures.
- Collection IDs and normalized test bodies must remain identical after fixture argument renaming.

## Tasks

- [x] Record the three CI groups' baseline results, timings, collection IDs and DDL counts using task-owned disposable stores.
- [x] Set checkfirst=False on fresh SQLite fixture creation only; remove final SQLite drops immediately before disposal in roller_store/manual_sessions.
- [x] Extract shared HTTP assembly in tests/conftest.py; add callback_client(callback_db, redis); migrate only public callback consumers in five test modules.
- [x] Compare collection IDs and normalized test bodies with baseline; run the same three full CI groups and compare actual timings.
- [x] Verify SQLite-only operation, Ruff and cleanup; obtain a fresh review of the final diff and record evidence.

## Decisions and evidence

- Existing current worktree is clean and matches develop's test tree. No new checkout is needed.
- This is a test infrastructure refactor. Existing behavioral tests are the before/after contract; no permanent fixture-wiring tests will be added.
- Ruling: 44 authenticated callback cases retain both stores because merging authorization into shared PostgreSQL would change isolation and seed behavior. The remaining 306 public callback cases can share their callback store.
- Ruling: retain timing logs and review material outside tracked source; leave changes uncommitted as requested.

## Verification and timing evidence

One paired local run of the same three concurrent CI groups, each with its own PostgreSQL 17 and Redis 8. Recreated all six disposable stores on identical ports before the second run. Both runs used identical measurement hooks; times below are pytest's reported elapsed seconds. These are local measurements, not hosted CI results.

| Group | Before | After | Change | Results in both runs |
| --- | ---: | ---: | ---: | --- |
| Workflows | 347.78s | 350.78s | +0.86% | 964 passed / 6 skipped |
| Remaining | 349.73s | 332.77s | -4.85% | 4273 passed / 41 skipped |
| Migrations | 389.27s | 392.92s | +0.94% | 182 passed / 74 skipped |

- Every group retained identical ordered collection IDs and outcome counts: 5419 passed / 121 skipped, 5540 unique cases in total.
- All 1881 behavioral test functions retain identical normalized AST bodies and decorators after reversing the local client argument rename.
- Remaining-group create_all calls: 2326 -> 2020; ordinary client setups: 679 -> 373; public callback_client setups: 306, with no additional database creation.
- Remaining-group CREATE statements: 145842 -> 121974; PRAGMA statements: 127061 -> 3165; measured create_all time: 66.54s -> 44.53s.
- Workflow-group DROP statements: 5358 -> 2234; PRAGMA statements: 16426 -> 6886. Nested cleanup and PostgreSQL resets remain unchanged.
- SQLite-only verification with TEST_DATABASE_URL/TEST_REDIS_URL unset: 99 passed / 83 skipped across WMS HTTP callbacks, entry transactions and workline HTTP tests.
- Forced-exception check verified HTTP context restoration of existing dependency overrides and audit factory; no permanent fixture-wiring test was added.
- Ruff check, Ruff format check and diff whitespace check pass. Independent read-only review found no issues.
- PostgreSQL inspection found zero remaining owned workflow fixture schemas in all three stores; the six task-owned containers and anonymous volumes were removed.
- Interpretation: repeated setup work is reduced, with a measured improvement in the remaining group. Workflow runtime is approximately unchanged. The migration group remains the local parallel bottleneck, so this run does not establish a reduction in overall parallel waiting time.
- Raw logs, collection IDs and comparison.json are retained in /Users/kaizhou/.codex/tmp/ci-sqlite-fixtures-f4qa7szx. No production, migration, CI configuration, commit or push changes are included.
