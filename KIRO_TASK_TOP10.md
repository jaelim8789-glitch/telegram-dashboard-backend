# 작업 지시서 — 계정(Account) 관리 안정화 Top 10

## 작업 방식 (반드시 지켜주세요)

1. **master에서 새 브랜치를 따서 작업하세요.** 브랜치명: `fix/account-stability-top10`
   ```
   git checkout master && git pull origin master
   git checkout -b fix/account-stability-top10
   ```
2. 항목별로 **커밋을 나눠서** 진행하세요 (한 커밋에 다 몰아넣지 말 것). 커밋 메시지에 어떤 항목(#1, #2 ...)인지 표시.
3. 다 끝나면 **master로 직접 push/merge 금지.** 대신:
   ```
   git push origin fix/account-stability-top10
   gh pr create --base master --title "fix: 계정 관리 안정화 Top 10" --body "..."
   ```
   PR만 열어두고 **merge는 하지 마세요.** (다른 AI 세션이 동시에 master에 작업 중이라, 병합 충돌/덮어쓰기 방지 위해 사람이나 다른 세션이 확인 후 머지합니다.)
4. 작업 중간에 크레딧이 끊기면, **그 시점까지 한 커밋만 push해두고 멈추세요.** (커밋 안 된 변경사항은 다음 세션에서 안 보입니다.)

## 작업 대상 저장소
- 백엔드: `telegram-dashboard-backend` (현재 디렉토리)
- 프론트: `../` (TeleMon-kiro) — 항목 중 프론트 변경 필요한 것도 있음, 표시해둠

---

## 항목별 지시사항

### #1. 세션 암호화 실패 시 계정 복구 수단 없음 (최우선)
- **현상**: `ENCRYPTION_KEY` 불일치로 `decrypt_session()`이 실패하면 해당 계정은 영구 사용 불가. 현재 유일한 해결책이 "계정 삭제 후 재등록"뿐.
- **할 일**: `app/services/telethon_pool.py` 또는 계정 관련 서비스에서 decrypt 실패를 감지하면, 계정 status를 `"session_corrupted"` 같은 명확한 상태로 표시하고, 프론트에서 "재인증 필요" 버튼(기존 재인증 흐름 재사용)으로 유도. 완전 삭제 없이 세션만 새로 발급받아 복구 가능하게.
- **파일**: `app/services/telethon_pool.py`, `app/api/deps.py`(또는 관련), 프론트 `AccountRegisterTab.tsx`의 재인증 분기.

### #2. 대화방 하나가 이상하면 계정 전체 대화 목록이 깨짐
- **현상**: `app/services/telegram_mappers.py`의 `dialog_to_dict`에서 `dialog.message.message`가 `None`인 케이스(미디어 전용 메시지) 외에도 비슷한 패턴이 더 있을 수 있음. 이미 이 특정 케이스는 고쳤음(`(dialog.message.message or "")[:200]`).
- **할 일**: `app/services/chat_actions.py`, `app/services/telegram_mappers.py` 전체를 훑어서, 대화방 목록/개별 대화 조회 시 **하나의 dialog/message 파싱 실패가 전체 요청을 500으로 만들지 않도록** try/except로 개별 항목 단위 격리. 실패한 항목은 스킵하고 로그만 남기기.

### #3. 에러 메시지가 사용자에게 뭉뚱그려짐
- **현상**: 500(서버 오류)과 403(rate limit)이 프론트에서 비슷하게 보임.
- **할 일**: 프론트 `src/lib/api.ts`의 `ApiError` 처리 부분에서 status별 한국어 메시지 분기를 계정 등록/재인증 관련 API 호출부(`AccountRegisterTab.tsx`)에도 일관되게 적용. 429는 "잠시 후 재시도" 카운트다운, 500은 "일시적 오류, 다시 시도해주세요"로 명확히 구분.

### #4. 재배포마다 봇 폴링 충돌(Conflict) 발생, 화면엔 표시 안 됨
- **현상**: 백엔드 재시작 직후 몇 분간 `telegram.error.Conflict` 로그 반복. 이 동안 자동응답 등 봇 관련 기능이 불안정할 수 있는데 사용자는 모름.
- **할 일**: 급하지 않음 — 근본 원인(재시작 시 이전 워커의 getUpdates 연결이 텔레그램 서버에서 늦게 정리됨)은 구조적이라 당장 해결 어려움. 대신 `app/services/telegram_bot_service.py`에서 Conflict 예외를 잡아 조용히 재시도하고, 일정 횟수 이상 반복되면 관리자 알림(로그 레벨을 warning→error로) 정도로 완화.

### #5. Alembic 마이그레이션 이력이 두 갈래로 끊어져 있음 (인프라 리스크)
- **현상**: `alembic heads`에 서로 연결 안 된 head가 2개(`escrow_trust_001`, `f7a8b9c0d1e2`) 있고, DB의 `alembic_version`이 실제 스키마와 안 맞음.
- **할 일**: **이건 신중하게, 위험한 작업입니다.** 절대 `alembic stamp heads`를 그냥 실행하지 마세요(멀쩡한 테이블도 재생성 시도하다 실패하거나, 반대로 진짜 필요한 마이그레이션을 건너뛸 수 있음). 대신:
  1. `alembic history`로 전체 그래프를 텍스트로 뽑고
  2. 실제 DB에 있는 테이블 목록(`\dt`)과 대조해서 각 head가 어디까지 실제로 적용됐는지 표로 정리
  3. 두 체인을 하나의 merge revision으로 합치는 마이그레이션 파일을 작성 (`down_revision = (chain1_head, chain2_head)`)
  4. **로컬/스테이징에서 먼저 테스트**, 절대 운영 DB에 바로 실행 금지. 다 정리되면 그 결과와 실행 계획만 커밋하고, 실제 운영 DB 적용은 사람 확인 후 진행.

### #6. 중복 전화번호 재등록 시 안내 없음
- **현상**: `app/api/auth.py`의 `prepare_account`(또는 `telegram_auth.py`)에 이미 등록된 phone에 대한 명시적 체크가 안 보임.
- **할 일**: 같은 tenant 내에 동일 phone의 Account가 이미 있으면 400으로 "이미 등록된 번호입니다. 계정 목록에서 확인하세요." 반환. 프론트에서 해당 메시지 그대로 표시.

### #7. 계정 상태(제한됨/차단)에서 조치 가이드 부족
- **현상**: `Sidebar.tsx`의 `HEALTH_FILTERS`에 `rate_limited`, `banned` 필터는 있는데, 그 상태일 때 사용자가 뭘 해야 하는지 안내가 없음.
- **할 일**: `AccountCard.tsx`에서 해당 상태일 때 툴팁/배지에 "계정이 일시 제한되었습니다. 24시간 후 자동 해제되거나, 텔레그램 앱에서 직접 확인하세요" 같은 안내 문구 추가. (`banned`는 텔레그램 정책 위반이라 우리 쪽에서 해줄 수 있는 게 거의 없다는 것도 명시.)

### #8. 여러 기기 로그인 시 예고 없이 로그아웃
- **현상**: `app/crud/session.py`의 `create_session()`이 새 세션 생성 시 기존 세션을 조용히 비활성화(`deactivate_all_user_sessions`). 사용자가 다른 기기에서 로그인하면 원래 기기가 예고 없이 로그아웃됨.
- **할 일**: 이건 의도된 "동시 로그인 1개만 허용" 정책인지 먼저 확인 필요(이전 세션에서 의도적으로 이렇게 고친 이력 있음 — 중복 로그인 누적 방지 목적). 만약 유지한다면, 최소한 새로 로그인할 때 프론트에서 "다른 기기에 로그인된 세션이 있습니다. 계속하면 그 세션은 로그아웃됩니다" 확인창 추가.

### #9. 서버 재시작마다 계정 수만큼 텔레그램 재연결 지연
- **현상**: `app/main.py` lifespan에서 계정마다 순차적으로 telethon 재연결(계정당 0.3~2.4초).
- **할 일**: `attach_all_account_realtime_listeners()` 쪽에서 순차(sequential) 대신 `asyncio.gather()`로 병렬 연결하도록 변경. 단, 텔레그램 API rate limit에 걸리지 않도록 동시 연결 수를 5~10개로 제한(`asyncio.Semaphore`).

### #10. CSV 대량 등록 실패 시 상세 리포트 여부 확인
- **할 일**: `AccountRegisterTab.tsx`의 CSV 업로드 처리 부분과 백엔드 대응 엔드포인트를 확인해서, 실패한 행에 대해 "몇 번째 행, 어떤 이유로 실패"를 사용자에게 보여주는지 점검. 없으면 추가.

---

## 완료 후
- PR 설명에 각 항목별로 **어떤 파일을 왜 고쳤는지 3줄 이내 요약** 부탁드립니다.
- #5(마이그레이션)와 #8(세션 정책)은 판단이 필요한 항목이라, 확실하지 않으면 코드 대신 **분석 결과만 PR 설명에 남기고 코드는 건드리지 마세요.**
