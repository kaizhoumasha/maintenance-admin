# Task 2 report

## Status

Implemented pending queue indexes and coarse due filtering.

## Changes

- Added partial ORM indexes for AUTO pending, release-result, unconsumed-report, and arrived-but-not-terminal candidate queries.
- Added the coarse pending/due index on `next_check_at` and `id`.
- Added Alembic migration `pending_scan_indexes_v1` after `smt_auto_manual_bypass_v1` with the exact requested index names and downgrade support.
- Updated `CoarseSortingService.scan_pending` to include rows with no due time or a `next_check_at` at or before the service clock. Preparation and event-driven branches remain unchanged.
- Added migration index coverage and a regression test proving a future coarse wait is not advanced.

## TDD evidence

The new tests were run before production changes. The migration assertion failed because the indexes were absent. After correcting the regression test to patch the frozen service class, the unfiltered service failed with `assert 1 == 0`, proving that the future row entered the advance path. The production filter was then added.

## Verification

- `uv run pytest -q tests/migrations/test_pending_scan_indexes.py tests/businesses/test_coarse_sorting_direct_put.py tests/migrations/test_reliable_stalled.py::test_revision_discovery_has_one_head_without_running_migrations`
  - 39 passed, 38 skipped. PostgreSQL cases were skipped because no disposable PostgreSQL URL was configured.
- `uv run pytest -q tests/migrations/test_smt_auto_state.py tests/businesses/test_coarse_sorting_migration.py tests/migrations/test_project_schema.py`
  - 19 passed, 8 skipped.
- Ruff check and format check passed for all changed files.
- `uv run alembic heads` reports one head, `pending_scan_indexes_v1`.

## Concerns

PostgreSQL index creation and PostgreSQL-specific service behavior were not exercised locally because the disposable PostgreSQL test URL was unavailable. The migration uses matching partial predicates for SQLite and PostgreSQL and should be validated in CI.

## Round 1 follow-up

The existing outbound migration regression test compared the inbound-plate schema snapshot byte-for-byte against the pre-index revision. The test now permits and checks the two intentional pending/report indexes while retaining exact comparisons for all other inbound-plate schema fields and prior indexes. The focused SQLite case passes. PostgreSQL remains skipped without a disposable PostgreSQL URL.

## Round 2 follow-up

The coarse application downgrade helper now invokes the pending-scan-index migration downgrade before the legacy coarse migrations, matching the real head-to-previous order and preventing the old SQLite batch rebuild from referencing the removed `next_check_at` column. The focused SQLite case passes. No production code changed.
