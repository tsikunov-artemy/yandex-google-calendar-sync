#syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.12-slim

FROM ghcr.io/astral-sh/uv:latest AS uv

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONFAULTHANDLER=1 \
    PYTHONHASHSEED=random \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
COPY --from=uv /uv /bin/uv
WORKDIR /app

FROM base AS builder
COPY pyproject.toml uv.lock /app/
RUN uv venv /venv && UV_PROJECT_ENVIRONMENT=/venv uv sync --locked

FROM base AS final
ENV PATH=/venv/bin:$PATH
COPY --from=builder /venv /venv
COPY src/ ./src/
RUN adduser --system --no-create-home --uid 1000 syncuser
RUN mkdir -p /app/data /app/credentials && chown -R syncuser /app/data /app/credentials
USER syncuser

ENTRYPOINT ["python", "-m", "src.main"]
