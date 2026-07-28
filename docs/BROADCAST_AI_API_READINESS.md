# Broadcast / AI Chat API Readiness

> 캡처일: 2026-07-29. 코덱스 새 디자인(채팅관리, 발송 마법사) 연동 기준.

## 1. Broadcast 기능별 매핑

### 메시지 + 이미지 첨부해서 여러 방에 발송 ✅ 완료

`POST /api/broadcast` (multipart/form-data)

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `account_id` | string | ✅ | 발송 계정 ID |
| `message` | string | ✅ (reply 시 선택) | 텍스트 메시지 |
| `recipients` | JSON string[] | 조건부 | 수신처 chat_id 배열 (JSON string) |
| `group_ids` | JSON string[] | 조건부 | 그룹 ID 배열 (recipients 대체) |
| `image` | UploadFile | ❌ | 첨부 이미지 |
| `delivery_mode` | `"normal" \| "cycle" \| "bulk" \| "reply"` | ❌ | 발송 모드 |

응답 예시:
```json
{"id":"uuid","account_id":"uuid","message":"...","status":"pending","created_at":"2026-07-29T...","delivery_mode":"normal",...}
```

**curl 테스트:**
```bash
curl -X POST https://telemon.online/api/broadcast \
  -H "Authorization: Bearer <token>" \
  -F "account_id=b6b50ce8-..." \
  -F "message=안녕하세요" \
  -F 'recipients=["-100123456789"]' \
  -F "image=@photo.jpg"
```

### 반복발송 ✅ 완료

| 엔드포인트 | 메서드 | 설명 |
|---|---|---|
| `POST /api/broadcast` (with `recurring_interval_minutes`) | POST | 반복 발송 생성 (30/60/120/180/360/720/1440분) |
| `GET /api/broadcast/recurring` | GET | 반복 발송 목록 |
| `POST /api/broadcast/{id}/pause` | POST | 일시중지 |
| `POST /api/broadcast/{id}/unpause` | POST | 재개 |
| `GET /api/broadcast/{id}/children` | GET | 자식 발송 내역 |
| `POST /api/broadcast/{id}/cancel` | POST | 취소 |

curl 응답 예시 (recurring 목록):
```json
[]
```

### 예약발송 ✅ 완료

`POST /api/broadcast` (with `scheduled_at`)

| 필드 | 설명 |
|---|---|
| `scheduled_at` | ISO 8601 datetime string (UTC). 미래 시각이면 예약, 과거/생략이면 즉시 발송 |

캘린더 조회: `GET /api/schedule/calendar?start=...&end=...` (start/end 필수)
스케줄러 상태: `GET /api/scheduler/status`

```json
{"tick_interval_seconds":30,"next_tick_at":"...","due_broadcasts_count":0,"running_broadcasts_count":0,"scheduler_running":true}
```

### 랜덤 리플라이 / 자동응답 ✅ 부분적

담당 라우트: `/api/accounts/{account_id}/auto-reply/*` (별도 라우터)

| 엔드포인트 | 설명 |
|---|---|
| `GET/PUT .../auto-reply/{rule_id}` | 규칙 CRUD |
| `POST .../auto-reply/toggle` | 활성/비활성 |
| `GET .../auto-reply/logs` | 응답 로그 |
| `PATCH .../auto-reply/ai-fallback` | AI 폴백 설정 |
| `GET .../auto-reply/suggestions` | AI 추천 응답 |
| `POST .../suggestions/{id}/reviewed` | 추천 검토 처리 |

**curl 테스트 (logs):** `[]` (기록 없음)

AI 기반 자동응답(v2)도 별도 라우터 존재: `POST /api/ai-reply-v2/suggestions`

---

## 2. AI Chat — 자유 대화형 채팅

### `/api/ai-chat-v2/*` ✅ SSE 스트리밍

**세션 관리:**

| 엔드포인트 | 메서드 | 설명 |
|---|---|---|
| `/sessions` | POST | 세션 생성 |
| `/sessions` | GET | 세션 목록 |
| `/sessions/{id}` | GET | 세션 조회 |
| `/sessions/{id}` | PUT | 세션 수정 |
| `/sessions/{id}` | DELETE | 세션 삭제 |
| `/sessions/{id}/messages` | GET | 메시지 내역 |

**채팅 (SSE):**

`POST /api/ai-chat-v2/chat`

```
Request:  {"session_id":"uuid","message":"오늘 발송 현황 알려줘"}
Response: SSE stream →
  data: {"type":"chunk","content":"현재 "}
  data: {"type":"chunk","content":"발송 "}
  ...
  data: {"type":"done","message_id":"uuid","content":"..."}
```

**curl 테스트 (세션 목록):** `[]` (아직 없음)
**curl 테스트 (템플릿):** `[]` (아직 없음)

### `/api/ai/chat/*` ✅ Legacy

| 엔드포인트 | 설명 |
|---|---|
| `POST /api/ai/chat` | 전체 응답 (non-streaming) |
| `GET /api/ai/chat/history/{session_id}` | 대화 내역 |
| `GET /api/ai/chat/sessions` | 세션 목록 |

---

## 3. 추가 AI 엔드포인트

| prefix | routes | 설명 |
|---|---|---|
| `/api/ai-reply-v2` | 14 | AI 자동응답 제안/검토 |
| `/api/ai/content-studio` | 6 | 콘텐츠 생성/캘린더 |
| `/api/copilot` | 6 | AI 코파일럿 (추천/액션) |
| `/api/ai/agents` | 8 | AI 에이전트 관리 |

---

## 결론

| 기능 | 상태 | 비고 |
|---|---|---|
| 이미지+메시지 발송 | ✅ **완료** | multipart, 분산 발송 지원 |
| 반복발송 | ✅ **완료** | pause/unpause/children |
| 예약발송 | ✅ **완료** | scheduler daemon, calendar API |
| 랜덤 리플라이 | ✅ **부분적** (auto-reply router) | AI 추천+v2 라우터 존재 |
| AI 자유채팅 | ✅ **완료** (SSE, legacy) | 세션관리+스트리밍 |

**추가 권장:** 이미지 첨부 시 `media_path`가 `BroadcastRead`에 포함되나, 업로드된 이미지를 발송 시 Telegram API에 전달하는 로직은 `services/broadcast_processor.py`에서 실제 구현됨. 코덱스가 구현할 발송 마법사에서 multipart로 이미지를 보내면 정상 처리됩니다.
