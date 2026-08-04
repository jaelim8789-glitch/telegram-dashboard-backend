# TeleMon — 백엔드 인수인계 문서 (Handover)

> 작성일: 2026-08-04
> 저장소: `C:\Dev\TeleMon-kiro\telegram-dashboard-backend` (개발) / `C:\Dev\telegram-dashboard-backend` (프로덕션)
> GitHub remote: `jaelim8789-glitch/telegram-dashboard-backend`

---

## 1. 프로젝트 개요

**TeleMon**은 Telegram 마케팅/고객관리 대시보드입니다. 사용자(운영자)가 텔레그램 계정 여러 개를 연결해 브로드캐스트, 자동응답, AI 답변, 그룹 관리 등을 수행합니다.

- **프론트엔드**: Next.js 15 (App Router) + TypeScript + Tailwind + zustand + framer-motion
- **백엔드**: FastAPI + PostgreSQL + Redis + APScheduler + Telethon (Telegram MTProto)
- **결제**: NOWPayments (USDT/TON) + Telegram Stars, 별도 USDT flow
- **배포**: Docker Compose + Cloudflare Tunnel (https://app.telemon.online)

---

## 2. 실행/배포 방식

### 개발 (로컬)
```bash
# 백엔드
cd C:\Dev\TeleMon-kiro\telegram-dashboard-backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### 프로덕션 (VPS, Docker)
- `C:\Dev\telegram-dashboard-backend` — 프로덕션 compose (prod override로 시크릿 관리)
- 배포 흐름:
  - 백엔드: 개발 저장소에서 수정 → push → 프로덕션 저장소에서 pull → `docker compose up -d --build`
  - 프론트: 개발 저장소에서 빌드 → 프로덕션에서 `--no-cache build` → `--force-recreate`
- Cloudflare Tunnel → `docker-compose` 내 nginx가 80/443 수신
- **관리자 로그인**: admin / ADMIN2026 (또는 DB bcrypt 계정 — `/api/admin/setup`)

### Docker 네트워크 규칙
- 프론트→백엔드: `http://backend:8000` (localhost 금지)
- `REDIS_URL`은 비밀번호 포함 (`redis://:${REDIS_PASSWORD}@redis:6379/0`)

---

## 3. 백엔드 모듈 지도

### API 라우터 (`app/api/`)
| 파일 | 역할 |
|---|---|
| `auth.py` | 사용자 인증, SMS/OTP, 세션, /me (tenant_id 포함) |
| `accounts.py` | 텔레그램 계정 CRUD, bulk(activate/deactivate/connect/disconnect/delete/reset_session), maintenance, sync-progress |
| `telegram_auth.py` | 멀티스텝 텔레그램 인증 (send-code/verify-code/verify-2fa/status) — **Epic 19 계정 연결 핵심** |
| `chats.py` | 채팅/다이얼로그, 파일 업로드, WS 이벤트 |
| `broadcast.py` | 브로드캐스트 생성/발송 |
| `auto_reply.py` | 자동응답 규칙 |
| `reply_macro.py` | 답장 매크로 |
| `billing.py` | 플랜, USDT 인보이스, Stars, billing summary (Epic 17) |
| `nowpayments.py` | NOWPayments 결제, 웹훅, 상태, 영수증 (PDF) |
| `referral.py` | 추천인 코드/커미션/지급/리더보드 (Epic 26 기반) |
| `admin.py` | 관리자 대시보드/통계/오퍼레이션/인시던트/헬스스코어 |
| `ai*.py` | AI 채팅/코파일럿/에이전트 (ai_chat_v2가 SSE 스트리밍) |
| `scheduler.py` | 스케줄러 상태 (다음 틱/실행중) |
| `push_notifications.py` | Web Push/FCM (외부 push용) |
| `mcp_gateway.py` | MCP Gateway (AI 도구 통합) |

### 서비스 (`app/services/`)
| 파일 | 역할 |
|---|---|
| `session_manager.py` | 텔레그램 세션 연결/모니터/자동복구 (Epic 19·20 핵심) |
| `sync_progress.py` | Redis 기반 동기화 진행률 (TTL 10분) |
| `incident_engine.py` | 인시던트 엔진 — 자동복구/지수백오프/헬스스코어 (Epic 20) |
| `nowpayments.py` | NOWPayments API + 재검증 + reconciliation |
| `billing.py` | 결제/구독 + billing summary |
| `referral.py` | 커미션/지급 로직 |
| `receipt.py` | PDF 영수증 (fpdf2 + Malgun 폰트) |
| `usage_tracker.py` | 월별 사용량 (계정/브로드캐스트/AI) |
| `account_health.py` | 계정 헬스 점수 |
| `cryptomus.py` / `usdt_watcher.py` | USDT 결제 (레거시) |

### 핵심 모델 (`app/models/`)
- `tenant.py` — 사용자(테넌트): plan/subscription/trial/referral/payment_records
- `account.py` — 텔레그램 계정: status/session_data/dialog_count/last_sync_at
- `nowpayments.py` — NOWPayments 트랜잭션 (fulfilled 마커 포함)
- `referral.py` — ReferralCode/Commission/Payout/Config
- `session.py` — 사용자 세션 (device binding 포함)
- `auto_reply.py`, `broadcast.py`, `chat.py`(Conversation/Message), `system_setting.py`

---

## 4. 보안 상태 (Epic 20 완료 기준)

| 영역 | 상태 |
|---|---|
| 비밀번호 | bcrypt + 상시 비교 (`verify_password_stored`) |
| JWT | ADMIN_JWT_SECRET / JWT_USER_SECRET 분리 |
| 세션 | opaque 토큰 + user-agent/IP 해시 바인딩 (`requires_reauth`) |
| WS 인증 | `/ws/*` 토큰 필수 + 테넌트 소유권 검증 (IDOR 차단) |
| 레이트리밋 | Redis 분산 (INCR+EXPIRE) + in-memory fallback |
| 시크릿 | docker-compose에서 제거, env/Docker secret으로 |
| 인시던트 | Redis 장애 감지 → 자동복구(5s/15s/30s) → 알림(ALERT_WEBHOOK_URL) |
| 업로드 | 콘텐츠타입 화이트리스트 + 크기 200MB 제한 + UUID 저장 |
| CI | pip-audit + gitleaks + pytest (Epic 20·23) |

### 운영 런북
- `docs/INCIDENT_CHECKLIST.md` — 장애 대응 절차 (DB/Redis/스케줄러/큐/텔레그램)
- `scripts/dr_recovery.sh` — 수동 DR 복구
- `scripts/security_check.py` — 배포 전 보안 점검

---

## 5. 스케일 검증 (Epic 23 — 인프라 준비됨, 실행은 베타 직전)

```bash
python scripts/seed_scale.py accounts=5 conversations=100 messages=10000
python scripts/scale_test.py --base http://<staging>:8000 --login-token TOKEN --duration 21600 --out baseline.json
python scripts/chaos_test.py --compose-dir /opt/telemon/backend --base http://<staging>:8000 --token TOKEN
```
- 지표: P50/95/99/Max (monitoring.py), 시스템 포화(psutil), WS 지연, 스케줄러 지연, 성공률
- **실행은 VPS 스테이징에서만** (프로덕션 금지)

---

## 6. 테스트

```bash
python -m pytest tests/ -q -n 0
```
- 핵심 테스트: `test_sync_progress`, `test_incident_engine`, `test_admin_operations_overview`, `test_admin_key_rotation`, `test_ws_tenant_isolation`, `test_receipt`
- **기존 실패 주의**: master에 이미 몇몇 실패가 있음 (예: `test_api_telegram_auth` 일부, `test_billing_entitlements` 5개) — 베이스에서도 동일, 신규 회귀와 구분 필요

---

## 7. 주요 환경변수

| 변수 | 용도 |
|---|---|
| `DATABASE_URL` | Postgres 연결 |
| `REDIS_URL` | Redis (비밀번호 포함) |
| `REDIS_PASSWORD` | Redis 인증 |
| `ADMIN_JWT_SECRET` | 관리자 JWT 시크릿 (기본값 사용 금지) |
| `JWT_USER_SECRET` | 사용자 JWT 시크릿 (ADMIN과 분리 필수) |
| `NOWPAYMENTS_API_KEY` / `NOWPAYMENTS_IPN_SECRET` | NOWPayments |
| `API_BASE_URL` | 공개 백엔드 URL (NOWPayments 웹훅 도달) |
| `ALERT_WEBHOOK_URL` | 인시던트 알림 webhook (Slack/Discord/Telegram) |
| `ENCRYPTION_KEY` | 텔레그램 세션 암호화 |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | Telethon |

---

## 8. 알려진 이슈 / 다음 단계

- **Epic 26 (Growth)**: Referral은 완성, **Coupon/Promo/Affiliate 모델 없음 + 봇 `start=ref_` 딥링크 미처리** (초대 귀속 안 됨) → 신규 구현 대상
- **Scale Validation 실행** 연기됨 (베타 직전)
- **Mobile UX** (Epic 21 로드맵의 Mobile 경험) 진행 여지 있음
- `test_billing_entitlements` 등 기존 실패 정리 필요 (기능과 무관하지만 CI 노이즈)
