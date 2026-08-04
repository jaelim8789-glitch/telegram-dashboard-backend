#!/bin/sh
set -e

# TeleMon DR Recovery — run from the backend host (VPS).
#
# Usage:
#   ./scripts/dr_recovery.sh <service>    # postgres | redis | backend | all
#
# Recovers a failed service with minimal downtime and verifies it came back.
# Always sends a notification via ALERT_WEBHOOK_URL when the incident monitor
# is running (it detects the recovery itself); this script is the manual/DR
# path for when auto-recovery cannot act (e.g. host-level failure).

SERVICE="${1:-all}"
COMPOSE_DIR="${COMPOSE_DIR:-/opt/telemon/backend}"
WEBHOOK="${ALERT_WEBHOOK_URL:-}"
HOSTNAME_LABEL="${HOSTNAME:-telemon-prod-01}"

now() { date '+%Y-%m-%d %H:%M:%S'; }

notify() {
  if [ -n "$WEBHOOK" ]; then
    curl -s -X POST "$WEBHOOK" \
      -H 'Content-Type: application/json' \
      -d "{\"title\":\"$1\",\"message\":\"$2\",\"severity\":\"info\",\"server\":\"$HOSTNAME_LABEL\"}" >/dev/null 2>&1 || true
  fi
}

cd "$COMPOSE_DIR" || { echo "[$(now)] ERROR: compose dir not found: $COMPOSE_DIR"; exit 1; }

restore_postgres() {
  echo "[$(now)] Recovering PostgreSQL..."
  docker compose up -d db
  for i in $(seq 1 12); do
    if docker compose exec -T db pg_isready -U telegram_dashboard >/dev/null 2>&1; then
      echo "[$(now)] PostgreSQL healthy"
      notify "RECOVERED: PostgreSQL" "PostgreSQL restored (manual DR)"
      return 0
    fi
    sleep 5
  done
  echo "[$(now)] ERROR: PostgreSQL did not become healthy"
  notify "CRITICAL: PostgreSQL" "Manual DR could not restore PostgreSQL"
  return 1
}

restore_redis() {
  echo "[$(now)] Recovering Redis..."
  docker compose up -d redis
  sleep 3
  if docker compose exec -T redis redis-cli -a "$$REDIS_PASSWORD" ping 2>/dev/null | grep -q PONG; then
    echo "[$(now)] Redis healthy"
    notify "RECOVERED: Redis" "Redis restored (manual DR)"
    return 0
  fi
  echo "[$(now)] ERROR: Redis did not respond"
  notify "CRITICAL: Redis" "Manual DR could not restore Redis"
  return 1
}

restore_backend() {
  echo "[$(now)] Recovering backend..."
  docker compose up -d --no-deps backend
  for i in $(seq 1 12); do
    if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
      echo "[$(now)] Backend healthy"
      notify "RECOVERED: Backend" "Backend restored (manual DR)"
      return 0
    fi
    sleep 5
  done
  echo "[$(now)] ERROR: Backend did not become healthy"
  notify "CRITICAL: Backend" "Manual DR could not restore backend"
  return 1
}

case "$SERVICE" in
  postgres) restore_postgres ;;
  redis)    restore_redis ;;
  backend)  restore_backend ;;
  all)
    restore_postgres
    restore_redis
    restore_backend
    ;;
  *)
    echo "Usage: $0 <postgres|redis|backend|all>"
    exit 1
    ;;
esac

echo "[$(now)] DR recovery finished: $SERVICE"
