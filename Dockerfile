# Multi-stage build: the builder stage's toolchain (gcc etc., pulled in by some wheels)
# never ends up in the final image, only the installed packages do.
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --user -r requirements.txt

FROM python:3.12-slim

# fonts-nanum: Korean-capable TTF, needed by app/services/performance_card.py
# to render the shareable stats card (python:3.12-slim ships no fonts at all).
RUN apt-get update && apt-get install -y --no-install-recommends fonts-nanum && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

COPY --from=builder /root/.local /home/appuser/.local
ENV PATH=/home/appuser/.local/bin:$PATH \
    PYTHONUNBUFFERED=1

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .

# Pre-create the dirs the app writes to (media/ is a shared named volume mounted here —
# Docker seeds a fresh named volume from whatever already exists at the mount point in
# the image, ownership included, which is what makes this work for the non-root user
# below instead of the mount showing up root-owned).
RUN mkdir -p /app/media/broadcasts /app/logs && chown -R appuser:appuser /app /home/appuser/.local
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)" || exit 1

# Exec form with explicit shell so $PORT expands — Render and similar free-tier
# hosts assign the port dynamically via this env var instead of a fixed 8000.
# Runs migrations before starting the app so a fresh database is always ready.
# Exec form ensures the uvicorn process receives SIGTERM directly (not the shell),
# enabling graceful shutdown.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
