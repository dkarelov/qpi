FROM python:3.14-slim AS production

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /usr/local/bin/

WORKDIR /bot/upstream

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY upstream/pyproject.toml upstream/uv.lock ./
RUN uv sync --locked --no-dev

COPY upstream/app ./app
COPY upstream/config ./config

CMD ["uv", "run", "--no-sync", "python", "-m", "app"]
