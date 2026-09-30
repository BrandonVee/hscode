FROM ghcr.io/astral-sh/uv:0.12.19 AS uv
FROM postgres:16-bookworm AS postgres-client

FROM python:3.13-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ARG PYPI_INDEX_URL=https://pypi.org/simple
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_HTTP_TIMEOUT=120
COPY pyproject.toml uv.lock ./
# requirements 只携带锁定版本和哈希，让镜像源能实际接管包下载地址。
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file /tmp/requirements.lock > /dev/null \
    && uv venv /app/.venv \
    && uv pip sync --python /app/.venv/bin/python --require-hashes \
        --default-index "$PYPI_INDEX_URL" /tmp/requirements.lock
COPY README.md ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv pip install --python /app/.venv/bin/python --no-deps \
        --default-index "$PYPI_INDEX_URL" .

FROM python:3.13-slim-bookworm AS runtime
ARG DEBIAN_MIRROR=http://deb.debian.org/debian
ARG DEBIAN_SECURITY_MIRROR=http://deb.debian.org/debian-security
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    sed -i -E \
        -e "s|^URIs: https?://deb.debian.org/debian$|URIs: ${DEBIAN_MIRROR}|" \
        -e "s|^URIs: https?://deb.debian.org/debian-security$|URIs: ${DEBIAN_SECURITY_MIRROR}|" \
        /etc/apt/sources.list.d/debian.sources \
    && rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get update \
    && apt-get install -y --no-install-recommends libpq5 liblz4-1 libzstd1 \
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
