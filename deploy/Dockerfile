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

FROM python:3.12-slim AS runtime
ARG VERSION=0.5.0
LABEL org.opencontainers.image.title="Oracle of Daedalus" \
      org.opencontainers.image.description="Offline Retrieval-Augmented Command-Line Environment" \
      org.opencontainers.image.source="https://github.com/Baulehrer/oracle-of-daedalus" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.licenses="MIT"
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 oracle \
    && useradd --uid 10001 --gid oracle --home-dir /var/lib/oracle-of-daedalus \
        --shell /usr/sbin/nologin oracle \
    && install -d -o oracle -g oracle /var/lib/oracle-of-daedalus
COPY --from=builder /opt/omarag-venv /opt/omarag-venv
ENV PATH=/opt/omarag-venv/bin:$PATH \
    HOME=/var/lib/oracle-of-daedalus \
    OMARAG_DATA_DIR=/var/lib/oracle-of-daedalus \
    PYTHONUNBUFFERED=1
WORKDIR /var/lib/oracle-of-daedalus
VOLUME ["/var/lib/oracle-of-daedalus"]
USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/v1/health', timeout=2)"
ENTRYPOINT ["omaragd"]
CMD ["--host", "0.0.0.0", "--port", "8765"]
