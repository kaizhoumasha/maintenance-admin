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
- `app/http_client/`: standalone HTTP client; no `app.core`, business, database, Redis, Celery, or FastAPI imports. single-request scope only (no retries, rate limiting, or application assembly).
- `app/reliable/`: protocol-agnostic outbound outbox, leases, retries and TTL; uses only injected adapter ports. No business/protocol implementations, FastAPI, Celery or result-consumption API. Assembly lives in `app/reliable_runtime.py`; Celery only cleans terminal rows. Never stack another retry scheduler around these messages.
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
