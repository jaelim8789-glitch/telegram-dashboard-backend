#!/bin/bash
set -e # 스크립트 실행 중 오류 발생 시 즉시 종료

echo "Deploying backend..."

# 1. 현재 디렉터리 확인 (Backend repo root)
if [[ ! -f "requirements.txt" ]]; then
  echo "Error: Not in the backend repository root." >&2
  exit 1
fi

# 2. Deploy용 worktree 생성
DEPLOY_DIR="../telegram-dashboard-backend-deploy"
echo "Creating worktree at $DEPLOY_DIR..."
git worktree add --detach "$DEPLOY_DIR" origin/master

# 3. 빌드 및 도커 이미지 생성
echo "Building Docker image..."
cd "$DEPLOY_DIR"
docker build -t ghcr.io/jaelim8789-glitch/telemon-backend:latest .

# 4. 도커 컴포즈로 배포
echo "Deploying with Docker Compose..."
cd .. # 현재 Backend repo root
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps --wait backend

# 5. Worktree 정리
echo "Cleaning up worktree..."
git worktree remove "$DEPLOY_DIR" --force

echo "Backend deployment completed successfully!"