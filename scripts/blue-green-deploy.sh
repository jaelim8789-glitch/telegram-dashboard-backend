#!/bin/bash
# Blue-Green Deploy Script for TeleMon
# Usage: ./scripts/blue-green-deploy.sh [service]
#
# Deploys with zero downtime by:
# 1. Starting new container alongside old one
# 2. Waiting for health check
# 3. Switching traffic
# 4. Stopping old container

set -euo pipefail

SERVICE="${1:-backend}"
COMPOSE_FILE="${2:-docker-compose.yml}"
HEALTH_TIMEOUT=60
HEALTH_INTERVAL=5

echo "🔄 Blue-Green Deploy: $SERVICE"

# Step 1: Scale up
echo "📦 Starting new container..."
docker compose -f "$COMPOSE_FILE" up -d --no-deps --scale "${SERVICE}=2" "${SERVICE}"

# Step 2: Find new container
NEW_CONTAINER=$(docker compose -f "$COMPOSE_FILE" ps -q "${SERVICE}" | tail -1)
echo "📦 New container: ${NEW_CONTAINER:0:12}"

# Step 3: Health check
echo "🏥 Waiting for health check..."
ELAPSED=0
while [ $ELAPSED -lt $HEALTH_TIMEOUT ]; do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' "$NEW_CONTAINER" 2>/dev/null || echo "starting")
    if [ "$STATUS" = "healthy" ]; then
        echo "✅ New container is healthy!"
        break
    fi
    sleep $HEALTH_INTERVAL
    ELAPSED=$((ELAPSED + HEALTH_INTERVAL))
    echo "⏳ Waiting... ($ELAPSED/${HEALTH_TIMEOUT}s)"
done

if [ "$STATUS" != "healthy" ]; then
    echo "❌ Health check failed. Rolling back..."
    docker compose -f "$COMPOSE_FILE" up -d --no-deps --scale "${SERVICE}=1" "${SERVICE}"
    exit 1
fi

# Step 4: Stop old container
OLD_CONTAINER=$(docker compose -f "$COMPOSE_FILE" ps -q "${SERVICE}" | head -1)
if [ "$OLD_CONTAINER" != "$NEW_CONTAINER" ]; then
    echo "🛑 Stopping old container: ${OLD_CONTAINER:0:12}"
    docker stop "$OLD_CONTAINER"
    docker rm "$OLD_CONTAINER"
fi

echo "✅ Blue-Green deploy complete: $SERVICE"
