# Architecture Audit — Recurring Broadcast Implementation

## Current Architecture (Broadcast)
- **Single Broadcast model** handles immediate, one-time-scheduled, and (future) recurring
- **POST /api/broadcast** accepts FormData with account_id, message, recipients, scheduled_at, image
- **BroadcastCreate schema**: account_id, message, recipients, scheduled_at
- **BroadcastRead schema**: id, account_id, message, media_path, recipients, status, scheduled_at, sent_at, created_at, error_message
- **CRUD**: create, get, update_status, list_due, claim, retry, list_logs
- **Scheduler**: `dispatch_due_broadcasts` runs every 30s, finds pending + due scheduled_at broadcasts, claims atomically
- **BroadcastProcessor**: `process_broadcast` executes the send, marks as sent/failed

## Existing RecurringSchedule Model
- A separate `RecurringSchedule` model exists in models/crud/schemas, but is **not integrated** into the API, scheduler, or main flow. It has `interval_minutes`, `group_ids` (not recipients), `is_active`, `total_sends`, `last_sent_at`.
- **Not reused** — it conflicts with the frontend contract which stores everything on the Broadcast model.

## Frontend Contract (commit fda2994)
- **ApiBroadcast** includes: `recurring_interval_minutes`, `cancelled_at`, `next_scheduled_at` (all nullable)
- **POST /api/broadcast** sends `recurring_interval_minutes` as Form field (allowed: 30/60/120/180/360/720/1440)
- **POST /api/broadcast/{id}/cancel** → returns updated Broadcast with cancelled status
- **GET /api/broadcast/recurring** → returns list of active recurring Broadcasts
- **Cancel status**: "cancelled" added to BroadcastStatus type
- **Helpers**: `isRecurringBroadcast`, `isRecurringActive`, `RECURRING_INTERVALS`

## Design Decision
The RecurringSchedule model is NOT reused. Instead, all recurring state is stored **directly on the Broadcast model** to match the frontend contract. The approach uses a **parent-child pattern**:
- The "parent" Broadcast row keeps `recurring_interval_minutes` + `next_scheduled_at`
- Each execution creates a child Broadcast record for history
- This preserves the existing scheduler architecture for dispatch