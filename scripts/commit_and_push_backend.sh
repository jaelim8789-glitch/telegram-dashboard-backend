#!/bin/bash
set -e # 스크립트 실행 중 오류 발생 시 즉시 종료

echo "Adding changes and committing for backend..."

# 변경된 파일 모두 추가
git add .

# 변경 사항 커밋
git commit -m "chore: apply development speed optimizations (deploy script)"

# 원격 저장소에 푸시
git push origin master

echo "Backend changes committed and pushed successfully!"