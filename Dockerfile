# podium — uv multi-stage build (CP-6). One image, two commands:
#   api:       uvicorn podium.main:create_app --factory   (default CMD)
#   conductor: python -m podium.conductor                  (compose overrides command)
#
# The engine repos (dream/chorus/lattice/horizon) are uv path dependencies of podium, so the
# BUILD CONTEXT IS THE PARENT DIRECTORY holding all five checkouts:
#   docker build -f podium/Dockerfile ..        (compose does this for you)

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
WORKDIR /src/podium
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# The engine sources first (path deps), then podium itself.
COPY Harness /src/Harness
COPY chorus /src/chorus
COPY lattice /src/lattice
COPY horizon /src/horizon
COPY podium/pyproject.toml podium/uv.lock ./
COPY podium/src ./src
COPY podium/migrations ./migrations
COPY podium/alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM python:3.13-slim-bookworm AS runtime
# git: the engine's workspaces are git worktrees; the harness shells out to it per beat.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r podium && useradd -r -g podium podium
WORKDIR /src/podium
COPY --from=builder --chown=podium:podium /src /src
RUN mkdir -p /src/podium/.podium && chown podium:podium /src/podium/.podium
ENV PATH="/src/podium/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER podium

# Liveness is /healthz; readiness (/readyz) proves the DB — compose wires the healthchecks.
EXPOSE 8000
CMD ["uvicorn", "podium.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
