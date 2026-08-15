# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/
ENV UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/omarag-venv
WORKDIR /build/python
COPY python/pyproject.toml python/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --extra haiku --no-dev --no-install-project
COPY python/ ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --extra haiku --no-dev --no-editable
RUN /opt/omarag-venv/bin/python -m omarag_bridge.compat_probe

FROM python:3.12-slim AS runtime
ARG VERSION=1.2.0
LABEL org.opencontainers.image.title="OmaRag" \
      org.opencontainers.image.description="Oracle of Metis & Aletheia — local evidence workbench" \
      org.opencontainers.image.source="https://github.com/Baulehrer/OmaRag" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.licenses="MIT"
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 omarag \
    && useradd --uid 10001 --gid omarag --home-dir /var/lib/omarag \
        --shell /usr/sbin/nologin omarag \
    && install -d -o omarag -g omarag /var/lib/omarag
COPY --from=builder /opt/omarag-venv /opt/omarag-venv
ENV PATH=/opt/omarag-venv/bin:$PATH \
    HOME=/var/lib/omarag \
    OMARAG_DATA_DIR=/var/lib/omarag \
    MALLOC_ARENA_MAX=2 \
    MALLOC_TRIM_THRESHOLD_=131072 \
    TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=4 \
    MKL_NUM_THREADS=4 \
    OPENBLAS_NUM_THREADS=4 \
    NUMEXPR_NUM_THREADS=4 \
    RAYON_NUM_THREADS=4 \
    PYTHONUNBUFFERED=1
WORKDIR /var/lib/omarag
VOLUME ["/var/lib/omarag"]
USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/v1/health', timeout=2)"
ENTRYPOINT ["omaragd"]
CMD ["--host", "0.0.0.0", "--port", "8765"]
