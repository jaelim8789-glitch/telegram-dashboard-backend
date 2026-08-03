# 작업 지시서 — Alembic 마이그레이션 이력 정리 (신중하게, 시간 걸리는 작업)

## 배경
`telegram-dashboard-backend`의 alembic 마이그레이션 히스토리가 두 개의 서로 연결 안 된 체인으로 갈라져 있습니다:
- 체인 A: `<base> -> dfe6ec1e04a3 (create accounts table)` 부터 시작해서 `merge_folders_and_reply_macro_heads` 등을 거쳐 `escrow_trust_001`까지 이어짐
- 체인 B: `<base> -> b1d2e3f4a5b6 (Stub migration, placeholder)` 부터 시작해서 referral 관련 테이블들을 거쳐 `f7a8b9c0d1e2`까지 이어짐

DB의 `alembic_version` 테이블은 현재 `b1d2e3f4a5b6`(체인 B)에 stamp 되어 있는데, **실제 운영 DB의 물리적 스키마는 체인 A쪽 테이블(folders, team_members, reply_macros 등)까지 이미 다 가지고 있습니다.** 즉 alembic이 기록하고 있는 "현재 위치"와 실제 DB 상태가 다릅니다.

이것 때문에 오늘 실제로 터진 문제:
- `escrow_trust_001` 마이그레이션이 `alembic upgrade heads` 실행 시 "accounts 테이블이 이미 존재함" 에러로 실패 (체인 A 전체를 처음부터 다시 만들려고 시도했기 때문)
- 배포 스크립트가 `alembic upgrade heads 2>/dev/null`로 에러를 조용히 삼키고 있어서, 이 실패가 몇 주간 아무도 모르게 지나갔고 그 결과 escrow/trust/bookmark 관련 테이블들이 실제로는 한 번도 생성된 적이 없었음 (제가 오늘 응급으로 `create_all()`을 직접 호출해서 테이블만 만들어놓은 상태 — 근본 원인은 그대로 남아있음)

## 목표
alembic이 실제 DB 상태를 정확히 알고 있는 단일하고 일관된 상태로 만들기. 이후 새로운 마이그레이션을 추가해도 `alembic upgrade heads`가 항상 정상 동작해야 함.

## 작업 방식
1. `git checkout master && git pull origin master && git checkout -b chore/alembic-history-cleanup`
2. **운영 DB를 직접 건드리는 명령(`alembic stamp`, `alembic upgrade` 등)은 로컬/스테이징에서 충분히 검증 후에만 실행하세요. 이 브랜치의 PR은 merge 전에 반드시 사람이 리뷰하도록 남겨두세요 (직접 merge 금지, 이번 작업은 리스크가 있어서 예외입니다).**
3. 끝나면 PR만 열어두고, **merge는 하지 마세요.**

## 진행 순서 (제안)
1. **현재 상태 정확히 파악**
   - `alembic history` 전체 출력을 텍스트로 저장
   - `\dt` (또는 `information_schema.tables`)로 운영 DB의 실제 테이블 목록 전체를 뽑기
   - 두 체인 각각의 마이그레이션이 "어떤 테이블/컬럼을 만드는지" 매핑표를 만들어서, 실제 DB에 있는 테이블과 대조 — 체인 A/B 각각 어디까지 실제로 반영되어 있는지 확인
2. **정리 전략 결정** (아마 이 방식이 맞을 것 같습니다, 확인 후 진행)
   - 두 체인을 하나로 합치는 merge revision을 작성 (`down_revision = (chain_a_head, chain_b_head)`), 이 merge revision의 `upgrade()`는 비워둠 (이미 실제로 다 적용되어 있으므로 새로 실행할 게 없음)
   - 로컬 테스트 DB를 운영 DB 스키마와 최대한 비슷하게 만든 후, 이 merge revision까지 `alembic stamp`가 정상 동작하는지 검증
3. **로컬/스테이징에서 전체 리허설**
   - 운영 DB의 스키마만 복제한 테스트 DB를 만들고 (데이터는 필요 없음, `pg_dump --schema-only` 같은 걸로)
   - 그 위에서 `alembic stamp <merge-revision>` 실행 → 이후 `alembic upgrade heads`가 아무 에러 없이 "이미 최신 상태"로 나오는지 확인
   - 그 다음 새 더미 마이그레이션 하나를 만들어서 (예: 테스트용 컬럼 추가) `alembic upgrade heads`가 정상적으로 그 하나만 적용하는지 확인 → 확인되면 더미 마이그레이션은 삭제
4. **문서화**
   - `docs/` 아래에 이번에 발견한 문제와 정리 과정을 기록 (다음에 비슷한 일이 왜 생겼는지, 어떻게 진단했는지, 재발 방지를 위해 뭘 바꿨는지)
   - 배포 스크립트의 `alembic upgrade heads 2>/dev/null`에서 `2>/dev/null` 제거하거나, 최소한 실패 시 컨테이너가 죽거나 명확히 로그에 ERROR로 남도록 변경 — 에러를 조용히 삼키는 게 이번 사고의 핵심 원인 중 하나였습니다.

## 절대 하지 말아야 할 것
- 운영 DB에 `alembic stamp heads`를 검증 없이 바로 실행 (테이블이 조용히 스킵되거나 잘못된 상태로 stamp될 위험)
- `alembic upgrade heads`를 운영 DB에 검증 없이 실행 (오늘 있었던 것처럼 "accounts 테이블 이미 존재" 에러로 배포가 깨질 수 있음)
- 기존 alembic 마이그레이션 파일들을 삭제하거나 히스토리를 rewrite (git rebase 등으로) — 오직 새 merge revision을 추가하는 방식으로만 정리

## 완료 조건
- `alembic history`가 단일하고 연결된 그래프여야 함 (또는 최소한 명시적으로 merge된 상태)
- 로컬 테스트에서 `alembic upgrade heads`가 처음부터 끝까지 에러 없이 실행됨을 증명
- PR 설명에 위 3번(리허설) 결과를 캡처해서 남길 것
- **운영 DB에는 아직 아무것도 적용하지 마세요.** PR만 올려두면, 검토 후 사람이 직접 운영 반영 여부를 결정합니다.
