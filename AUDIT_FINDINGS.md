# Production Audit — Recurring Broadcast Backend

## Audit Summary: FIXES REQUIRED (2 bugs)

---

## BUG 1 (CRITICAL): Crash-safety — parent `next_scheduled_at` advanced AFTER child delivery

**Location**: `app/services/broadcast_processor.py` — `process_recurring_parent()`

**Problem**: The parent's `next_scheduled_at` is advanced in a separate `reschedule_recurring_broadcast` call AFTER `process_broadcast(child_id)` completes. If the process crashes between child completion and rescheduling, the parent's `next_scheduled_at` stays in the past. The next scheduler tick will find it due and create a **second** child — causing the same broadcast interval to fire twice.

**Fix**: Advance `next_scheduled_at` at the time of child creation, BEFORE calling `process_broadcast`. This prevents double-fire on crash. The parent's schedule moves forward regardless of whether the child succeeds or fails.

## BUG 2 (IMPORTANT): Recurring parent records pollute broadcast history

**Location**: `app/crud/broadcast.py` — `list_logs()`

**Problem**: `list_logs()` returns ALL broadcasts including recurring parent records. Parents have `status='sending'` (from `claim_broadcast_dispatch`), which makes them appear as stuck "sending" entries in SendTab/LogTab. The actual execution history is maintained by child records; parents should be excluded from logs.

**Fix**: Filter out recurring parents (`recurring_interval_minutes IS NOT NULL`) from `list_logs`.

## Issues Investigated & VERIFIED SAFE

### Contract alignment ✅
- POST /api/broadcast recurring_interval_minutes: ✅ Matches frontend FormData field name
- GET /api/broadcast/recurring: ✅ Returns active recurring parents
- POST /api/broadcast/{id}/cancel: ✅ Works on recurring parents only
- BroadcastRead: ✅ Exposes all fields frontend expects
- parent_broadcast_id: ✅ Exposed but frontend ignores it (safe)
- cancelled status: ✅ Frontend/types.ts BroadcastStatus includes "cancelled"
- /api/logs: ⚠️ Bug #2 above — will fix

### End-to-end lifecycle ✅
- Create recurring broadcast: Works
- First scheduled execution: Creates child, dispatches through pipeline
- Child history: Child records created with parent_broadcast_id link
- Next_scheduled_at advancement: ⚠️ Bug #1 above — will fix
- Cancellation: Sets status='cancelled', clears next_scheduled_at, excludes from dispatch
- Cancelled never executes again: Verified — query checks status != "cancelled"
- Restart recovery: overdue next_scheduled_at picked up correctly
- Failed child execution: Only ONE child created (not duplicate-spammed)
- Account/tenant isolation: Uses existing require_account_tenant_access mechanism

### Parent/child history semantics ✅ (after fixes)
- Parents excluded from logs via fix #2
- Children distinguishable: parent_broadcast_id is set, recurring_interval_minutes is null
- DashboardTab recurring panel only receives active parents from list_recurring_broadcasts
- Analytics (delivery_analytics.py) uses MessageLog, not Broadcast — no double-count
- Retrying a child: safe — retry checks status='failed', child status is independent
- Cancelling a child: impossible — cancel checks recurring_interval_minutes is not None

### Scheduler correctness ✅
- Atomic claim via status='sending' prevents overlapping execution
- _running_recurring in-memory guard prevents same-process duplicates
- _running_recurring only for recurring (separate from _running_broadcasts)
- Scheduler tick overlap: next tick finds parent already dispatched (claimed), skips
- Transaction rollback: DB commit failures roll back correctly via SQLAlchemy
- Cross-worker safety: claim_broadcast_dispatch is DB-level (status='pending'→'sending')
- Overdue recovery: Only ONE catch-up child created (not backlog)
- Timezone: All timestamps use utcnow_naive() pattern

### Migration safety ✅
- All columns nullable with default=None
- Self-referencing FK with ondelete="SET NULL"
- PostgreSQL compatible
- Downgrade: reverses all changes

### Existing broadcast regression ✅
- All existing CRUD functions unchanged (create, get, update_status, list_due, claim, retry)
- list_logs modified only to filter recurring parents
- Scheduler flow for one-time broadcasts unchanged
- Broadcast processor unchanged for non-recurring broadcasts
- Retry endpoint: unchanged

## Tests
- 30 recurring tests + all existing passing (13 pre-existing failures unchanged)
- After fixes: will run full test suite