# Repository Guidelines

## Working Principles

- **Think before coding:** state assumptions and tradeoffs. Clarify ambiguous interpretations or blocking uncertainty before implementation. Propose simpler approaches.
- **Simplicity first:** implement requested behavior only. Reuse existing patterns; avoid speculative features, single-use abstractions, and impossible-case handling.
- **Surgical changes:** every changed line must serve the request. Match surrounding style; avoid unrelated refactoring/formatting. Remove only dead code your changes create; report pre-existing issues separately.
- **Verify outcomes:** define success criteria. For multi-step work, pair each step with a check. Reproduce bugs before fixing; verify refactors before/after. Report checks and limitations accurately. Use judgment for trivial tasks.

## Project Structure & Module Organization

`enterprise-backend` is a Python 3.13+ FastAPI enterprise backend template for WMS, MES, WES, and other manufacturing applications, with shared administration and third-party/internal API integration capabilities. Keep domain business rules in the consuming projects and distinguish implemented capabilities from planned ones.

- `app/controllers/` → `app/services/` → `app/models/`: routes, business rules/transactions, SQLAlchemy models.
- `app/schemas/`: Pydantic contracts; `core/`, `middlewares/`, `tasks/` under `app/`: infrastructure, middleware, Celery jobs.
- `app/diagnostics.py`: configuration-free safe error fields, origin and stack generation for HTTP, reliable maintenance and Celery. Use `log_exception` with a fixed message and allowlisted identifiers; never log raw exceptions, SQL, payloads or credentials. Each task owns its suppression/recovery state; diagnostics never change retry, transaction or delivery decisions. See `docs/logging.md`.
- `app/http_client/`: standalone HTTP client; no `app.core`, business, database, Redis, Celery, or FastAPI imports. single-request scope only (no retries, rate limiting, or application assembly).
- `app/reliable/`: protocol-agnostic outgoing-message outbox, leases, retries and TTL; uses only injected adapter ports. No business/protocol implementations, FastAPI, Celery or result-consumption API. Assembly lives in `app/reliable_runtime.py`; Celery only cleans terminal rows. Never stack another retry scheduler around these messages.
- `app/integrations/<business>/dev.py` + `app/core/dev_scenarios.py` + `app/controllers/dev*.py`: local-only (`ENVIRONMENT=local`) dev console (task submission) and built-in mock WMS; register scenarios with `@dev_scenario`. See `docs/superpowers/specs/2026-10-22-transport-dev-console-design.md`.
- `app/controllers/ecs_callback.py` + `app/integrations/ecs_callback/`: generic ECS event receiver (`POST /api/v1/callback/event`: strict envelope, content-derived identity, durable receipts and ACK). Add a confirmed event with `@ecs_event` in `app/integrations/<business>/ecs_event.py` or an `ecs_event/` package (one lowercase event-type filename per handler); `dev_ecs_event` is local-only. Handlers write only the current transaction; external intent goes through the outbox. No concrete event handlers or unknown-event replay yet. See `app/integrations/ecs_callback/__init__.py` and `docs/superpowers/specs/2026-10-24-ecs-event-callback-design.md`.
- `app/controllers/wms_events.py` + `app/integrations/wms_callback/`: generic receiver for peer callbacks (`POST /api/v1/wms/events`: envelope, idempotent receipts, snapshots). Add an operation with `@callback_operation(name, data=Payload)` on `async def handler(ctx, payload)` in `app/integrations/<business>/callback.py` or a `callback/` package with one file per operation, named after the operation (`a.b@v1` → `a_b_v1.py`, checked at registration) (auto-discovered; `dev_callback.py` only when local); see `wms_callback/dev_callback.py` and `docs/integrations/registration.md`. Do not name generic mechanisms with warehouse terms such as `inbound`.
- `app/integrations/wms_request/`: WES → WMS `decisions` / `facts` request endpoints (protocols `wms-facts`, `wms-decisions`), endpoint-level envelope/ACK classification and an operation registry with no concrete operations. Add one with `@request_operation(name, Endpoint)` on a `RequestPayload` model in `app/integrations/<business>/request.py` or a `request/` package with one file per operation, named the same way (auto-discovered) and send with `WmsRequests.send` inside the caller's transaction; confirm each operation against the WMS contract first. Both endpoints use the reliable outbox; local debugging (operation catalog, trigger, status, mock WMS) lives in the dev console (`/api/v1/dev/wms/requests`, demo operations in `wms_request/dev.py`); calling code cannot yet read the WMS response (same gap as Transport result consumption).
- `app/integrations/ecs_command/` + `app/controllers/ecs_result.py`: ECS P1 commands and results, with explicit `EcsCommands`/route/`DeviceAnnex`/lifecycle/client assembly (no production route or annex defaults). Create within the caller transaction; freeze identity, bytes, origin and deadline. Snapshot HOLD/HELD never auto-replays uncertainty; results and settlement share a transaction and long-lived evidence. STATIC/WMS behavior remains unchanged. See `docs/integrations/ecs-command.md`; real sending requires confirmed supplier contracts and route authorization.
- All three integration registries share `registration_filename(name)` (`lower`, replace `.`/`@` with `_`, append `.py`), import-time repair hints and source metadata. Local authenticated catalog: `GET /api/v1/dev/integrations/registry`. See `docs/integrations/registration.md`.
- `tests/`: mirrors application areas; `scripts/`: development/bootstrap tools.
- `migrations/versions/`: Alembic migrations (`YYYY-MM-DD_slug.py`).

Services own business logic. Register routers in `app/main.py`; export models from `app/models/__init__.py`.

## Build, Test, and Development Commands

- `uv sync`: install locked dependencies.
- `cp .env.example .env`: initialize configuration; do not overwrite existing settings.
- `docker compose up -d`: start PostgreSQL (25432) and Redis (26379).
- `uv run alembic upgrade head`: apply migrations.
- `uv run uvicorn app.main:app --reload`: start the API.
- `uv run celery -A app.tasks.celery_app worker -l info`: start workers.
- `uv run celery -A app.tasks.celery_app beat -l info`: start the scheduler (run exactly one).
- `uv run pytest`: run tests.
- `scripts/lint.sh`: fix/format with Ruff; review its diff. Check without edits using `uv run ruff check .` and `uv run ruff format --check .`.

## Coding Style & Naming Conventions

Use four spaces, type hints, `snake_case` functions/modules, and `PascalCase` classes. Ruff uses a 100-character line limit. Example: `services/user_service.py`. Raise `AppError(ErrorCode.X, ...)`; use shared error handlers.

## Testing Guidelines

Use pytest, pytest-asyncio, HTTPX; name tests `test_*.py` / `test_<behavior>`. Reuse `client`, `redis`, `admin` fixtures. Unit tests use SQLite/fake Redis; Celery runs eagerly. Integration tests require explicit `TEST_DATABASE_URL`/`TEST_REDIS_URL` for disposable stores; unset configuration skips tests, configured connection failures fail. PostgreSQL applies migrations. No coverage threshold. Cover changes and failures. Tests pin long-lived behavior contracts, not the change that introduced them: assert observable behavior through public entry points (HTTP, service functions), not private helpers or exact internal shapes. Prefer rules over snapshots (e.g. "every route with a permission documents 403", not a hard-coded response set per route). Keep regression tests for fixed bugs; do not keep one-off verification of a refactor, transitional states (e.g. "old parameter now rejected"), or fixture wiring smoke tests. Shared helpers live in `tests/helpers.py` or `conftest.py`; test files never import from each other. Name tests and comments by behavior, not by SPEC number.

## Commit & Pull Request Guidelines

Follow history: `feat:`, `fix:`, `test:`, `chore:`, concise Chinese descriptions, optional `(SPEC-015)` references. Keep commits focused. PRs should explain behavior changes, link issues/specifications, list validation, and identify migrations/configuration changes.

## Branching (Git Flow)

Baseline is `main` at tag `v0.1.0`; work follows Git Flow from there.

- `main`: released code only. Every commit is a release or hotfix merge, tagged `vX.Y.Z` (annotated). Never commit directly.
- `develop`: integration branch, always green (CI runs ruff and pytest on every push and PR). All new work branches from it.
- `feature/<slug>` (from `develop`, e.g. `feature/spec-017-dictionary`): merge back with `--no-ff` or a PR into `develop`, then delete the branch.
- `release/X.Y.Z` (from `develop`): only the version bump (`pyproject.toml` and `version=` in `app/main.py`), upgrade notes, and fixes. Merge into `main` with `--no-ff`, tag `vX.Y.Z` on `main`, merge back into `develop`, delete the branch.
- `hotfix/X.Y.Z` (from `main`): urgent production fix. Merge into `main` (tag) and `develop`.
- One Alembic head at all times: when two branches each add a migration, the later one to merge rebases its `down_revision` onto the new head.
- Agents work on `feature/*` branches created from `develop`, never directly on `main` or `develop`, and commit or push only when asked.

## Security & Configuration

Never commit `.env` or secrets; replace the example JWT secret. Review autogenerated migrations. Permission codes use `<module>:<resource>:<action>`; update `INITIAL_PERMISSIONS` and permission tests together.
