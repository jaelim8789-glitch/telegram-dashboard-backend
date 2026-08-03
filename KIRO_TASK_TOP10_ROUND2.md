# 작업 지시서 — 라운드 2 (발견된 심각 버그 10개)

## 작업 방식

1. `git checkout master && git pull origin master && git checkout -b fix/round2-critical-bugs`
2. 항목별 커밋 분리
3. **이번엔 다 끝나면 PR 만들고 나서, CI/린트/타입체크 통과 확인 후 직접 merge까지 해주세요.** (master로 squash merge or merge commit, 브랜치 삭제까지)
   ```
   gh pr create --base master --title "fix: 심각 버그 10개 수정" --body "..."
   # CI 통과 확인 후
   gh pr merge --merge --delete-branch
   ```
4. merge 전에 반드시: `npx tsc --noEmit` (프론트), `python -c "import ast; ..."` 문법체크(백엔드) 통과 확인
5. #1, #2는 실제 결제/매출에 직결되는 부분이라 신중하게 — 고치고 나서 로그로 정상 동작(TronGrid 정상 응답) 확인까지 하고 merge하세요

---

## 항목

### #1. USDT 자동결제 감지가 사실상 완전히 죽어있음 (최우선, 매출 직결)
- `.env`의 `USDT_WALLET_ADDRESS`가 플레이스홀더 값(`0x0000000000000000000000000000000000000000`)으로 설정되어 있음.
- `app/services/usdt_watcher.py`가 5분마다 이 잘못된 주소로 TronGrid API를 호출 → 429 rate limit 반복 (`docker logs`에서 `trongrid_api_error` 확인됨).
- **결과: 배포 이후 USDT로 결제한 고객이 있었다면 단 한 건도 자동으로 활성화되지 않았을 가능성이 높습니다.**
- 할 일: 실제 운영 지갑 주소를 `.env`에 설정하고, 재배포 후 로그에서 `trongrid_api_error`가 사라지고 정상 응답 오는지 확인.

### #2. 지갑 주소 미설정 가드가 플레이스홀더를 못 잡아냄
- `usdt_watcher.py:68`의 `if not USDT_WALLET_ADDRESS:` 체크는 빈 문자열만 걸러내고, `"0x000...0"` 같은 placeholder 값은 "설정된 값"으로 취급해 그대로 진행됨.
- 할 일: 빈 문자열 OR 모두-0 주소 패턴을 함께 체크하도록 가드 보강. `USDT_WALLET_ADDRESS`가 유효하지 않으면 스케줄러 job 자체를 건너뛰고 warning 로그 남기기.

### #3. draft_routes.py 배치 승인 SQL 자체가 문법적으로 깨져있음
- `app/routers/draft_routes.py` 369-374줄: `placeholders = ",".join([""] * len(draft_ids))`가 `?,?,?` 대신 빈 문자열(`,,`)을 만들어서, `WHERE id IN (,,) AND user_id = ` 형태의 잘못된 SQL이 생성됨. 이 엔드포인트(배치 승인)는 호출될 때마다 예외가 날 가능성이 높음.
- 같은 패턴이 427줄, 463줄에도 반복됨 (배치 반려/삭제).
- 할 일: `["?"] * len(draft_ids)` 로 수정하고, 실제로 호출해서 정상 동작하는지 테스트.

### #4. AI 워크플로우 엔진의 LLM 호출 단계가 스텁 상태
- `app/ai/workflow/executor.py:167`: 실제 LLM을 호출하지 않고 `"response": "[LLM call placeholder]"` 고정 문자열만 반환.
- 자동화 스튜디오(관제실)에서 AI 액션이 포함된 워크플로우를 실행하면 실제로는 아무 AI 응답도 생성되지 않고 저 문자열만 나옵니다.
- 할 일: 실제 AI 서비스(`ai_core_service.py` 등 기존 LLM 호출 로직) 연결.

### #5. AI 스케줄러 조건 평가가 항상 True로 하드코딩
- `app/ai/scheduler/service.py:249`: `should_run = True  # Placeholder` — 실제 스케줄 조건(시간/이벤트 조건 등)을 평가하지 않고 항상 실행 처리됨.
- 할 일: 실제 조건 평가 로직 구현 또는 최소한 조건이 없을 때만 True가 되도록 수정.

### #6. 봇 명령어에서 브로드캐스트 생성이 미구현 (TODO로 남음)
- `app/bot/service.py:346`: `# TODO: insert into broadcast table via API` — 해당 봇 명령을 받으면 브로드캐스트가 생성돼야 하는데 실제로 DB에 insert하는 코드가 없음.
- 할 일: 실제 broadcast 생성 API 호출 연결, 혹은 이 명령어가 현재 사용되지 않는 게 확실하면 안내 메시지로 대체.

### #7. 프로덕션 에러 알림이 아무 데도 안 감
- `ALERT_WEBHOOK_URL` 환경변수가 비어있어서, `app/monitoring.py`의 에러 알림이 전부 `logger.warning("Alert not sent...")`로만 남고 실제 알림(Slack/Discord webhook 등)은 발송 안 됨.
- 할 일: Slack이든 뭐든 webhook URL 하나 설정해서 최소한 500 에러가 나면 알림 오도록. (설정만 하면 되는 부분, 코드 변경 불필요할 수 있음 — 없으면 webhook 발송 코드가 실제로 동작하는지부터 확인)

### #8. `except Exception:` 패턴이 API 레이어에 29곳 — 에러가 조용히 삼켜질 위험
- `app/api/*.py` 전반에 걸쳐 무분별한 bare exception catch가 많음. 전부 고칠 필요는 없지만, 결제/인증/계정 관련 파일만이라도 훑어서 실제 에러를 삼키고 있는 곳(로그도 안 남기고 그냥 pass)이 있는지 확인.
- 할 일: `app/api/` 디렉토리에서 결제(`usdt_payment.py`, `billing`), 인증(`auth.py`) 관련 파일의 bare except만 우선 점검. 로그 없이 넘어가는 곳엔 최소 `logger.warning`이라도 추가.

### #9. billing.py와 usdt_watcher.py가 지갑 주소를 서로 다른 방식으로 읽음
- `billing.py`는 `os.getenv("USDT_WALLET_ADDRESS", ...)`로 직접 읽고, `usdt_watcher.py`는 `settings.usdt_wallet_address`(pydantic Settings)로 읽음. 지금은 같은 env var라 우연히 일치하지만, 설정 방식이 이원화되어 있어 나중에 둘이 어긋날 위험이 있음.
- 할 일: 한쪽으로 통일 (pydantic settings 경유 권장).

### #10. (분석 필요) 정기결제/구독 갱신 로직 전체 점검
- #1, #2를 고치는 김에, `downgrade_expired_tenants`, `notify_expiring_trials` 등 결제/구독 관련 스케줄러 job들도 같은 패턴(잘못된 설정값을 조용히 통과시키는 가드)이 있는지 훑어봐 주세요.
- 실제 버그가 없으면 "확인함, 문제 없음"으로 PR 설명에 남기고 코드 변경 없이 넘어가도 됩니다.

---

## 완료 후
- PR 설명에 항목별 요약 + **#1 관련해서는 실제 지갑 주소를 뭘로 설정했는지, 정상 동작 로그를 캡처해서 남겨주세요.**
- 전부 확인되면 merge까지 직접 진행해주세요.
