# CI parallel tests and database fixture reuse

> **For agentic workers:** Use superpowers:executing-plans to implement and verify this plan in the current isolated worktree.

**Goal:** Reduce CI waiting time and repeated PostgreSQL test DDL while retaining every test and real transaction behavior.

**Architecture:** Three independent matrix jobs partition migrations, BIN_Roller/SMT manual, and the remaining tests. A stable `test` job succeeds only when every matrix job succeeds. Module-owned PostgreSQL schemas retain their tables between cases; function-owned engines preserve event-loop and transaction isolation, and an explicit schema-local reset clears committed rows and sequences before reuse.

**Tech Stack:** GitHub Actions, pytest/pytest-asyncio, SQLAlchemy, PostgreSQL 17.

**Spec:** Approved approach in this chat: three CI groups, measure fixture cost, pilot BIN_Roller then SMT manual, retain only measured improvements. SQLite templates and migration-baseline caching are later independent work.

## Global constraints

- No production code, dependencies, migrations, isolation levels, or business contracts change.
- Keep PR/manual triggers and cancellation of superseded PR runs.
- The existing required GitHub Actions check remains `test`; failure, cancellation, and skipped matrix results must not pass it.
- Every collected test belongs to exactly one group; the original baseline contains 5537 cases.
- Reuse only owned disposable PostgreSQL schemas and independent test MetaData, never production tables or migration state.
- Preserve real commits, multiple connections, fresh function-level engines, and SQLite behavior.
- No commit, push, PR creation, or merge is included in this implementation request.

## Review focus

- Matrix groups must be exhaustive and disjoint, including newly added tests.
- A failed or cancelled matrix must fail the stable merge gate.
- Committed rows and sequence state must not leak between cases; unrelated schemas must remain intact.
- Async engines must never cross event loops, and schema teardown must run after failures.
- Dynamic fixture tables, deferred constraints, and concurrency/recovery tests must retain their behavior.

## Tasks and validation

- [x] Measure representative BIN_Roller and SMT manual files before edits: wall time, setup/call/teardown, and DDL timing/counts. Use task-owned PostgreSQL/Redis containers.
- [x] Implement the three groups in `.github/workflows/ci.yml`, retaining a stable `test` aggregate. Verify with Actionlint, collection union/disjoint checks, and pass/fail/cancel/skip aggregate checks.
- [x] Add a module-owned PostgreSQL schema fixture in `tests/conftest.py`. Pin committed-row cleanup, sequence reset, multi-connection commits, dynamic tables, and unrelated-schema preservation through the fixture's public context API.
- [x] Apply reuse first to `tests/businesses/bin_roller/conftest.py`. Re-run the same representative files; retain only if measured setup/teardown cost improves.
- [x] Apply the proven fixture to `tests/businesses/smt_manual/conftest.py`, preserving native and cloned table declarations. Re-run the same file and concurrency/recovery suites.
- [x] Run all three groups against separate disposable stores, Ruff, Actionlint, and reordered/repeated representative tests. Record before/after evidence and test counts.
- [x] Review the complete diff and fix material findings. Leave changes uncommitted for review.

## Evidence

Baseline CI: run 41707609224, 5416 passed / 121 skipped, pytest 2138.99 seconds. Log-derived group times: migrations 754.77s; BIN_Roller + SMT manual 783.63s; remaining tests 594.58s. Matrix execution time remains an estimate until an authorized CI run.


Implementation checkpoint:
- Actionlint 1.7.12 and Ruff check/format pass. Actual collection verifies 5540 cases = 256 migrations + 970 workflows + 4314 remaining, with no duplicates or omissions.
- Aggregate shell check accepts success and rejects failure/cancelled/skipped/empty results.
- New schema reset contract tests: first observed 3 failures without reset, then 3 passed after schema-local TRUNCATE RESTART IDENTITY. They cover committed visibility, added tables, sequence reset, failed cases, and unrelated schemas.
- Instrumented BIN_Roller representative file: 100 passed; phase total 47.78s -> 40.32s; setup+teardown 16.69s -> 10.03s.
- Instrumented SMT manual representative file: 26 passed; phase total 57.81s -> 49.29s; CREATE statements 1664 -> 64; DROP statements 676 -> 1.
- Ruling: reuse schemas per module, not per session, and build a fresh engine for each use. This bounds cross-module schema variants and preserves function event-loop lifetimes, at the cost of repeating DDL once per module.
- Three full groups are running against independent disposable stores; no production resources are used.


Full-group verification, first pass:
- workflows: 964 passed / 6 skipped, 390.33s.
- migrations: 182 passed / 74 skipped, 420.98s.
- remaining: 4273 passed / 41 skipped, 372.24s.
- Total: 5419 passed / 121 skipped. Separate PostgreSQL instances and separate Redis instances for the groups that use Redis.
- SQLite-only check with TEST_DATABASE_URL/TEST_REDIS_URL unset: 2 passed.
- Fresh read-only review found one cleanup issue: a failed schema drop prevented later independent drops. Ruling: treat this as an ownership-cleanup correctness issue rather than polish; fix it in this scope. A real-PostgreSQL one-off fault injection reproduced the leaked second schema, then passed after AsyncExitStack scheduled every drop while preserving the original error.
- Final verification repeats all groups after that fix; the workflow group is reversed to expose order dependencies. Hosted CI timing remains unverified because publishing has not been requested.


Final verification after the cleanup fix:
- Reversed workflow group: 964 passed / 6 skipped, 383.07s.
- Migrations: 182 passed / 74 skipped, 407.41s.
- Remaining: 4273 passed / 41 skipped, 358.82s.
- Total: 5419 passed / 121 skipped, all 5540 collected cases accounted for.
- Ruff check/format, Actionlint, diff whitespace check, group coverage checks, and success-only aggregate checks passed.
- Direct PostgreSQL inspection found zero remaining owned fixture schemas in all three disposable stores.
- Independent review completed; its cleanup finding was reproduced and fixed. No remaining review findings.
- SQLite templates and migration-baseline reuse remain outside this first implementation, as approved. No files outside the CI/test/plan scope changed; nothing was committed or published.
