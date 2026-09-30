FROM ghcr.io/astral-sh/uv:0.12.19 AS uv
FROM postgres:16-bookworm AS postgres-client

FROM python:3.13-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim-bookworm AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 liblz4-1 libzstd1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 hscode \
    && mkdir -p /cache/models /exports \
    && chown -R hscode:hscode /cache /exports
COPY --from=postgres-client /usr/lib/postgresql/16/bin/pg_dump /usr/lib/postgresql/16/bin/pg_restore /usr/local/bin/
COPY --from=builder /app/.venv /app/.venv
RUN pg_dump --version && pg_restore --version
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HS_MODEL_CACHE=/cache/models \
    MCP_HTTP_PATH=/mcp/hscode
USER hscode
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "-m", "hscode.healthcheck"]
CMD ["hscode", "--http", "8765", "--host", "0.0.0.0", "--stateless"]
