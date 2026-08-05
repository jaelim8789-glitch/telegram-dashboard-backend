# TeleMon 전체 분석 보고서

> 분석일: 2025-08-05
> 대상: TeleMon-kiro (프론트엔드) + telegram-dashboard-backend (백엔드)
> 목적: 아키텍처 파악, 코드 품질 이슈, 버그 위험 지점, 보안 취약점 식별

---

## 목차

1. [전체 아키텍처 요약](#1-전체-아키텍처-요약)
2. [코드 품질 이슈](#2-코드-품질-이슈)
3. [버그 위험 지점](#3-버그-위험-지점)
4. [보안 체크](#4-보안-체크)
5. [개선 우선순위 제안](#5-개선-우선순위-제안)

---

## 1. 전체 아키텍처 요약

### 1.1 시스템 개요

```
┌─────────────────────┐         ┌──────────────────────────────┐
│  TeleMon-kiro       │  HTTP   │  telegram-dashboard-backend  │
│  (Next.js 15)       │ ──────> │  (FastAPI + SQLAlchemy)       │
│  Port: 3000         │  /api/* │  Port: 8000                   │
│                     │ <────── │                              │
│  - React 18         │  JSON   │  - PostgreSQL (asyncpg)       │
│  - Zustand          │         │  - Alembic migrations         │
│  - Tailwind CSS     │  WS     │  - APScheduler                │
│                     │ <──────>│  - Telethon (Telegram MTProto)│
│                     │         │  - DeepSeek AI                │
└─────────────────────┘         └──────────────────────────────┘
```

- **통신 방식**: 프론트엔드는 `src/lib/api.ts`의 `request()` 함수를 통해 백엔드에 REST API 호출. WebSocket은 `sessionWebSocket.ts`로 실시간 세션 업데이트 수신. SSE는 AI 채팅 스트리밍에 사용.
- **인증**: `X-Session-Token` (세션 토큰) 또는 `Authorization: Bearer {jwt}` 헤더. 토큰은 `localStorage`에 저장 (쿠키 아님).
- **캐싱**: 프론트엔드에 stale-while-revalidate 캐시 (`requestCached()`, TTL 30초), 별도 대화 메시지 캐시 (TTL 45초).

### 1.2 도메인별 파일 매핑

#### 프론트엔드 (TeleMon-kiro)

| 도메인 | 주요 파일/폴더 |
|--------|---------------|
| **계정 관리** | `src/components/workspace/tabs/AccountRegisterTab.tsx`, `src/components/account/` (7개), `src/lib/api.ts:459-784` |
| **채팅 관리** | `src/components/chat-management/` (4개, 최대 2294줄), `src/hooks/useChatWebSocket.ts`, `src/lib/sessionWebSocket.ts` |
| **자동 응답** | `src/components/workspace/tabs/AutoReplyTab.tsx` (750줄), `src/lib/api.ts:1843-1975` |
| **브로드캐스트/발송** | `src/components/workspace/tabs/SendTab.tsx` (2172줄), `FlowEditorTab.tsx`, `RecurringScheduleTab.tsx`, `LogTab.tsx` |
| **AI 채팅** | `src/components/workspace/tabs/AiChatTab.tsx`, `src/hooks/useAiChatSession.ts`, `src/components/ai-shell/` (12개) |
| **결제/빌링** | `src/app/billing/`, `src/components/billing/` (6개), `src/lib/planLimits.ts`, `src/lib/api.ts:2895-3031` |
| **그룹 관리** | `src/components/workspace/tabs/GroupTab.tsx`, `GroupSearchTab.tsx`, `FoldersTab.tsx`, `LinkInspectorTab.tsx` |
| **대시보드** | `src/components/workspace/tabs/DashboardTab.tsx` (1113줄), `dashboard/` (8개 위젯) |
| **관리자** | `src/app/admin/` (8개 페이지), `src/components/admin/AdminGuard.tsx` |
| **랜딩/마케팅** | `src/components/landing/` (25개), `src/app/(public)/` |
| **에스크로/신뢰** | `src/lib/api.ts:491-611` (API), 백엔드 연동 |
| **Mini App** | `src/app/miniapp/page.tsx` |

#### 백엔드 (telegram-dashboard-backend)

| 도메인 | 주요 파일/폴더 |
|--------|---------------|
| **인증** | `app/api/auth.py`, `app/api/admin.py`, `app/api/deps.py`, `app/models/user.py`, `app/models/session.py` |
| **계정 관리** | `app/api/accounts.py`, `app/api/telegram_auth.py`, `app/crud/account.py`, `app/models/account.py` |
| **AI 채팅/답변** | `app/api/ai.py`, `app/api/ai_chat_v2.py`, `app/api/ai_reply_v2.py`, `app/services/ai_chat_v2_service.py`, `app/services/deepseek_service.py` |
| **자동 응답** | `app/api/auto_reply.py`, `app/services/auto_reply_service.py`, `app/models/auto_reply.py` |
| **브로드캐스트** | `app/api/broadcast.py`, `app/services/broadcast_processor.py`, `app/services/delivery.py` |
| **결제** | `app/api/billing.py`, `app/api/usdt_payment.py`, `app/api/nowpayments.py`, `app/services/nowpayments.py` |
| **에스크로** | `app/api/escrow.py`, `app/models/escrow.py` |
| **그룹/채널** | `app/api/groups.py`, `app/api/group_search.py`, `app/api/channel_hub.py` |
| **AI 플랫폼** | `app/ai/` (routers, tools, task_queue, scheduler, event_bus, plugin) |
| **실시간** | `app/realtime/`, `app/routes/ws.py` |
| **스케줄러** | `app/scheduler/scheduler.py` (APScheduler) |
| **레퍼럴** | `app/api/referral.py`, `app/models/referral.py` |

### 1.3 백엔드 라우터 등록 현황 (main.py 기준)

- **인증 필요 라우터**: 55개 (`dependencies=_auth_required`)
- **공개 라우터**: 16개 (인증 불필요)

---

## 2. 코드 품질 이슈

### 2.1 🔴 치명적 중복: `runtimeManager.ts` ↔ `connectionManager.ts`

**가장 큰 코드 품질 문제.**

- `src/lib/runtimeManager.ts` (559줄)과 `src/lib/connectionManager.ts` (593줄)은 거의 동일한 싱글턴 계정 상태 관리자
- 동일한 `AccountRuntimeCache` 인터페이스, 동일한 `createEmptyCache()`, 동일한 `_fetchAndCacheGroups()`, `_fetchAndCacheBroadcasts()` 등
- **차이점**: `ConnectionManager`는 WebSocket 기반, `RuntimeManager`는 30초 폴링 기반
- **문제**: `SendTab.tsx:23`, `AutoReplyTab.tsx:15`는 `RuntimeManager`를 사용하고, 나머지는 `ConnectionManager`를 사용 → 탭 간 계정 데이터 불일치 가능

### 2.2 거대 파일 (500줄 초과)

| 파일 | 줄 수 | 권장 사항 |
|------|-------|----------|
| `src/lib/api.ts` | **3354** | 도메인별 분리: `api/accounts.ts`, `api/broadcast.ts`, `api/chat.ts` 등 |
| `src/components/chat-management/ChatConversationPanel.tsx` | 2294 | 메시지 렌더링, 입력 영역, 핸들러 분리 |
| `src/components/workspace/tabs/SendTab.tsx` | 2172 | 수신자 선택, 스케줄링, 전달 모드 로직 분리 |
| `app/api/admin.py` (백엔드) | **1470** | 6개 이상 관심사 혼재 (로그인, 사용자관리, API키, 빌링, 대시보드, AI로그) |
| `app/api/auth.py` (백엔드) | 1250+ | 인증 흐름 분리 필요 |
| `app/services/telegram_bot_service.py` | ~1200 | 봇 명령어 핸들러 분리 |

### 2.3 죽은 코드 (사용되지 않는 파일)

| 파일 | 비고 |
|------|------|
| `src/lib/exportData.ts` | 전체 코드베이스에서 임포트 없음 |
| `src/lib/notificationsEnabled.ts` | 임포트 없음 |
| `src/lib/reportWebVitals.ts` | 임포트 없음 |
| `src/lib/sessionState.ts` | 임포트 없음 |
| `src/hooks/useBatchedState.ts` | 임포트 없음 |
| `src/lib/useKeyboardShortcuts.ts` | 임포트 없음 |
| `app/scheduler/scheduler.py:305-339` (백엔드) | `_run_wrapped()` 모니터링 코드 — 실제로 사용되지 않음 |

### 2.4 중복 DeepSeek API 클라이언트 (백엔드)

3개의 별도 HTTP 클라이언트가 DeepSeek API를 호출:

1. `app/services/ai_core_service.py:77-128` — 정규 중앙 클라이언트
2. `app/services/deepseek_service.py:30-55` — 독립 클라이언트 (자체 URL, 키, 모델)
3. `app/services/translation_service.py:59-76` — 또 다른 독립 클라이언트

### 2.5 로깅 불일치 (백엔드)

`app/core/logging.py`에 `get_logger()` 구조화 로거가 존재하나, **26개 파일**이 `import logging` + `logging.getLogger(__name__)`를 사용하여 일관성 없는 로그 출력:

- `app/bot/service.py:18`, `app/bot/guest_engine.py:22`, `app/services/deepseek_service.py:6`, `app/services/session_manager.py:8` 등

### 2.6 `eslint-disable` 억제 (프론트엔드)

**27개**의 `react-hooks/exhaustive-deps` 억제. 가장 많은 곳:

- `ChatManagementTab.tsx`: 12개 (줄 259, 296, 327, 333, 353, 371, 387, 402, 429, 453, 461, 603)
- `SendTab.tsx`: 4개 (줄 728, 797, 889, 911)

### 2.7 빈 `catch {}` 블록 (프론트엔드)

**64개**의 빈 catch 블록. 가장 우려되는 곳:

| 파일 | 줄 | 컨텍스트 |
|------|-----|---------|
| `src/lib/connectionManager.ts` | 343 | `refreshAccounts()` 에러 무시 |
| `src/lib/sessionWebSocket.ts` | 87 | WebSocket 메시지 파싱 실패 무시 |
| `src/components/workspace/tabs/KnowledgeBaseTab.tsx` | 117, 143, 166, 173 | 모든 KB 작업 실패 무시 |

### 2.8 `console.log` 잔존 (프론트엔드)

36개의 `console.log/warn` 호출. 제거 대상:

- `src/lib/runtimeManager.ts:143` — "attempting recovery probe..."
- `src/lib/runtimeManager.ts:146` — "backend recovered -- reinitializing"
- `src/lib/perf/benchmark.ts:95` — 벤치마크 결과

### 2.9 중복 SQLite DB 사용 (백엔드)

- `app/routers/trigger_routes.py:24` — `data/admin.db`에 raw sqlite3 직접 연결
- `app/routers/draft_routes.py:32` — 동일한 `data/admin.db`에 raw sqlite3 직접 연결
- 메인 PostgreSQL DB와 별도 데이터베이스, 동기 블로킹 호출, ORM/migration 없음

### 2.10 일관성 없는 API 경로 패턴 (프론트엔드)

- 대부분: `/api/{resource}` (예: `/api/accounts`)
- Knowledge Base: `/api/v1/kb/{resource}` (버전 접두사, 유일)
- 테넌트 범위: `/api/tenants/{tenantId}/{resource}`
- 채팅: `/api/chat-telegram/accounts/{id}/...` (긴 중첩 경로)

### 2.11 중복 엔드포인트 함수 (프론트엔드)

- `fetchSessionEvents()` (`api.ts:914`)와 `fetchAccountSessionTimeline()` (`api.ts:729`)이 동일한 `/api/accounts/{id}/events` 엔드포인트 호출

---

## 3. 버그 위험 지점

### 3.1 🔴 `NameError` — billing.py (백엔드)

- **위치**: `app/api/billing.py:42`
- **문제**: `result.get("error", "  ")` — `result` 변수가 정의되지 않음. Rate limit 트리거 시 `NameError` → 500 에러 발생

### 3.2 🔴 Knowledge Base 인증 누락 (프론트엔드)

- **위치**: `src/components/workspace/tabs/KnowledgeBaseTab.tsx:113, 115, 128, 138, 159, 171, 180`
- **문제**: `fetch()` 직접 호출 시 `Authorization`/`X-Session-Token` 헤더 없음. 중앙 `request()` 함수 우회. 인증 필요 KB 엔드포인트에서 401 발생 가능
- 같은 문제: `src/components/auth/FloatingAiChat.tsx:89`, `src/hooks/usePredictiveLoading.ts:27,50`

### 3.3 🔴 blockUser/unblockUser 백엔드 미구현 (프론트엔드)

- **위치**: `src/lib/api.ts:1205-1233`
- **문제**: 주석에 "NOT YET IMPLEMENTED on the backend" 명시. `ChatInfoPanel.tsx:204`에서 호출 → 차단 버튼 클릭 시 404 발생

### 3.4 병렬 상태 관자 불일치 (프론트엔드)

- `ConnectionManager` (WebSocket)와 `RuntimeManager` (폴링)이 별도 캐시 유지
- `SendTab`과 `AutoReplyTab`은 `RuntimeManager` 사용, 나머지는 `ConnectionManager` 사용
- 탭 간 그룹/자동응답 데이터 불일치 가능

### 3.5 `streamDialogMessages()` 무한 재연결 (프론트엔드)

- **위치**: `src/lib/api.ts:1280-1366`
- **문제**: 메시지 수신 후 `attempt` 리셋 (줄 1349), 최대 재연결 횟수 초과 시 30초 대기 후 리셋 (줄 1353-1357) → 영구 중단 불가능

### 3.6 Race Condition: 브로드캐스트 전송 (백엔드)

- **위치**: `app/services/broadcast_processor.py:15-23`
- **문제**: 인메모리 `_account_locks` dict로 동시 전송 제어. `uvicorn --workers N` 환경에서 각 워커가 별도 lock 보유 → 같은 브로드캐스트가 여러 워커에서 중복 처리 가능

### 3.7 Race Condition: 자동응답 중복 제거 (백엔드)

- **위치**: `app/services/auto_reply_service.py:22-23`
- **문제**: 인메모리 `_recent_messages` dict로 중복 제거. 멀티 워커 환경에서 같은 메시지 이벤트가 각 워커에서 자동응답 트리거 가능

### 3.8 `bare except:` 절 (백엔드)

- **위치**: `app/services/deepseek_service.py:62`
- **문제**: `except: return None` — `SystemExit`, `KeyboardInterrupt` 포함 모든 예외 무시. `except Exception:`으로 변경 필요

### 3.9 미구현 API 함수 (프론트엔드)

- **위치**: `src/lib/api.ts:1205-1233` (`blockUser`, `unblockUser`)
- 주석에 명시: "NOT YET IMPLEMENTED on the backend"
- `ChatInfoPanel.tsx:204`에서 이미 호출 중 → 404 발생

### 3.10 관리자 설정 무 rate limit (백엔드)

- **위치**: `app/api/admin.py:133-147` (`POST /api/admin/setup`)
- **문제**: 관리자 계정이 없을 때 무제한 요청 가능. 공격자가 합법적 관리자보다 먼저 관리자 계정 생성 가능

---

## 4. 보안 체크

### 4.1 🔴 CRITICAL: `/metrics` 엔드포인트 인증 없음 (백엔드)

- **위치**: `app/main.py:461` (`GET /metrics`), `app/main.py:534` (`GET /api/metrics`)
- **문제**: 내부 앱 메트릭 (요청 수, 레이턴시, 세션 수)을 인증 없이 노출

### 4.2 🔴 CRITICAL: Knowledge Base 문서 목록 인증 없음 (백엔드)

- **위치**: `app/api/knowledge_base.py:47-58` (`GET /api/v1/kb/documents`)
- **문제**: `get_current_identity` 의존성 없음. 미발행 문서 포함 전체 KB 문서 열거 가능

### 4.3 🔴 CRITICAL: 미들웨어 인증 우회 (프론트엔드)

- **위치**: `middleware.ts:17-18`
- **문제**: `return NextResponse.next()` — 완전한 no-op. `/admin/*` 라우트에 서버사이드 인증 없음. `AdminGuard` 클라이언트 사이드에서만 보호

### 4.4 "deepseek-chat" 하드코딩 (13곳)

| # | 파일 | 줄 |
|---|------|-----|
| 1 | `app/config.py` | 120 |
| 2 | `app/ai/config.py` | 26 |
| 3 | `app/ai/config.py` | 80 |
| 4 | `app/services/ai_core_service.py` | 34 |
| 5 | `app/services/deepseek_service.py` | 13 |
| 6 | `app/services/translation_service.py` | 15 |
| 7 | `app/services/ai_chat_v2_service.py` | 396 |
| 8 | `app/services/ai_chat_v2_service.py` | 445 |
| 9 | `app/bot/guest_engine.py` | 45 |
| 10 | `app/api/ai_agent.py` | 748 |
| 11 | `app/models/ai_chat_v2.py` | 24 |
| 12 | `app/models/ai_chat_v2.py` | 71 |
| 13 | `app/models/ai.py` | 36 |

### 4.5 DeepSeek API URL 하드코딩 (백엔드)

- `app/services/deepseek_service.py:12` — `https://api.deepseek.com/chat/completions`
- `app/bot/guest_engine.py:44` — `https://api.deepseek.com/v1/chat/completions`
- `app/config.py:119`에 `deepseek_api_base` 설정이 있지만 우회

### 4.6 인증 없는 AI 비용 엔드포인트

| 라우터 | main.py 줄 | 위험도 |
|--------|-----------|--------|
| `ai_guest_router` | 375 | MEDIUM — IP당 10회/일 제한, 실제 DeepSeek 예산 소비 |
| `demo_router` | 409 | MEDIUM — IP당 8회/60초 제한, 실제 DeepSeek 예산 소비 |
| `translate_router` | 454 | MEDIUM — IP당 20회/60초 제한, 실제 DeepSeek 예산 소비 |
| `miniapp_router` | 452 | MEDIUM — IP당 10회/60초 제한 |

### 4.7 USDT 결제 엔드포인트 인증 없음

- `app/api/usdt_payment.py` — `GET /api/payment/status/{ref}`에서 마스킹된 API 키 노출
- `GET /api/payment/claim-key/{ref}`에서 원본 API 키 반환 (rate limited이지만 낮은 엔트로피의 ref)

### 4.8 localStorage 기반 토큰 저장 (프론트엔드)

- `src/lib/auth.ts` — `admin_token`, `session_token`을 `localStorage`에 저장
- XSS 공격 시 자격 증명 탈취 가능

### 4.9 하드코딩된 인프라 값

| 파일 | 줄 | 값 | 우려 |
|------|-----|-----|------|
| `app/config.py` | 127 | `http://172.18.0.1:11434` | Docker 브릿지 게이트웨이 IP |
| `app/config.py` | 114 | `http://localhost:8000` | 프로덕션 검증 존재하나 기본값이 localhost |
| `app/services/nowpayments.py` | 52 | `http://localhost:8000` | 웹훅 URL 하드코딩 폴백 |
| `app/services/telegram_bot_service.py` | 989, 992 | `https://app.telemon.online/miniapp` | 설정에서 가져와야 함 |

---

## 5. 개선 우선순위 제안

### Priority 1 — 즉시 수정 (버그/보안)

| # | 이슈 | 영향 | 위치 |
|---|------|------|------|
| 1 | `billing.py` NameError 버그 | 프로덕션 500 에러 | `app/api/billing.py:42` |
| 2 | Knowledge Base 문서 목록 인증 누락 | 데이터 노출 | `app/api/knowledge_base.py:48` |
| 3 | `/metrics` 엔드포인트 인증 추가 | 내부 정보 노출 | `app/main.py:461,534` |
| 4 | KnowledgeBaseTab fetch에 auth 헤더 추가 | 401 에러 | `KnowledgeBaseTab.tsx:113+` |
| 5 | `blockUser/unblockUser` 프론트엔드 호출 제거 또는 백엔드 구현 | 404 에러 | `api.ts:1205-1233` |

### Priority 2 — 높은 우선순위 (품질/안정성)

| # | 이슈 | 영향 | 위치 |
|---|------|------|------|
| 6 | `runtimeManager` ↔ `connectionManager` 통합 | 탭 간 데이터 불일치 | `src/lib/runtimeManager.ts`, `connectionManager.ts` |
| 7 | DeepSeek API 클라이언트 3곳 → 1곳으로 통합 | 설정 드리프트 | `app/services/deepseek_service.py`, `translation_service.py` |
| 8 | "deepseek-chat" 하드코딩 13곳 → config 참조로 변경 | 모델 변경 시 코드 수정 필요 | 위 표 참조 |
| 9 | DeepSeek API URL 하드코딩 제거 → config 사용 | 설정 우회 | `deepseek_service.py:12`, `guest_engine.py:44` |
| 10 | `api.ts` 3354줄 → 도메인별 파일 분리 | 유지보수성 | `src/lib/api.ts` |

### Priority 3 — 중간 우선순위 (일관성)

| # | 이슈 | 영향 | 위치 |
|---|------|------|------|
| 11 | 로깅 통일: `logging.getLogger` → `get_logger()` | 로그 형식 불일치 | 백엔드 26개 파일 |
| 12 | `bare except:` → `except Exception:` 변경 | 시그널 무시 | `deepseek_service.py:62` |
| 13 | 빈 catch 블록 64개 정리 | 에러 은닉 | 프론트엔드 전체 |
| 14 | `eslint-disable` 27개 억제 해결 | 잠재적 stale closure | `ChatManagementTab.tsx` 등 |
| 15 | 중복 SQLite DB 사용 제거 (trigger/draft routes) | 데이터 분산, 동기 블로킹 | `app/routers/trigger_routes.py`, `draft_routes.py` |
| 16 | 스케줄러 래핑 코드 활용 또는 제거 (죽은 코드) | 코드 혼란 | `app/scheduler/scheduler.py:305-339` |
| 17 | API 경로 패턴 일관성 (`/api/v1/kb/` → `/api/kb/`) | 혼란 | KnowledgeBase 관련 |

### Priority 4 — 낮은 우선순위 (정리)

| # | 이슈 | 영향 | 위치 |
|---|------|------|------|
| 18 | 죽은 코드 6개 파일 제거 | 번들 크기 | `src/lib/exportData.ts` 등 |
| 19 | `console.log` 제거 | 프로덕션 로그 오염 | `runtimeManager.ts:143,146` |
| 20 | 중복 엔드포인트 함수 정리 (`fetchSessionEvents` ↔ `fetchAccountSessionTimeline`) | 혼란 | `api.ts:729,914` |
| 21 | 하드코딩된 infra 값 config화 | 배포 유연성 | `config.py:127`, `nowpayments.py:52` |
| 22 | 관리자 설정 엔드포인트 rate limit 추가 | 무차별 대입 | `app/api/admin.py:133` |

---

## 부록: 공개 라우터 전체 목록 (백엔드 main.py)

| 줄 | 라우터 | 위험도 | 비고 |
|----|--------|--------|------|
| 373 | `admin_router` | LOW | 개별 엔드포인트에서 인증 검사 |
| 374 | `auth_router` | LOW | 인증 흐름 (공개 필수) |
| 375 | `ai_guest_router` | MEDIUM | 실제 AI 예산 소비 |
| 376 | `telegram_verify_router` | LOW | 채널 인증 게이트 |
| 394 | `billing_public_router` | LOW | 읽기 전용 가격 |
| 395 | `usdt_payment_router` | MEDIUM | 지갑 주소 + API 키 노출 가능 |
| 396 | `nowpayments_router` | LOW | 웹훅 서명 보호 |
| 398 | `referral_public_router` | LOW | 리더보드만 공개 |
| 400 | `free_api_key_router` | MEDIUM | 채널 인증 기반 키 발급 |
| 401 | `client_errors_router` | LOW | 에러 로깅 |
| 409 | `demo_router` | MEDIUM | 실제 AI 예산 소비 |
| 451 | `ws_router` | LOW | 핸들러 내부에서 인증 |
| 452 | `miniapp_router` | MEDIUM | 인증 없이 AI 호출 |
| 453 | `knowledge_base_router` | **HIGH** | `GET /documents` 인증 없음 |
| 454 | `translate_router` | MEDIUM | 실제 AI 예산 소비 |
| 455 | `link_preview_router` | LOW | SSRF 보호 존재 |
| 461 | `/metrics` | **HIGH** | 인증 없음 |
| 534 | `/api/metrics` | **HIGH** | 인증 없음 |
