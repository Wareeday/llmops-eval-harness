# syntax=docker/dockerfile:1.7
# Multi-stage, non-root, reproducible image. Works on Docker, Render and Hugging Face Spaces.

FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt constraints.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install -r requirements.txt -c constraints.txt

FROM python:3.12-slim AS runtime
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="llmops-agent-eval-harness" \
      org.opencontainers.image.description="Evaluate, red-team, cache and observe any AI agent (Arabic + English)" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${GIT_SHA}"

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=7860 \
    GIT_SHA=${GIT_SHA} \
    ENVIRONMENT=production \
    SQLITE_PATH=/app/data_store/llmops_traces.db \
    REPORTS_DIR=/app/reports

# UID 1000 is required by Hugging Face Spaces and matches most Kubernetes defaults.
RUN useradd --create-home --uid 1000 appuser
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser datasets ./datasets
COPY --chown=appuser:appuser frontend ./frontend
COPY --chown=appuser:appuser examples ./examples
COPY --chown=appuser:appuser cli_eval.py ./cli_eval.py
RUN mkdir -p /app/data_store /app/reports && chown -R appuser:appuser /app/data_store /app/reports

USER appuser
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','7860'), timeout=4)" || exit 1

# `sh -c` lets platforms inject $PORT (Render, Cloud Run, ...); `exec` keeps signals working.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860} --no-access-log --proxy-headers"]
