# DEVELOP 窄范围性能优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在不改变现有业务推进和唤醒语义的前提下，降低 pending 扫描、粗分等待重试和深页管理查询的数据库开销。

**Architecture:** 保留现有扫描顺序和推进语义，把 SMT 扫描改为短事务按有界批次物化本轮固定候选 ID 到临时文件，再在事务外按文件批次推进；粗分查询在候选阶段过滤未来检查时间；为现有队列条件补部分索引；旧式 OFFSET 分页增加固定最大偏移保护。任务去重、审计异步化和连接池调整不在本计划内。

**Tech Stack:** Python 3.13, FastAPI, Pydantic, SQLAlchemy async, Alembic, pytest/pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-11-18-develop-narrow-performance-optimization-design.md`

## Global Constraints

- 保持单 Alembic head；新迁移必须挂在 `smt_auto_manual_bypass_v1` 之后。
- 不改变业务推进顺序、原始外部身份、回调唤醒和重试语义。
- 使用现有模型索引声明与 Alembic 迁移双写，禁止只改其中一侧。
- `MAX_OFFSET` 固定为 `100_000`；超出仍返回 `INVALID_REQUEST`。
- 每项实现先写测试并确认失败，再写最小生产代码。

## Review Focus

- `page * page_size` 恰好等于最大偏移时仍接受，超过一行立即拒绝；验证现有 64 位边界仍有效。
- 粗分 `next_check_at` 为空的记录继续扫描，未来时间记录不进入推进事务，到期后重新进入。
- 本轮候选 ID 集合固定在扫描开始时，按有界 keyset 批次落到临时文件；扫描期间新增的 ID 不会被本轮处理，即使其字典序早于既有记录，worker 内存只保留当前批次。
- 粗分未来等待记录在出现更高代次的货架准备时仍会进入扫描，保持原有即时恢复语义。
- SMT 扫描取得固定候选 ID 后，读取事务已结束，实际 `advance` 仍按原顺序和原异常处理执行。
- 迁移在 SQLite 和 PostgreSQL 测试路径都能创建、回滚并保留唯一 head。

### Task 1: 深页 OFFSET 保护

**Files:**
- Modify: `app/schemas/base.py:11-25`
- Test: `tests/controllers/test_pagination.py`

**Interfaces:**
- Produces: `PageParams` rejects offsets greater than `100_000` with the existing Pydantic-to-`INVALID_REQUEST` path.

- [ ] **Step 1: Write the failing test**

  Add a boundary case asserting `page=1001,page_size=100` is accepted and `page=1002,page_size=100` is rejected, while retaining the existing 64-bit overflow cases.

- [ ] **Step 2: Run the pagination test to verify it fails**

  Run: `uv run pytest -q tests/controllers/test_pagination.py::test_pagination_offset_bounds`

  Expected: the new over-limit case returns 200 before the implementation.

- [ ] **Step 3: Implement the minimal guard**

  Add a module constant `MAX_OFFSET = 100_000` and make `PageParams.check_offset` reject offsets above it before the existing 64-bit check.

- [ ] **Step 4: Run the pagination tests to verify they pass**

  Run: `uv run pytest -q tests/controllers/test_pagination.py tests/services/test_pagination.py`

  Expected: all tests pass, including existing shallow pagination and cursor tests.

### Task 2: Pending queue indexes and coarse due filtering

**Files:**
- Modify: `app/businesses/smt_auto/models.py:177-277,340-400`
- Modify: `app/businesses/coarse_sorting/models.py:45-104`
- Modify: `app/businesses/coarse_sorting/service.py:2091-2150`
- Create: `migrations/versions/2026-11-18_pending_scan_indexes.py`
- Test: `tests/migrations/test_pending_scan_indexes.py`
- Test: `tests/businesses/test_coarse_sorting_direct_put.py`

**Interfaces:**
- Produces: partial indexes for AUTO pending/report/terminal candidate queries and a coarse pending/due index; `CoarseSortingService.scan_pending` only sends due jobs to `advance`.

- [ ] **Step 1: Write the failing migration and due-filter tests**

  Add a migration test that upgrades to head and asserts these indexes exist: `smt_auto_bin_work_pending_idx`, `smt_auto_bin_work_release_idx`, `smt_auto_inbound_plate_pending_idx`, `smt_auto_inbound_plate_report_idx`, `smt_auto_outbound_plate_pending_idx`, `smt_auto_outbound_plate_report_idx`, `smt_auto_outbound_source_member_terminal_idx`, and `coarse_sorting_job_pending_due_idx`. Add a coarse service test with a future `next_check_at` row and assert `scan_pending` does not call its advance path.

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `uv run pytest -q tests/migrations/test_pending_scan_indexes.py tests/businesses/test_coarse_sorting_direct_put.py`

  Expected: index assertions fail and the future-due row is still visited.

- [ ] **Step 3: Implement the minimal model, migration, and query changes**

  Add matching partial `Index` declarations and a migration whose `down_revision` is `smt_auto_manual_bypass_v1`. In `scan_pending`, add the current clock value to the coarse job predicate and require `next_check_at IS NULL OR next_check_at <= now`; leave preparation and event-driven branches unchanged.

- [ ] **Step 4: Run migration and coarse tests to verify they pass**

  Run: `uv run pytest -q tests/migrations/test_pending_scan_indexes.py tests/businesses/test_coarse_sorting_direct_put.py tests/migrations/test_reliable_stalled.py::test_revision_discovery_has_one_head_without_running_migrations`

  Expected: all pass and exactly one migration head is reported.

### Task 3: Short-lived SMT pending snapshots

**Files:**
- Modify: `app/businesses/smt_auto/service.py:183-278`
- Modify: `app/businesses/smt_manual/service.py:109-162`
- Test: `tests/businesses/smt_auto/test_outbound.py`
- Test: `tests/businesses/smt_manual/test_pending.py`

**Interfaces:**
- Produces: `scan_pending` keeps the existing ordering, return count, and per-item advancement contract, but the read-only candidate transaction is closed before the first `advance` call.

- [ ] **Step 1: Write the failing transaction-lifetime tests**

  Wrap the session factory with a tracker and assert the scan's candidate-snapshot transaction has ended before the first monkeypatched `advance` call. Keep an assertion that a record inserted after candidate capture is not visited in the current scan, including an arbitrary string ID that sorts before the captured records.

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `uv run pytest -q tests/businesses/smt_manual/test_pending.py tests/businesses/smt_auto/test_outbound.py`

  Expected: the transaction-lifetime or arbitrary-string-ID boundary assertion fails because the current snapshot remains open during advancement or the old upper-bound logic admits a newly inserted ID.

- [ ] **Step 3: Implement the minimal transaction split**

  Move candidate-ID queries into a short `async with session_factory()` transaction, write immutable ordered keyset batches to bounded temporary storage, close it, then run the existing cursor loops by reading those batches. Do not change `advance`, retry handling, logging, or ordering.

- [ ] **Step 4: Run the business pending tests to verify they pass**

  Run: `uv run pytest -q tests/businesses/smt_manual/test_pending.py tests/businesses/smt_auto/test_outbound.py tests/businesses/smt_manual/test_two_worklines.py`

  Expected: transaction-lifetime, insertion-boundary, repeated-scan, and concurrency tests pass.

### Task 4: Whole-branch verification

**Files:**
- No additional production files.

- [ ] **Step 1: Run focused regression tests**

  Run the three task-level commands above and confirm zero failures.

- [ ] **Step 2: Run static checks**

  Run: `uv run ruff check . && uv run ruff format --check .`

- [ ] **Step 3: Run the full suite**

  Run: `uv run pytest -q`

  Expected: the full suite completes successfully; if local integration services are absent, only the repository's configured skips remain.

- [ ] **Step 4: Inspect the final diff and migration head**

  Run: `git diff --check`, `git status --short`, and `uv run alembic heads`.

  Expected: only planned files changed, no whitespace errors, and one Alembic head remains.
