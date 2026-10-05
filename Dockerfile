# syntax=docker/dockerfile:1

# Global build arg, before the first FROM: BuildKit sets it to the target platform's arch, so
# only the matching tailwind-<arch> stage is built.
ARG TARGETARCH

# Stage uvtool: pinned Python + pinned uv. Also used on its own to write and check uv.lock.
FROM python:3.14.7-slim-trixie AS uvtool
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /uvx /bin/
ENV UV_PYTHON_DOWNLOADS=0
WORKDIR /app

# Stage tailwind-amd64: the pinned Tailwind CSS v4.3.3 standalone CLI for linux/amd64. The
# sha256 is the approved vendor manifest's Dockerfile#tailwind-amd64 entry; ADD fails on any
# other bytes (INV-26, D6-07).
FROM uvtool AS tailwind-amd64
ADD --chmod=755 --checksum=sha256:dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a \
    https://github.com/tailwindlabs/tailwindcss/releases/download/v4.3.3/tailwindcss-linux-x64 /usr/local/bin/tailwindcss

# Stage tailwind-arm64: the same CLI for linux/arm64 (manifest entry Dockerfile#tailwind-arm64).
FROM uvtool AS tailwind-arm64
ADD --chmod=755 --checksum=sha256:55fd0b241214eff3de1e8ee4f22796662f2d2e7a49bcfca7477cfd0bac398195 \
    https://github.com/tailwindlabs/tailwindcss/releases/download/v4.3.3/tailwindcss-linux-arm64 /usr/local/bin/tailwindcss

# Stage css: builds the admin stylesheet. It copies every path the entry lists in @source,
# so the build sees exactly the classes the templates and admin.js use. Also the target of
# the local dev watcher (docker-compose.dev-ui.yml).
FROM tailwind-${TARGETARCH} AS css
WORKDIR /app
COPY powermon/web/assets powermon/web/assets
COPY powermon/web/templates powermon/web/templates
COPY powermon/web/static/web/admin.js powermon/web/static/web/admin.js
RUN tailwindcss -i powermon/web/assets/css/app.css -o /out/app.css --minify

# Stage base: runtime dependencies from the lock (fails if uv.lock is stale), app code, static files.
FROM uvtool AS base
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev
COPY . /app
# The built CSS goes in after the source copy and before collectstatic, which hashes it into
# the manifest. Only this one file leaves the css stage: no Tailwind binary, no Node.
COPY --from=css /out/app.css powermon/web/static/web/build/app.css
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
