# Migration baseline and retained PostgreSQL table reuse

> **For agentic workers:** Use superpowers:executing-plans inline in the current feature/ci-parallel-fixtures worktree.

**Goal:** Reduce repeated migration preparation and fixed PostgreSQL table checks while retaining all 5540 existing cases and their behavioral assertions.

**Architecture:** One test helper executes real Alembic subprocesses with the caller's environment overrides. A lazy session fixture caches closed SQLite databases at explicit starting revisions and copies them into each case's disposable file. A retained-PostgreSQL-table cache was prototyped and discarded after measurement; only empty PostgreSQL metadata transactions are skipped. Dynamic tables and SQLite metadata retain their existing creation/cleanup behavior.

**Tech Stack:** Existing pytest, SQLAlchemy, Alembic, SQLite and PostgreSQL 17.

**Spec:** User-approved items 4 and 5 from the test audit; earlier items 1-3 are already uncommitted and must be retained.

## Global constraints

- Preserve existing test IDs, parameter combinations, assertions, expected failures and migration command sequences under test.
- Cache only preparatory SQLite starting revisions; execute each tested upgrade/downgrade and guard through the actual CLI. Keep empty-database full-chain cases.
- PostgreSQL migration databases still reset as before; no PostgreSQL database-template cache or migration changes.
- No new PostgreSQL table-cache state is retained; existing module-owned schemas and resets remain unchanged.
- Retain function-owned engines, real commits, schema-local resets, dynamic-table creation/drop and SQLite behavior.
- No production, dependency, CI, main-checkout, commit or push changes.

## Review focus

- Failed or cancelled migration preparation cannot publish a partial cached database; copies cannot mutate cached rows/version/schema.
- Caller-specific RELIABLE_TARGETS overrides, CLI arguments, stdout/stderr and expected nonzero exit codes remain intact.
- All fixed/dynamic PostgreSQL table checks and per-case drops remain unchanged after discarding the prototype.
- Revision files are used only for explicit SQLite preparation; no PostgreSQL files, connections or engines are cached.
- Earlier dirty files outside this scope remain byte-identical.

## Tasks

- [x] Measure representative migration files and workflow files with subprocess/metadata-probe counts on task-owned stores.
- [x] Add the shared Alembic executor, retain local bindings, and implement revision-specific SQLite preparation copies; verify isolation and real migration failure/round-trip behavior.
- [x] Prototype bounded retained-metadata creation, excluding roller_scans' per-case production table; discard the cache after measurement and retain only skipping empty metadata transactions.
- [x] Verify existing collection and assertion coverage, run all three full CI groups, SQLite-only checks, Ruff and an independent review.
- [x] Record results and cleanup; retain all changes uncommitted.

## Decisions and evidence

- Initial state and the 13 earlier dirty files are preserved in /Users/kaizhou/.codex/tmp/ci-migration-pg-v78_d7v9.
- Ruling: retain lightweight local migration bindings where they close over per-case URLs; centralize process execution rather than changing test steps.
- Ruling: only metadata that remains in the owned schema may skip repeated existence checks; the per-case production table remains independently created and dropped.
- Ruling: the PostgreSQL cache prototype reduced representative metadata probes from 178 to 56, but create_all time only changed from 1.477s to 1.229s and total pytest time changed from 82.54s to 83.82s. Discard its extra cache state; manual_bins now simply avoids opening create/drop transactions for its empty PostgreSQL metadata.

## Verification and measurements

Paired pilot runs used identical commands/instrumentation and fresh dedicated stores on identical ports:

| Pilot | Before | After | Results |
| --- | ---: | ---: | --- |
| SMT manual / BIN_Roller persistence migrations | 136.62s | 101.52s | 64 passed / 64 skipped |
| Rack lifecycle / factory profile with PG cache prototype | 82.54s | 83.82s | 126 passed |

- Migration pilot setup time: 50.52s -> 11.16s. A separate real-CLI check observed four preparatory processes for two copies before caching, versus one successful revision build after caching.
- The PG cache prototype is not in the final code. Its original dynamic production-table creation/drop remained enabled during the pilot; CREATE/DROP counts were unchanged. Only empty manual_bins PG transactions are removed in the final implementation.
- Eighteen executor definitions now delegate/alias the shared helper. Local URL bindings, RELIABLE_TARGETS overrides, stdout/stderr and expected nonzero returns are retained.

Final three CI-group run, compared with the preceding items-1/2/3 local run:

| Group | Previous local run | Final run | Results |
| --- | ---: | ---: | --- |
| Migrations | 375.71s | 341.12s | 182 passed / 74 skipped |
| Workflows | 329.67s | 341.38s | 964 passed / 6 skipped |
| Remaining | 281.87s | 289.82s | 4273 passed / 41 skipped |

- Total: 5419 passed / 121 skipped. Every group's ordered collection IDs and outcome counts match the previous run; the union remains 5540 unique cases.
- All 1881 existing test bodies/decorators remain identical after normalizing only the local migrate binding implementation. Actual migration calls and assertions in test bodies are unchanged.
- Full migration setup time: 87.22s -> 39.56s. The final migration group is 9.2% faster than the previous local record; unmodified-group timing includes shared-host variation. These are not hosted-CI results.
- Three external cache contracts pass: same-revision reuse without mutation sharing, failed candidate recovery and separate revision versions. Cancellation/publication and SQLite sidecar behavior were also checked by independent source review.
- With TEST_DATABASE_URL, TEST_REDIS_URL and the separate migration URLs unset, the selected state/persistence suite passes 31 cases and skips 31 configured-PostgreSQL cases in 60.50s; no-initial SQLite head migration remains exercised.
- Independent review found no actionable findings. Ruff check/format and diff whitespace checks pass.
- Twelve of the thirteen previously dirty files remain byte-identical. Only manual_bins' empty-PG-metadata transactions changed in the remaining file; earlier items 1-3 are preserved.
- All task stores had zero remaining owned workflow fixture schemas. Six task-owned containers and anonymous volumes were removed. No commit, push or main-checkout change was made.
- Evidence: /Users/kaizhou/.codex/tmp/ci-migration-pg-v78_d7v9/comparison.json, pilot-comparison.json, current.diff, external contract logs and the discarded pg-cache-prototype directory.
