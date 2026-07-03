# Telegram Management Dashboard

개인 학습·연구 목적의 Telegram 계정/발송 관리 대시보드입니다. 소수(1~3개)의 본인 소유 계정과 채널을
관리하는 용도로 설계했고, Telegram의 스팸/어뷰징 정책을 지키기 위해 발송 속도를 의도적으로 제한합니다
(계정당 최대 10명 · 1분에 1회). 관리자 로그인(JWT) + API 키 발급 시스템으로 보호되어 있어, 로그인하지
않으면 대시보드도 API도 접근할 수 없습니다.

프론트엔드와 백엔드가 별도 저장소로 나뉘어 있습니다:

- `telegram-dashboard/` — Next.js 프론트엔드 (이 저장소와 형제 디렉터리)
- `telegram-dashboard-backend/` — FastAPI 백엔드, Docker Compose, 배포 관련 파일 전부 (이 저장소)

## 주요 기능

- **관리자 인증**: 고정 관리자 계정(JWT 로그인) + 외부 연동용 API 키 발급/조회/삭제. `/api/*`는 관리자
  세션 또는 유효한 API 키가 있어야 호출 가능
- **계정 관리**: 전화번호 등록 → Telegram 인증(인증번호/2단계 비밀번호) → 세션 암호화 저장
- **그룹/채널 조회**: 인증된 계정이 속한 그룹·채널 목록을 Telethon으로 실시간 조회
- **발송**: 최대 10명에게 메시지(+이미지) 발송, 즉시 또는 예약, 계정당 1분에 1회 제한
- **예약 발송**: 지정 시각에 자동 발송 (APScheduler가 이 서버 프로세스 안에서 30초 주기로 확인·디스패치)
- **발송 로그**: 계정/상태별 필터링, 진행 중인 작업은 화면에서 자동 갱신(폴링)
- **자동 응답(FAQ 매크로)**: 방/DM으로 들어오는 메시지에 키워드가 포함되면 미리 등록한 답변을 자동
  전송. 계정 단위 On/Off + 규칙별 활성화, 같은 사용자에게는 규칙별 쿨다운(기본 1시간) 안에 다시
  응답하지 않고, 규칙별 일일 최대 응답 횟수(기본 100회)를 넘으면 그날은 멈춤. 모든 시도(성공/실패/제한됨)를
  로그로 남김. **로컬/VM 상시 구동 환경 전용** — 자세한 이유는 아래 "자동 응답(Auto-Reply)" 섹션 참고
- **구조화된 로깅**: 모든 주요 이벤트(계정 생성/삭제, 인증, 발송, 레이트리밋, 자동 응답)를 JSON으로
  `logs/app.jsonl`에 기록

## 기술 스택

**백엔드**
- FastAPI, SQLAlchemy 2.x(async) + asyncpg, Alembic
- Telethon (Telegram MTProto 클라이언트)
- PostgreSQL
- APScheduler (예약 발송 디스패처, FastAPI 프로세스 안에서 백그라운드로 실행)
- structlog (JSON 구조화 로깅)
- cryptography(Fernet) — 세션 데이터 암호화
- PyJWT — 관리자 로그인 세션
- python-telegram-bot — `/autoreply` 원격 제어용 봇 (선택 사항, 없어도 대시보드 토글로 충분)
- pytest, pytest-asyncio, pytest-cov, httpx

**프론트엔드**
- Next.js 15(App Router), React 18, TypeScript, TailwindCSS, Zustand
- Playwright (E2E)

**인프라**
- Docker / Docker Compose, nginx(리버스 프록시, 로컬 전체 스택용)

## 아키텍처

발송 처리에 별도 워커/큐(Redis 등)가 없습니다 — 즉시 발송은 FastAPI `BackgroundTasks`로, 예약 발송은
같은 프로세스 안의 스케줄러가 직접 처리합니다. Render 같은 무료 호스팅이 상시 백그라운드 워커를
무료로 제공하지 않는 걸 감안한 설계입니다 (아래 "배포" 섹션 참고). 단, 자동 응답(Auto-Reply) 기능만은
예외입니다 — 들어오는 메시지를 실시간으로 받아야 해서 Telethon 세션이 계속 연결되어 있어야 하고,
이 때문에 그 기능만큼은 sleep-on-idle 호스팅에서 신뢰성 있게 동작하지 않습니다 (아래 "자동 응답"
섹션 참고).

```
                      ┌────────────────────────────┐
   브라우저 ───80──▶  │           nginx             │   (로컬 docker-compose 전용;
                      │  /        → frontend:3000   │    Render+Vercel 배포에서는 안 씀)
                      │  /api/*   → backend:8000     │
                      │  /docs 등 → backend:8000     │
                      └───────────┬─────────────┬────┘
                                  │             │
                      ┌───────────▼───┐   ┌─────▼───────────┐
                      │   frontend    │   │     backend      │
                      │  (Next.js)    │   │ (FastAPI + 예약   │
                      │               │   │  발송 스케줄러)    │
                      └───────────────┘   └────────┬─────────┘
                                                    │
                                              ┌─────▼─────┐
                                              │ postgres  │
                                              └───────────┘
```

## 설치 방법

### 방법 A: Docker Compose (로컬에서 전체 스택 한 번에)

```bash
git clone <이 저장소>
cd telegram-dashboard-backend
cp .env.example .env   # ENCRYPTION_KEY, TELEGRAM_API_ID/HASH, ADMIN_* 채워넣기 (아래 "환경 변수" 참고)

docker compose build
docker compose up -d
docker compose exec backend alembic upgrade head   # 최초 1회, 테이블 생성
```

`http://localhost` 접속하면 로그인 화면이 보입니다. `frontend`/`backend` 컨테이너는 호스트 포트를 열지
않습니다(포트 3000/8000이 다른 로컬 프로젝트와 자주 겹치는 걸 감안한 설계) — nginx(포트 80)가 유일한
진입점입니다. `backend`는 예외적으로 `8000:8000`도 열려 있어 `/docs` 등에 직접 접근할 수 있습니다.

### 방법 B: 로컬 개발 (백엔드는 venv, 프론트는 npm)

DB만 Docker로 띄우고 나머지는 직접 실행 — 코드 변경 후 재빌드 없이 바로 반영되어 반복 개발이 빠릅니다.

```bash
# 1) DB만 컨테이너로
cd telegram-dashboard-backend
docker compose up -d db

# 2) 백엔드
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements-dev.txt
cp .env.example .env              # 값 채우기
alembic upgrade head
uvicorn app.main:app --reload --port 8000

# 3) 프론트엔드 (별도 터미널)
cd ../telegram-dashboard
npm install
npm run dev
```

## 실행 방법 요약

| 하고 싶은 것 | 명령 |
|---|---|
| 전체 스택 실행 | `docker compose up -d` |
| 전체 스택 중지 | `docker compose down` (데이터 유지) / `docker compose down -v` (볼륨까지 삭제) |
| 로그 보기 | `docker compose logs -f backend` |
| 마이그레이션 적용 | `docker compose exec backend alembic upgrade head` |
| 백엔드 테스트 | 아래 "테스트" 참고 |

## 환경 변수 (`.env`)

`.env.example` 참고. Docker Compose로 실행할 때 `DATABASE_URL`은 `backend` 서비스 정의에서 컨테이너
네트워크용 값(`db` 호스트명)으로 자동 덮어써지므로, `.env`의 값은 "로컬에서 venv로 직접 띄울 때" 기준
(`localhost`)으로 두면 됩니다.

| 변수 | 설명 |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` 형식이 아니어도(`postgres://`, `postgresql://`) 앱이 자동으로 asyncpg 드라이버를 붙여줍니다 — Render 등 호스팅이 주는 연결 문자열을 그대로 붙여넣어도 됨 |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | [my.telegram.org](https://my.telegram.org) → API Development Tools에서 발급. 앱 하나로 관리하는 모든 개인 계정 로그인에 재사용 (계정별로 따로 발급받지 않음) |
| `ENCRYPTION_KEY` | Telegram 세션을 암호화해 저장할 때 쓰는 Fernet 키. `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`로 생성. 분실 시 인증된 계정 전부 재인증 필요 |
| `CORS_ORIGINS` | 브라우저에서 API를 직접 호출할 origin 목록. Vercel+Render처럼 프론트/백엔드가 다른 도메인이면 **필수** — Vercel 배포 URL을 넣어야 함 |
| `ENVIRONMENT` | 표시용 (`/health` 응답에 포함) |
| `DEBUG` | `true`면 `/docs`, `/redoc`, `/openapi.json` 노출. 세션 데이터를 다루는 앱이라 실제 배포에서는 `false` 권장 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 관리자 로그인 계정. 배포 전 반드시 기본값에서 변경 |
| `ADMIN_JWT_SECRET` | 로그인 세션 JWT 서명 키. `python -c "import secrets; print(secrets.token_urlsafe(48))"`로 생성 |
| `ADMIN_JWT_EXPIRE_MINUTES` | 로그인 세션 유지 시간(분). 기본 1440(24시간) |
| `TELEGRAM_BOT_TOKEN` | 선택 사항. `/autoreply` 원격 제어 봇용 BotFather 토큰. 비워두면 봇 없이 대시보드 "자동 응답" 탭 토글만 사용 |

## 자동 응답(Auto-Reply)

방/DM으로 들어오는 메시지에 등록해둔 키워드가 포함되면 자동으로 답장하는 FAQ 매크로 기능입니다.
개인 채널 고객 응대 자동화가 목적이며, 대량 발송·스팸과는 무관합니다 (발송 기능과는 완전히 별개의
경로로 동작하고, 같은 발송 레이트리밋에 영향을 주지 않습니다).

**중요한 아키텍처 제약**: 이 기능은 Telethon 세션이 항상 연결된 상태로 들어오는 메시지를 실시간으로
수신해야 동작합니다(`events.NewMessage` 리스너). 발송·예약 발송과 달리 "요청이 올 때만 깨어나면"
되는 구조가 아니라서, **15분 유휴 시 잠드는 무료 호스팅(Render 무료 티어 등)에서는 잠든 동안 들어온
메시지에 응답할 수 없습니다.** 이 기능을 쓰려면 아래 "배포 방법"의 옵션 B(VM에 Docker Compose,
상시 구동)로 배포하거나, 로컬에서 `uvicorn`/`docker compose`를 계속 켜두세요. 옵션 A(Vercel+Render
무료)로 배포한 경우에도 나머지 기능(계정 관리, 발송, 예약 발송)은 정상 동작하며, 자동 응답 탭 자체도
뜨지만 서버가 깨어 있는 동안만 실제로 응답합니다.

**사용 방법**:
1. 대시보드에서 계정을 인증 완료 (Telegram 로그인)
2. "자동 응답" 탭에서 규칙 추가: 이름, 조건(키워드 포함 / 정확히 일치), 매칭 값, 응답 내용, 쿨다운
   시간, 일일 최대 응답 횟수 입력
3. 상단의 마스터 스위치를 켜면 그 계정의 Telethon 세션에 리스너가 붙어 실시간 감시를 시작합니다
   (계정이 아직 인증되지 않았다면 400 에러로 안내)
4. 규칙별로도 개별 중지/재개 가능 — 마스터 스위치가 꺼져 있으면 규칙이 켜져 있어도 응답하지 않습니다

**BotFather 봇으로 원격 제어 (선택)**: 대시보드에 접속하지 않고 Telegram 안에서 바로 계정별 자동
응답을 켜고 끄고 싶다면:
1. Telegram에서 [@BotFather](https://t.me/BotFather)에게 `/newbot`으로 새 봇 생성, 발급받은 토큰을
   `.env`의 `TELEGRAM_BOT_TOKEN`에 설정 후 서버 재시작
2. 그 봇과 1:1 대화(또는 봇을 초대한 아무 채팅)에서 `/autoreply` 입력
3. 등록된 계정마다 "🔴 켜기 / ⚫ 끄기" 버튼이 있는 상태 메시지가 옴 — 버튼을 누르면 해당 계정의
   자동 응답 마스터 스위치가 토글되고 메시지가 갱신됨 (원본 스펙의 "버튼 하나로 이 방의 자동 응답을
   토글"과 달리, 계정이 여러 개일 수 있어 계정별 버튼으로 구성했습니다 — 방↔계정 매핑 정보가 없어도
   동작하도록 하기 위한 설계입니다)

## API 문서

`DEBUG=true`일 때 `http://localhost/docs` (Swagger UI) 또는 `http://localhost/redoc`에서 확인할 수
있습니다. `DEBUG=false`면 보안상 비활성화됩니다.

## 테스트

### 백엔드 (pytest)

Postgres에 테스트 전용 DB가 하나 더 필요합니다 (기존 데이터와 완전히 분리):

```bash
docker compose exec db psql -U telegram_dashboard -d telegram_dashboard -c "CREATE DATABASE telegram_dashboard_test;"

.venv\Scripts\python.exe -m pytest tests/ --cov=app --cov-report=term-missing
```

Telethon 의존성은 전부 모의 객체로 대체되어 있어 실제 Telegram 계정/자격증명 없이 돌아갑니다.

### E2E (Playwright)

실제 백엔드 + Postgres가 떠 있어야 하는 통합 테스트입니다 (프론트엔드만으로는 부족합니다).

```bash
cd ../telegram-dashboard
npx playwright install chromium   # 최초 1회
# ADMIN_USERNAME/PASSWORD를 .env 기본값에서 바꿨다면 여기도 맞춰서 넘겨야 함:
E2E_ADMIN_USERNAME=<.env의 ADMIN_USERNAME> E2E_ADMIN_PASSWORD=<.env의 ADMIN_PASSWORD> npm run test:e2e
```

기본 baseURL은 `http://localhost` (docker compose 전체 스택 기준)이고, `PLAYWRIGHT_BASE_URL`
환경변수로 다른 주소(예: 로컬 개발 중인 `http://localhost:3002`)를 지정할 수 있습니다.

**알려진 한계**: 실제 Telegram 인증번호 수신·2단계 인증·실제 메시지 발송은 진짜 전화번호와
`TELEGRAM_API_ID`/`HASH`가 있어야 확인할 수 있어 자동 E2E 범위 밖입니다. `e2e/accounts.spec.ts`와
`e2e/broadcast.spec.ts`는 그 앞 단계까지(등록, 인증 요청 실패/재시도 처리, 예약·즉시 발송의 데이터
흐름과 UI 표시)를 검증하고, 실제 인증 완료는 계정을 하나 준비해 수동으로 확인하는 걸 권장합니다.

## 배포 방법

### 옵션 A — Vercel(프론트) + Render(백엔드), 카드 없이 시작 가능

가장 저렴하게(워커/카드 없이) 시작하는 조합입니다. 대신 알아두어야 할 무료 티어 한계가 있습니다:

- Render 무료 웹 서비스는 15분 미사용 시 잠들고, 다음 요청에서 콜드스타트(수십 초)가 걸립니다. **잠든
  동안은 예약 발송 스케줄러도 같이 멈춥니다** — "즉시 발송"은 요청이 들어올 때 깨어나서 처리되니
  문제없지만, "예약 발송"은 예정 시각에 서버가 마침 깨어 있어야 나갑니다.
- Render 무료 플랜은 영구 디스크가 없습니다 — 발송에 첨부한 이미지(`media/`)는 재배포/재시작 시
  사라집니다. 텍스트 발송에는 영향 없습니다.
- Render 무료 Postgres는 일정 기간 후 만료/삭제될 수 있습니다 — 배포 전에 Render의 최신 요금제
  페이지에서 정확한 조건을 확인하세요.
- **자동 응답(Auto-Reply) 기능은 서버가 잠든 동안 동작하지 않습니다** — 실시간 리스너가 필요한
  기능이라 sleep-on-idle 호스팅과는 근본적으로 안 맞습니다. 이 기능이 꼭 필요하면 옵션 B를 쓰세요.
  나머지 기능(계정 관리/발송/예약 발송)은 옵션 A에서도 정상 동작합니다.

**1) 백엔드를 Render에 배포**

1. 이 저장소(`telegram-dashboard-backend`)를 GitHub에 푸시
2. Render 대시보드 → New → Blueprint → 방금 푸시한 저장소 선택. 저장소 루트의 `render.yaml`을
   Render가 읽어서 웹 서비스 + Postgres를 같이 만듭니다
3. Render가 `sync: false`로 표시된 값(`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `ENCRYPTION_KEY`,
   `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `CORS_ORIGINS`)을 입력하라고 물어봅니다 — `CORS_ORIGINS`는
   일단 비워두거나 아무 값이나 넣고, 2단계에서 Vercel URL을 받은 뒤 다시 채워넣으세요
4. 배포가 끝나면 최초 1회 마이그레이션 실행: Render 대시보드의 backend 서비스 → Shell 탭에서
   ```bash
   alembic upgrade head
   ```
5. 서비스 URL(예: `https://telegram-dashboard-backend.onrender.com`)을 기록해둡니다

**2) 프론트엔드를 Vercel에 배포**

1. `telegram-dashboard` 저장소를 GitHub에 푸시
2. Vercel 대시보드 → Add New → Project → 방금 푸시한 저장소 선택 (Next.js는 자동 인식되어 별도 설정
   불필요)
3. 배포 전에 Environment Variables에 추가:
   - `NEXT_PUBLIC_API_BASE_URL` = 1단계에서 기록한 Render 백엔드 URL
4. Deploy. 끝나면 Vercel이 URL(예: `https://your-app.vercel.app`)을 줍니다

**3) 백엔드로 돌아가서 CORS 마무리**

Render 대시보드 → backend 서비스 → Environment → `CORS_ORIGINS`를 Vercel URL로 설정 (예:
`https://your-app.vercel.app`) → 저장하면 자동 재배포됩니다.

이제 Vercel URL로 접속하면 로그인 화면이 뜨고, `.env`에 설정한 관리자 계정으로 로그인하면 됩니다.

### 옵션 B — 단일 VM(Oracle Cloud 무료 티어 등)에 Docker Compose 그대로

카드 등록이 가능하다면 이쪽이 더 확실합니다 — 워커가 없어졌으니 리소스 요구량도 더 낮아졌습니다.

1. VM에 Docker/Docker Compose 설치
2. 이 저장소와 `telegram-dashboard` 저장소를 같은 부모 디렉터리 아래 클론 (frontend 서비스가
   `../telegram-dashboard`를 빌드 컨텍스트로 참조합니다)
3. `.env` 작성 (`DEBUG=false`, `ENVIRONMENT=production`, 실제 `TELEGRAM_API_ID`/`HASH`/`ENCRYPTION_KEY`/`ADMIN_*`)
4. `docker compose up -d --build`
5. `docker compose exec backend alembic upgrade head`
6. 방화벽에서 80번 포트만 외부에 열고 5432/8000은 막는 걸 권장 (지금 compose 파일은 로컬 개발
   편의상 5432/8000도 호스트에 노출합니다 — 운영 환경에서는 `docker-compose.yml`에서 해당
   `ports:` 항목을 제거하거나 방화벽 규칙으로 막으세요)
7. nginx가 평문 HTTP만 다루므로, 실제 도메인에 배포한다면 앞단에 Let's Encrypt 등 TLS 종료 계층을
   추가하는 걸 권장합니다 (이 저장소 범위 밖)

## 프로젝트 구조 (백엔드)

```
app/
├── main.py                    # FastAPI 앱, 미들웨어, 라이프스팬(스케줄러 시작/종료)
├── config.py                  # 환경변수 기반 설정 (DATABASE_URL 스킴 자동 정규화 포함)
├── database.py                # SQLAlchemy 엔진/세션
├── core/
│   ├── crypto.py               # Fernet 암복호화
│   ├── limits.py                # 발송 제한 상수
│   ├── logging.py               # structlog 설정
│   └── security.py              # 관리자 JWT 발급/검증, API 키 생성
├── models/                     # SQLAlchemy 모델 (Account, Broadcast, APIKey, AutoReplyRule/Log)
├── schemas/                    # Pydantic 스키마
├── crud/                       # DB 접근 계층
├── api/                        # 라우터 (admin, accounts, telegram_auth, groups, broadcast, logs, scheduler, auto_reply)
│   └── deps.py                  # 인증 의존성 (관리자 JWT 또는 X-API-Key)
├── services/                   # Telethon 연동, 발송 처리(broadcast_processor), 자동 응답 리스너
│                                 # (auto_reply_service), 원격 제어 봇(telegram_bot_service), 미디어 저장
└── scheduler/                   # APScheduler 디스패처 (예약 발송)
alembic/                       # DB 마이그레이션
tests/                         # pytest
nginx/                         # 리버스 프록시 설정 (로컬 docker-compose 전용)
render.yaml                    # Render Blueprint (백엔드 + Postgres 배포용)
```
