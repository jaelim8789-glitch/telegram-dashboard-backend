# API Response Schema Reference

> Generated 2026-07-29. Pydantic response models for core routes.
> Fields annotated with ` | None` are nullable — the frontend MUST handle `null`.

## Accounts

### `GET /api/accounts` — list

Returns `PaginatedAccounts`:

```
items: AccountWithHealth[]
total: int
page: int
page_size: int
total_pages: int
```

#### `AccountWithHealth` (list item)

| Field | Type | Nullable | Notes |
|---|---|---|---|
| `id` | `str` | no | UUID |
| `phone` | `str` | no | |
| `name` | `str` | **yes** | |
| `status` | `"active" \| "inactive" \| "banned"` | no | |
| `health_status` | `"healthy" \| "unauthorized" \| "banned" \| "rate_limited" \| "error" \| "unknown" \| "not_configured"` | no | |
| `has_session` | `bool` | no | |
| `today_sent` | `int` | no | |
| `group_count` | `int` | no | |
| `last_activity` | `datetime` | **yes** | ISO 8601 |
| `last_error` | `str` | **yes** | |
| `last_error_at` | `datetime` | **yes** | |
| `last_success_at` | `datetime` | **yes** | |
| `health_checked_at` | `datetime` | **yes** | |
| `auto_reply_enabled` | `bool` | no | |
| `recent_success_count` | `int` | no | |
| `recent_failure_count` | `int` | no | |
| `total_delivery_attempts` | `int` | no | |
| `created_at` | `datetime` | no | |
| `updated_at` | `datetime` | no | |

### `GET /api/accounts/summary` — operational summary

Returns `AccountSummary`:

| Field | Type | Nullable |
|---|---|---|
| `total` | `int` | no |
| `healthy` | `int` | no |
| `unhealthy` | `int` | no |
| `not_configured` | `int` | no |
| `banned` | `int` | no |
| `rate_limited` | `int` | no |
| `unauthorized` | `int` | no |
| `active_accounts` | `int` | no |
| `inactive_accounts` | `int` | no |
| `has_session` | `int` | no |
| `has_errors` | `int` | no |
| `total_today_sent` | `int` | no |
| `total_groups` | `int` | no |

### `POST /api/accounts/bulk` — bulk action

Request: `BulkActionRequest` → Response: `BulkActionResponse`

```
results: BulkActionResult[]
total_processed: int
total_failed: int
```

`BulkActionResult`: `account_id: str`, `success: bool`, `error: str | None`

---

## Broadcast

### `GET /api/broadcast/{id}` — single broadcast

Returns `BroadcastRead`:

| Field | Type | Nullable | Notes |
|---|---|---|---|
| `id` | `str` | no | |
| `account_id` | `str` | no | |
| `message` | `str` | no | |
| `media_path` | `str` | **yes** | |
| `recipients` | `list[str]` | no | |
| `status` | `"pending" \| "sending" \| "sent" \| "failed" \| "cancelled"` | no | |
| `scheduled_at` | `datetime` | **yes** | |
| `sent_at` | `datetime` | **yes** | |
| `created_at` | `datetime` | no | |
| `error_message` | `str` | **yes** | |
| `recurring_interval_minutes` | `int` | **yes** | |
| `cancelled_at` | `datetime` | **yes** | |
| `next_scheduled_at` | `datetime` | **yes** | |
| `parent_broadcast_id` | `str` | **yes** | for recurring children |
| `is_recurring_paused` | `bool` | no | |
| `failure_info` | `dict` | **yes** | |
| `delivery_mode` | `"normal" \| "cycle" \| "bulk" \| "reply"` | no | |
| `reply_to_msg_id` | `int` | **yes** | |
| `delay_seconds` | `int` | **yes** | |
| `inline_buttons` | `list[dict]` | **yes** | |
| `group_ids` | `list[str]` | **yes** | |
| `groups_resolved` | `bool` | no | |
| `campaign_id` | `str` | **yes** | |
| `distribution_batch_id` | `str` | **yes** | |
| `content_studio_content_id` | `str` | **yes** | |

### `GET /api/broadcast/distribution/{batch_id}` — distribution status

Returns `DistributionStatusResponse`:

| Field | Type | Nullable |
|---|---|---|
| `batch_id` | `str` | no |
| `siblings` | `list[DistributionSiblingRead]` | no |

`DistributionSiblingRead`:
- `broadcast: BroadcastRead`
- `account_id: str`
- `account_phone: str`
- `account_name: str | None`

### `POST /api/broadcast/estimate` — time estimate

Returns `BroadcastEstimateResponse`:

| Field | Type |
|---|---|
| `estimated_seconds` | `int` |
| `estimated_minutes` | `int` |
| `readable` | `str` |

---

## Schedule

### `GET /api/schedule/calendar` — calendar view

Returns `list[CalendarEntry]`:

| Field | Type | Nullable |
|---|---|---|
| `id` | `str` | no |
| `title` | `str` | no |
| `scheduled_at` | `datetime` | **yes** |
| `status` | `str` | no |
| `broadcast_id` | `str` | **yes** |
| `campaign_id` | `str` | **yes** |

### `GET /api/scheduler/status` — scheduler status

| Field | Type | Notes |
|---|---|---|
| `active` | `bool` | scheduler running? |
| `queue_size` | `int` | pending jobs |
| `last_tick` | `datetime \| None` | ISO 8601 |

### `POST /api/schedule/sync` — sync schedule

Returns `SyncResponse`:
| Field | Type |
|---|---|
| `synced` | `int` |

---

## Common nullable patterns

All `datetime` fields SHOULD be treated as nullable — `created_at`, `updated_at` are non-nullable
(always set by the ORM), but most other timestamps (`last_activity`, `scheduled_at`, `sent_at`)
can be `null`/`None`.

> **Frontend rule of thumb:** `?.` or `|| ""` on every field that isn't `id`, `phone`,
> `status`, `created_at`, or `updated_at`.
