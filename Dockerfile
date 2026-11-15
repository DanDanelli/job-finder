# Imagem multi-arch (amd64 para desenvolvimento, arm64 para o Raspberry Pi).
FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.9.22 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    JOBFINDER_DATA_DIR=/data

WORKDIR /app

# Dependências primeiro, para aproveitar o cache de camadas.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app:app /data
USER app

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["sh", "-c", "job-finder db upgrade && exec job-finder serve"]
