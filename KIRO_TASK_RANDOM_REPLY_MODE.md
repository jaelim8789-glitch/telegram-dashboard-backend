# 작업 지시서 — 랜덤 리플라이 "모드" (토글 방식으로 재구현)

## 배경 / 무엇이 잘못됐었는지
이전에 제가(Claude) 발송 페이지에 "채팅방 ID 입력 → 메시지 입력 → 지금 실행" 하는 1회성 수동 폼(`RandomReplyPanel.tsx`)을 만들었는데, 이건 사용자가 원한 게 아니었습니다.

**실제로 원하는 것**: 발송 페이지에 토글 버튼 하나. 켜면(ON) → 지금 발송 대상으로 이미 잡혀있는 모든 채널/그룹에서, 그 안의 무작위 인원에게 자동으로 답장을 계속 발송하는 "모드"가 켜짐. 채팅방 ID를 손으로 입력할 필요 없음 — 이미 선택된 발송 대상 목록을 그대로 재사용.

## 작업 방식
1. `git checkout master && git pull origin master && git checkout -b feat/random-reply-mode`
2. 이 브랜치는 프론트(`TeleMon-kiro`)와 백엔드(`telegram-dashboard-backend`) 둘 다에 필요합니다. 각 저장소에서 동일한 브랜치명으로 작업.
3. 끝나면 PR 만들고, CI/타입체크 통과 확인 후 **직접 merge까지 진행**해주세요.

## 프론트 작업
- `src/components/workspace/RandomReplyPanel.tsx` — 지금 있는 수동 폼(채팅방 ID textarea, 메시지 textarea, "지금 실행" 버튼) 걷어내고, 다음으로 교체:
  - 토글 스위치 하나 ("랜덤 리플라이 모드")
  - 켜면: 현재 SendTab에서 이미 선택된 발송 대상 그룹 목록(`selectedIds`/`groups` — SendTab.tsx의 기존 상태 재사용)을 백엔드에 등록
  - 꺼짐/켜짐 상태와 마지막 실행 시각을 화면에 표시 (예: "지난 5분 전 3건 발송")
- `src/lib/api.ts`에 새 함수 추가 필요: `enableRandomReplyMode(accountId, targetChatIds, messageContent)` / `disableRandomReplyMode(accountId)` — 아래 백엔드 엔드포인트에 맞춰서.

## 백엔드 작업
- 현재 `app/api/reply_macro.py`의 `/{macro_id}/random-reply`는 "한 번 호출하면 한 번 실행"하는 구조입니다. 이걸 **지속 모드**로 바꿔야 합니다:
  - `ReplyMacro` 모델(또는 새 필드)에 `mode: "manual" | "continuous"` 같은 플래그 추가, 혹은 `is_active=True`인 매크로를 스케줄러가 주기적으로 자동 실행하도록 변경
  - `app/scheduler/scheduler.py`를 보면 이미 `dispatch_due_random_replies`라는 job이 등록되어 있습니다 (`app/services/random_reply_service.py`) — 이게 이미 "지속 모드"를 염두에 두고 만들어진 기존 인프라일 가능성이 높습니다. **새로 만들기 전에 이 기존 코드부터 확인하세요** — 이미 있는 걸 또 만들면 중복입니다.
  - 프론트의 "토글 ON"은 곧 `is_active=True`인 매크로를 생성/업데이트하는 것과 같아야 하고, 그러면 이미 존재하는 스케줄러가 알아서 주기적으로 실행하는 구조가 맞습니다.
- 발송 대상(target_chats)은 프론트에서 SendTab의 현재 선택된 그룹 목록을 그대로 넘겨받아 매크로의 `target_chats`에 저장.

## 확인해야 할 것
- 토글 ON 상태에서 스케줄러가 실제로 주기적으로 실행되는지 (로그로 확인: `docker logs ... | grep random_reply`)
- 토글 OFF 하면 확실히 멈추는지
- 계정당 1분 간격 같은 안전장치가 이미 있는지 확인 (있으면 유지, 없으면 추가 — 너무 빠른 반복 발송으로 계정 정지 위험 있음)

## 주의
- 코드 변경 전에 `app/services/random_reply_service.py`와 `app/scheduler/scheduler.py`의 `dispatch_due_random_replies` 부분을 먼저 읽고, 이미 구현된 지속 실행 로직이 있는지 반드시 확인 후 시작하세요.
