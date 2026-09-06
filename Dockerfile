# One image, two entry points. The ERP and the review UI share every
# dependency, so building them separately would double the build time and the
# registry footprint to save nothing -- `command:` picks which one runs.
#
# Multi-stage so the runtime layer carries no build tooling and no lockfile
# resolution machinery.

FROM ghcr.io/astral-sh/uv:0.9-python3.13-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, as their own layer. Source changes far more often than
# the lockfile, so this is the difference between a 3-second rebuild and a
# 90-second one.
COPY pyproject.toml uv.lock ./
COPY packages/erp_domain/pyproject.toml packages/erp_domain/
COPY packages/generator/pyproject.toml  packages/generator/
COPY packages/mock_erp/pyproject.toml   packages/mock_erp/
COPY packages/agent/pyproject.toml      packages/agent/
COPY packages/evals/pyproject.toml      packages/evals/
COPY packages/review_ui/pyproject.toml  packages/review_ui/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-workspace

COPY packages/ packages/
COPY scripts/ scripts/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked


FROM python:3.13-slim-bookworm AS runtime

# Never root. The service reads a data directory and writes one sqlite file;
# nothing it does needs more than that.
RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app
COPY --from=build --chown=app:app /app/.venv /app/.venv
COPY --from=build --chown=app:app /app/packages /app/packages
COPY --from=build --chown=app:app /app/scripts /app/scripts

# The generated dataset. Baked in rather than mounted: it is a pure function of
# a seed and a config that both live in version control, so an image and its
# data are one reproducible artefact.
COPY --chown=app:app data/ /app/data/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MOCK_ERP_ERP_DATA_DIR=/app/data/erp \
    MOCK_ERP_DB_PATH=/data/approvals.sqlite3

# The approval database is the only mutable state. A volume, so the image stays
# read-only and a restart does not lose an audit trail.
RUN mkdir -p /data && chown app:app /data
VOLUME ["/data"]

USER app
EXPOSE 8000 8001

# Overridden by compose / the k8s manifests. Defaults to the ERP because
# nothing else in the system works without it.
CMD ["uvicorn", "mock_erp.app:app", "--host", "0.0.0.0", "--port", "8000"]
