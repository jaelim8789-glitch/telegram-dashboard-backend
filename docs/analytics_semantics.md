# Delivery Analytics Semantics

## Overview

All delivery analytics are derived from `MessageLog` records persisted by the canonical delivery pipeline (`app/services/delivery.py`). This document describes the exact semantics of each analytics dimension.

---

## 1. MessageLog Analytics Semantics

### Row Granularity

Each `MessageLog` row represents **one delivery attempt to one recipient**. Multiple rows for the same `(source, source_id, recipient)` combination indicate retries. The row with `success=True` is the authoritative final state for that logical delivery.

### Attempt-Level vs Logical-Delivery-Level

| Category | Level | Explanation |
|---|---|---|
| Summary | Attempt-level | `total_attempted` counts all rows |
| Failure Breakdown | Attempt-level | Counts failure rows |
| Account Performance | Attempt-level | Sums all attempts per account |
| Timeline | Attempt-level | Aggregates all rows by time period |
| Recent Activity | Attempt-level | Shows individual attempt rows |
| Source Analytics | Attempt-level | Groups all attempts by source |
| Broadcast Analytics | Attempt-level | Groups attempts by broadcast_id |
| Failure Intelligence | Attempt-level | Analyzes failure rows |

**Important:** A recipient that failed twice and succeeded on the third attempt contributes 3 attempts (2 failed, 1 successful) to all aggregate metrics. This is the design choice — retries are **not** collapsed or deduplicated in aggregate counts.

---

## 2. Retry Handling

- Retries are **not excluded** from aggregate counts.
- Each retry attempt creates a separate `MessageLog` row via `_persist_log()` in `delivery.py`.
- The `attempt_count` field records which attempt number each row represents.
- Recoverable failures (`flood_wait`, `network_error`) are retried up to `MAX_RETRIES = 3`.
- Permanent failures (`banned`, `forbidden`, `invalid_recipient`, `session_expired`, `permanent_failure`, `internal_error`) are **not** retried.

---

## 3. Success/Failure Counting

- A row is "successful" if `success=True` and `status='success'`.
- All other rows are "failed" (including `flood_wait`, `network_error`, etc.).
- The `success_rate` is calculated as `(successful / total) * 100`, rounded to 1 decimal place.
- Zero-division is handled: if `total == 0`, `success_rate = 0.0`.

---

## 4. Source Attribution

The `MessageLog.source` field records the origin of each delivery attempt:

| Source Value | Origin |
|---|---|
| `broadcast` | Bulk broadcast via `broadcast_processor.py` |
| `reply_macro` | Reply macro execution |
| `manual` | Manual send via dashboard or API |
| `scheduled` | Scheduler-triggered delivery |
| `auto_reply` | Auto-reply listener (future) |

Source analytics aggregate all attempts by this field. Categories are derived from actual persisted data — there are no hardcoded zero-valued categories.

---

## 5. Tenant Isolation

- All analytics functions require an `Identity` for authorization.
- `_resolve_authorized_account_ids()` determines which accounts are visible:
  - **Admin**: sees all accounts.
  - **User with tenant_id**: sees only accounts where `Account.tenant_id == identity.tenant_id`.
  - **API key without tenant_id**: sees no accounts (returns empty).
- Optional `account_id` parameter further filters to a single account, but only if that account is authorized for the current identity.
- Cross-tenant queries are impossible — `_resolve_authorized_account_ids()` never returns accounts from other tenants.
- NULL-tenant accounts remain excluded for tenant users.

---

## 6. Filtering

All existing analytics endpoints accept optional filters:

| Filter | Type | Behavior |
|---|---|---|
| `source` | string | Filters `MessageLog.source` (exact match) |
| `account_id` | string | Filters to specific authorized account |
| `status` | string | Filters `MessageLog.status` (exact match) |
| `start_time` | ISO datetime | Filters `MessageLog.created_at >= start_time` |
| `end_time` | ISO datetime | Filters `MessageLog.created_at <= end_time` |

All filters preserve tenant isolation. Invalid datetime values are silently ignored (treated as no filter).

---

## 7. Broadcast Correlation

Broadcast analytics correlate `MessageLog` records via:
- `source = 'broadcast'`
- `source_id = <broadcast.id>`

**Limitations:**
- Only broadcasts with persisted `MessageLog` records appear in results.
- Broadcasts that were created but never processed (zero delivery attempts) are excluded.
- Counts are attempt-level within each broadcast.

---

## 8. Delivery Latency

### Current Limitation

The `MessageLog` model has **no start/end timing fields** beyond `created_at`. Specifically:

- No `started_at` or `sent_at` timestamp
- No `queue_duration` or `send_duration` metric
- No `enqueued_at` timestamp from the delivery pipeline

**Conclusion:** Accurate latency analytics (average, p50, p95, queue-to-send duration) are **not possible** with the current data model.

### Minimum Future Schema Change Required

To support latency analytics, the `MessageLog` model would need:

1. `enqueued_at: datetime` — when the delivery was queued
2. `started_at: datetime` — when the send attempt began
3. `completed_at: datetime` — when the send attempt finished

These fields must be populated by the delivery pipeline (`_send_single()` and `_deliver_with_retry()` in `delivery.py`).

---

## 9. Failure Intelligence

Failure intelligence provides enhanced failure analytics:

- `count` — number of failure occurrences per status
- `percentage` — percentage of total failures
- `affected_accounts` — count of distinct accounts affected
- `latest_occurrence` — ISO timestamp of most recent occurrence

**Safety:** The failure intelligence endpoint only exposes the safe `status` enum value and aggregate counts. It never exposes:
- Raw exception details
- API keys
- Telegram session secrets
- Credentials
- Internal filesystem paths

The `error_message` field is already sanitized at persistence time by the delivery pipeline's `classify_error()` function.

---

## 10. Overview Endpoint

`GET /api/delivery-analytics/overview` provides a single aggregated response combining:

- Summary (attempt-level totals)
- Source breakdown
- Top 5 accounts by performance
- Failure intelligence
- Daily timeline

Response sections are `null` if no data exists. Top accounts are bounded to 5. Timeline uses day interval only.