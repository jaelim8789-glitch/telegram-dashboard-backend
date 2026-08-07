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

# 4. 도커 이미지 GHCR에 푸시 (이미지 푸시 추가 - 아이디어 21)
echo "Pushing Docker image to GHCR..."
docker push ghcr.io/jaelim8789-glitch/telemon-backend:latest

# 5. 도커 컴포즈로 이전 컨테이너 정리 (down 추가 - 아이디어 16)
echo "Stopping old containers..."
# 현재 Backend repo root
docker compose -f docker-compose.yml -f docker-compose.prod.yml down backend

# 6. 도커 컴포즈로 배포 (up)
echo "Deploying with Docker Compose..."
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps backend
# --wait 옵션은 헬스체크가 설정되어 있어야 효과적입니다. (아이디어 27)

# 7. Git 태그 생성 (태깅 전략 도입 - 아이디어 19)
echo "Tagging the deployed commit..."
# 현재 Backend repo root
TAG_NAME="deploy-be-$(date +%Y%m%d-%H%M)"
git tag -a "$TAG_NAME" -m "Deployed backend version $TAG_NAME"
git push origin "$TAG_NAME"

# 8. Worktree 정리
echo "Cleaning up worktree..."
git worktree remove "$DEPLOY_DIR" --force

echo "Backend deployment completed successfully! Tag: $TAG_NAME"