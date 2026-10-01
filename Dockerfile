# syntax=docker/dockerfile:1

# Stage uvtool: pinned Python + pinned uv. Also used on its own to write and check uv.lock.
FROM python:3.14.7-slim-trixie AS uvtool
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /uvx /bin/
ENV UV_PYTHON_DOWNLOADS=0
WORKDIR /app

# Stage base: runtime dependencies from the lock (fails if uv.lock is stale), app code, static files.
FROM uvtool AS base
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev
COPY . /app
# APP_BUILD=1 is build mode: settings skip the fail-closed secret checks so collectstatic runs without secrets
RUN APP_BUILD=1 python manage.py collectstatic --noinput \
 && python -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/Kyiv')"
# Fixed non-root user
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app

# Stage dev: adds the dev dependency group and the device clients the INV-24 verbatim test runs.
FROM base AS dev
RUN apt-get update && apt-get install -y --no-install-recommends curl wget busybox \
 && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked
# /app is root-owned, so coverage cannot write /app/.coverage as uid 10001
ENV COVERAGE_FILE=/tmp/.coverage
# Keep comments on their own line: an inline comment after USER breaks the build (STACK)
USER 10001

# Stage runtime: production image, no dev tools.
FROM base AS runtime
USER 10001
