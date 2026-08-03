# Alembic 마이그레이션 관리 가이드

## 문제 history

### 2026-08-03 발생한 문제
- `escrow_trust_001` 마이그레이션이 `alembic upgrade heads`에서 "accounts 테이블이 이미 존재함" 에러 발생
- 원인: 배포 스크립트의 `alembic upgrade heads 2>/dev/null`이 에러를 조용히 삼킴
- 결과: escrow/trust/bookmark 관련 테이블이 한 번도 alembic으로 생성된 적 없음
- 응급 조치: `create_all()`로 테이블 생성 → alembic 버전을 head로 수정

### 근본 원인
1. 기존 마이그레이션의 `op.create_table()`이 idempotent하지 않음 (이미 존재하는 테이블 생성 시 에러)
2. 배포 스크립트가 에러를 `2>/dev/null`로 삼킴 → 실패가 무시됨
3. `create_all()`로 임의 테이블 생성 시 alembic이 모르는 테이블이 DB에 존재

### 해결
1. `alembic/utils.py` — `create_table_if_not_exists()`, `drop_table_if_exists()`, `add_column_if_not_exists()` 유틸리티 생성
2. 33개 마이그레이션 파일의 78개 `op.create_table()`을 `create_table_if_not_exists()`로 전환
3. 배포 스크립트에서 `2>/dev/null` 제거

## 재발 방지 규칙

### 새 마이그레이션 생성 시
- `op.create_table()` → 반드시 `create_table_if_not_exists()` 사용
- `op.add_column()` → 반드시 `add_column_if_not_exists()` 사용 (선택사항)
- `op.drop_table()` → 반드시 `drop_table_if_exists()` 사용 (선택사항)

### 배포 스크립트
- `alembic upgrade heads 2>/dev/null` 사용 금지
- 에러 시 컨테이너가 죽거나 명확한 로그가 남아야 함

### `create_all()` 사용
- alembic 마이그레이션이 아닌 방법으로 테이블을 생성하지 않음
- 긴급 복구 시 `create_all()` 사용 후 반드시 `alembic stamp head` 실행

## 유틸리티 위치

```
telegram-dashboard-backend/alembic/utils.py
```

```python
from alembic.utils import create_table_if_not_exists

def upgrade():
    create_table_if_not_exists(
        "my_table",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200)),
    )
```
