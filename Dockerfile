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
# ffmpeg: needed by app/services/ai_vision_service.py to extract frames from
# uploaded videos for AI vision analysis (qwen2.5vl).
RUN apt-get update && apt-get install -y --no-install-recommends fonts-nanum ffmpeg && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 appuser && mkdir -p /app/media/broadcasts /app/logs /app/data/uploads
WORKDIR /app

COPY --from=builder /root/.local /home/appuser/.local
ENV PATH=/home/appuser/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .
COPY migration_helpers.py .

RUN chown -R appuser:appuser /app /home/appuser/.local
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)" || exit 1

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
