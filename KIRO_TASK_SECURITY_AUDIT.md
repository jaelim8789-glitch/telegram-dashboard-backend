# 보안 담당 작업 지시서 — 자격증명/시크릿 감사

## 배경

2026-07-28 감사(OpenCode/DeepSeek V4 Flash)에서 TeleMon-kiro, TeleMon-preview-purple,
telegram-dashboard-backend 세 저장소에 걸쳐 평문 자격증명이 다수 발견됐고, 2026-07-30에
`e2e/`, `scripts/smoke_test_prod.py`, `test_verify.py`, `verify_all.py` 등 7개 파일의
실제 운영 admin 비밀번호(`sksk2929`/`qpqpqp10!!`/`ysjr0508`)를 하드코딩에서
`PROD_ADMIN_USERNAME`/`PROD_ADMIN_PASSWORD` 환경변수로 교체 완료했습니다.

이번 작업은 그 이후 새로 추가된 코드까지 포함해서 **범위를 넓혀 다시 감사**하고,
지난번에 "검토 안 함"으로 남겨둔 항목을 마무리하는 것입니다.

## 작업 범위 (이것만 하세요)

1. **저장소 전체 시크릿 재스캔** — `telegram-dashboard-backend`와 `TeleMon-kiro` 양쪽
   모두. gitleaks(이미 pre-commit 훅에 설치되어 있음)를 저장소 전체 히스토리가 아니라
   **현재 워킹트리 기준**으로 돌리고, 추가로 아래 패턴을 수동 grep으로도 확인:
   - 하드코딩된 비밀번호/API 키/토큰 문자열 (`password = "..."`, `api_key = "..."` 등)
   - 커밋된 `.env`, `.env.local`, `.env.production` 파일
   - CI/배포 스크립트(`docker-compose.yml`, `*.sh`, GitHub Actions workflow)에 박힌 자격증명

2. **지난번에 "검토 안 함"으로 남긴 4개 파일 마무리 확인**:
   - `tests/test_nowpayments.py`
   - `tests/test_sms_service.py`
   - `tests/test_cryptomus_payments.py`
   - `TeleMon-preview-purple/backend/tests/test_service.py`
   1차 확인 결과 이 4개는 `"test_secret"`, `"test_api_key"` 같은 가짜 플레이스홀더로 보이지만,
   직접 열어서 진짜 운영 키가 섞여 있지 않은지 확인하고, 맞으면 그대로 두고 보고서에
   "확인함, 문제없음"이라고 남기세요. 진짜 시크릿이 섞여 있으면 그 부분만 고치세요.

3. **`TeleMon-preview-purple/.env` 커밋 여부** — 지난 감사에서 "재확인 안 함"으로
   남은 항목입니다. `git log --all -- TeleMon-preview-purple/.env` 로 커밋된 적 있는지
   확인하고 보고서에 명시하세요.

4. 발견한 진짜(플레이스홀더 아닌) 시크릿은 전부 환경변수로 옮기고, 코드에는 `os.environ`/
   `process.env`로 읽도록 수정하세요. 이미 있는 `PROD_ADMIN_USERNAME`/`PROD_ADMIN_PASSWORD`
   패턴을 참고하세요.

## 절대 하지 마세요 (명시적 금지)

- **git 히스토리 재작성 금지** (`filter-branch`, BFG 등). 이미 과거 커밋에 노출된 실제
  운영 admin 비밀번호는 히스토리에 남아있는 게 맞고, 이건 사용자가 실제 비밀번호를
  로테이션한 *이후에* 별도로 명시적 승인을 받아 처리할 사안입니다. 이번 작업 범위 아님.
- **실제 운영 자격증명(admin 비밀번호, API 키 등)을 직접 로테이션하거나 변경하지 마세요.**
  이건 사람이 VPS/서비스 콘솔에서 직접 해야 하는 일입니다. 코드에서 그 값을 참조하는
  방식을 고치는 것까지만이 작업 범위입니다.
- 프로덕션 DB에 직접 접속해서 데이터를 조회/수정하지 마세요.
- `.env` 파일 자체의 실제 값(현재 운영 키)을 로그, 커밋 메시지, PR 설명에 옮겨 적지 마세요.

## 작업 방식

1. `telegram-dashboard-backend`에서 `security/credential-audit-YYYYMMDD` 형식으로
   본인 브랜치를 만드세요 (다른 세션과 겹치지 않게).
2. 위 범위대로 감사하고 필요한 코드 수정을 하세요.
3. 커밋 메시지는 무엇을 찾았고 왜 고쳤는지 명확히 남기세요 (실제 값은 절대 포함 금지).
4. 작업 끝나면 **PR을 생성하고, 본인이 직접 머지까지 진행**하세요 (이번 작업은 머지 권한
   부여됨 — 이전 Top-10 작업과 다름).
5. 머지 후, 아래 형식으로 결과 보고서를 남기세요:
   - 새로 발견해서 고친 항목 (파일명 + 무엇을 바꿨는지, 실제 값 제외)
   - 확인했지만 문제없었던 항목 (위 4개 테스트 파일 등)
   - **사람이 직접 해야 할 일** (실제 비밀번호 로테이션 여부, 히스토리 재작성 여부 등)
     — 이 항목은 코드로 처리 불가능하니 체크리스트 형태로 명확히 남기세요.

## 완료 기준

- 새로 발견된 진짜 시크릿이 전부 환경변수로 교체됨
- 4개 테스트 파일 + `.env` 커밋 여부 확인 완료
- PR 머지 완료
- 사람이 해야 할 일 체크리스트가 보고서에 포함됨
