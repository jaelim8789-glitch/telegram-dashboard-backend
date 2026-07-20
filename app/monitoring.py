"""
Production Monitoring Module — Prometheus metrics, structured JSON logging, alerting.

Provides:
  - Prometheus metrics endpoint (/metrics)
  - Structured JSON logging for log aggregation (ELK, Datadog, etc.)
  - Critical error alerting via webhook
  - System health metrics (memory, connections, runtime status)
  - Request latency tracking

v1 — Production monitoring foundation.
"""

from __future__ import annotations

import json
import logging
import os
import time
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import Request, Response
from fastapi.routing import APIRoute

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────

METRICS_ENABLED = os.environ.get("METRICS_ENABLED", "true").lower() == "true"
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")
ALERT_LEVEL = os.environ.get("ALERT_LEVEL", "ERROR").upper()
STRUCTURED_LOGGING = os.environ.get("STRUCTURED_LOGGING", "true").lower() == "true"

# ── Prometheus Metrics (lightweight, no external dependency) ─────────

_metrics: dict[str, Any] = {
    "http_requests_total": 0,
    "http_requests_active": 0,
    "http_requests_errors_total": 0,
    "http_request_duration_seconds_sum": 0.0,
    "http_request_duration_seconds_count": 0,
    "runtimes_total": 0,
    "runtimes_active": 0,
    "runtimes_errors_total": 0,
    "db_queries_total": 0,
    "db_queries_errors_total": 0,
    "db_query_duration_seconds_sum": 0.0,
    "auto_replies_total": 0,
    "broadcasts_total": 0,
    "rate_limits_hit_total": 0,
    "healing_actions_total": 0,
    "healing_actions_success_total": 0,
    "last_alert_timestamp": 0.0,
    "startup_timestamp": time.time(),
}

_metrics_lock = threading.Lock()


def inc_metric(name: str, value: int = 1) -> None:
    """Increment a counter metric."""
    with _metrics_lock:
        if name in _metrics:
            _metrics[name] += value
        else:
            _metrics[name] = value


def set_metric(name: str, value: Any) -> None:
    """Set a gauge metric."""
    with _metrics_lock:
        _metrics[name] = value


def observe_duration(name_sum: str, name_count: str, duration: float) -> None:
    """Record a duration observation."""
    with _metrics_lock:
        _metrics[name_sum] = _metrics.get(name_sum, 0.0) + duration
        _metrics[name_count] = _metrics.get(name_count, 0) + 1


def get_metrics_text() -> str:
    """Generate Prometheus exposition-format text."""
    with _metrics_lock:
        lines = [
            "# HELP telemon_startup_timestamp Startup timestamp in seconds",
            "# TYPE telemon_startup_timestamp gauge",
            f"telemon_startup_timestamp {_metrics.get('startup_timestamp', 0)}",
            "",
            "# HELP telemon_http_requests_total Total HTTP requests",
            "# TYPE telemon_http_requests_total counter",
            f"telemon_http_requests_total {_metrics.get('http_requests_total', 0)}",
            "",
            "# HELP telemon_http_requests_active Currently active HTTP requests",
            "# TYPE telemon_http_requests_active gauge",
            f"telemon_http_requests_active {_metrics.get('http_requests_active', 0)}",
            "",
            "# HELP telemon_http_requests_errors_total Total HTTP errors",
            "# TYPE telemon_http_requests_errors_total counter",
            f"telemon_http_requests_errors_total {_metrics.get('http_requests_errors_total', 0)}",
            "",
            "# HELP telemon_http_request_duration_seconds HTTP request duration summary",
            "# TYPE telemon_http_request_duration_seconds summary",
            f"telemon_http_request_duration_seconds_sum {_metrics.get('http_request_duration_seconds_sum', 0.0)}",
            f"telemon_http_request_duration_seconds_count {_metrics.get('http_request_duration_seconds_count', 0)}",
            "",
            "# HELP telemon_runtimes_total Total registered runtimes",
            "# TYPE telemon_runtimes_total gauge",
            f"telemon_runtimes_total {_metrics.get('runtimes_total', 0)}",
            "",
            "# HELP telemon_runtimes_active Currently active runtimes",
            "# TYPE telemon_runtimes_active gauge",
            f"telemon_runtimes_active {_metrics.get('runtimes_active', 0)}",
            "",
            "# HELP telemon_runtimes_errors_total Total runtime errors",
            "# TYPE telemon_runtimes_errors_total counter",
            f"telemon_runtimes_errors_total {_metrics.get('runtimes_errors_total', 0)}",
            "",
            "# HELP telemon_db_queries_total Total database queries",
            "# TYPE telemon_db_queries_total counter",
            f"telemon_db_queries_total {_metrics.get('db_queries_total', 0)}",
            "",
            "# HELP telemon_db_queries_errors_total Total database query errors",
            "# TYPE telemon_db_queries_errors_total counter",
            f"telemon_db_queries_errors_total {_metrics.get('db_queries_errors_total', 0)}",
            "",
            "# HELP telemon_db_query_duration_seconds Database query duration summary",
            "# TYPE telemon_db_query_duration_seconds summary",
            f"telemon_db_query_duration_seconds_sum {_metrics.get('db_query_duration_seconds_sum', 0.0)}",
            f"telemon_db_query_duration_seconds_count {_metrics.get('db_query_duration_seconds_count', 0)}",
            "",
            "# HELP telemon_auto_replies_total Total auto-replies sent",
            "# TYPE telemon_auto_replies_total counter",
            f"telemon_auto_replies_total {_metrics.get('auto_replies_total', 0)}",
            "",
            "# HELP telemon_broadcasts_total Total broadcasts processed",
            "# TYPE telemon_broadcasts_total counter",
            f"telemon_broadcasts_total {_metrics.get('broadcasts_total', 0)}",
            "",
            "# HELP telemon_rate_limits_hit_total Total rate limit hits",
            "# TYPE telemon_rate_limits_hit_total counter",
            f"telemon_rate_limits_hit_total {_metrics.get('rate_limits_hit_total', 0)}",
            "",
            "# HELP telemon_healing_actions_total Total healing actions",
            "# TYPE telemon_healing_actions_total counter",
            f"telemon_healing_actions_total {_metrics.get('healing_actions_total', 0)}",
            "",
            "# HELP telemon_healing_actions_success_total Successful healing actions",
            "# TYPE telemon_healing_actions_success_total counter",
            f"telemon_healing_actions_success_total {_metrics.get('healing_actions_success_total', 0)}",
            "",
            "# HELP telemon_last_alert_timestamp Timestamp of last sent alert",
            "# TYPE telemon_last_alert_timestamp gauge",
            f"telemon_last_alert_timestamp {_metrics.get('last_alert_timestamp', 0)}",
            "",
            "# HELP telemon_up Was the last scrape successful",
            "# TYPE telemon_up gauge",
            "telemon_up 1",
        ]
        return "\n".join(lines)


# ── Structured JSON Logging ──────────────────────────────────────────

class JSONFormatter(logging.Formatter):
    """Log formatter that outputs JSON lines for log aggregation."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        if hasattr(record, "structured_data"):
            log_entry["data"] = record.structured_data
        return json.dumps(log_entry, default=str)


def setup_structured_logging(app_name: str = "telemon") -> None:
    """Configure root logger with JSON formatter for production.

    If STRUCTURED_LOGGING is false, falls back to standard text format.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler()
    if STRUCTURED_LOGGING:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
    root_logger.addHandler(handler)

    # Set third-party loggers to WARNING
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    logger.info("Structured logging initialized for %s", app_name)


# ── Alerting ─────────────────────────────────────────────────────────

def send_alert(
    title: str,
    message: str,
    severity: str = "error",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Send an alert via webhook (Discord/Slack/Telegram compatible).

    Falls back to log warning if webhook URL not configured.
    """
    if not ALERT_WEBHOOK_URL:
        logger.warning("Alert not sent (no ALERT_WEBHOOK_URL): %s - %s", title, message)
        return

    try:
        import urllib.request

        payload = {
            "title": f"[{severity.upper()}] {title}",
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": severity,
            "service": "telemon",
        }
        if metadata:
            payload["metadata"] = metadata

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            ALERT_WEBHOOK_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)

        with _metrics_lock:
            _metrics["last_alert_timestamp"] = time.time()

        logger.info("Alert sent: %s [%s]", title, severity)
    except Exception as e:
        logger.error("Failed to send alert via webhook: %s", e)


def alert_error(title: str, message: str, metadata: dict[str, Any] | None = None) -> None:
    """Send an ERROR-level alert."""
    send_alert(title, message, severity="error", metadata=metadata)


def alert_critical(title: str, message: str, metadata: dict[str, Any] | None = None) -> None:
    """Send a CRITICAL-level alert (triggers pager duty)."""
    send_alert(title, message, severity="critical", metadata=metadata)


def alert_warning(title: str, message: str, metadata: dict[str, Any] | None = None) -> None:
    """Send a WARNING-level alert."""
    send_alert(title, message, severity="warning", metadata=metadata)


# ── Middleware ────────────────────────────────────────────────────────

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse


class MetricsMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware for request metrics tracking.

    Usage:
        app.add_middleware(MetricsMiddleware)
    """

    async def dispatch(self, request: StarletteRequest, call_next: Callable) -> StarletteResponse:
        inc_metric("http_requests_total")
        inc_metric("http_requests_active")
        start_time = time.monotonic()

        try:
            response = await call_next(request)
            return response
        except Exception as e:
            inc_metric("http_requests_errors_total")
            alert_error(
                "HTTP Request Error",
                f"{request.method} {request.url.path}: {e}",
                {"path": request.url.path, "method": request.method},
            )
            raise
        finally:
            duration = time.monotonic() - start_time
            observe_duration(
                "http_request_duration_seconds_sum",
                "http_request_duration_seconds_count",
                duration,
            )
            dec_metric("http_requests_active")


def dec_metric(name: str, value: int = 1) -> None:
    """Decrement a gauge metric."""
    with _metrics_lock:
        _metrics[name] = max(0, _metrics.get(name, 0) - value)


# ── Metrics endpoint handler ─────────────────────────────────────────

async def metrics_endpoint(request: Request) -> Response:
    """Serve Prometheus metrics at /metrics."""
    return Response(
        content=get_metrics_text(),
        media_type="text/plain; charset=utf-8",
    )


# ── Runtime metrics updater ──────────────────────────────────────────

def update_runtime_metrics(total: int, active: int) -> None:
    """Update runtime metrics from RuntimeManager."""
    set_metric("runtimes_total", total)
    set_metric("runtimes_active", active)