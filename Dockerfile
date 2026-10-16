FROM python:3.13-slim AS build
RUN pip install --no-cache-dir uv==0.12.13
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

FROM python:3.13-slim
RUN useradd --system --uid 10001 app
WORKDIR /app
COPY --from=build /app/.venv .venv
COPY app app
COPY migrations migrations
COPY scripts scripts
COPY alembic.ini .
RUN chmod -R a+rX app migrations scripts alembic.ini
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
