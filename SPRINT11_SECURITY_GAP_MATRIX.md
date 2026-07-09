# Sprint 11 — Security Gap Matrix

| # | Resource | Router File | Owning Entity | Tenant Resolution | Auth Dep | AuthZ Dep | Gap | Risk |
|---|---|---|---|---|---|---|---|---|
| 1 | Reply Macro CRUD | `app/api/reply_macro.py` | Account.tenant_id | ❌ None | `require_api_key_or_admin` (global) | ❌ None | **CRITICAL**: Tenant B can list/create/update/delete/execute Tenant A's reply macros | HIGH |
| 2 | Auto Reply CRUD | `app/api/auto_reply.py` | Account.tenant_id | ❌ None | global only | ❌ None | **CRITICAL**: Tenant B can manage Tenant A's auto-reply rules | HIGH |
| 3 | Broadcast | `app/api/broadcast.py` | Account.tenant_id | ❌ None | global only | ❌ None | **CRITICAL**: Tenant B can broadcast via Tenant A's account | HIGH |
| 4 | Group Search | `app/api/group_search.py` | Account.tenant_id | ❌ None | global only | ❌ None | **CRITICAL**: Tenant B can search/join groups as Tenant A | HIGH |
| 5 | Groups | `app/api/groups.py` | Account.tenant_id | ❌ None | global only | ❌ None | **CRITICAL**: Tenant B can list Tenant A's groups | HIGH |
| 6 | Logs | `app/api/logs.py` | Account.tenant_id | ❌ None | global only | ❌ None | **CRITICAL**: Tenant B can read Tenant A's logs | HIGH |
| 7 | Scheduler | `app/api/scheduler.py` | Account.tenant_id | ❌ None | global only | ❌ None | **CRITICAL**: Tenant B can manage Tenant A's schedules | HIGH |
| 8 | Billing | `app/api/billing.py` | Tenant | `tenant_id` path param | global only | `require_tenant_access` ✅ (Sprint 10) | ✅ Fixed | - |
| 9 | USDT Payment | `app/api/usdt_payment.py` | Tenant | `tenant_id` path param | ❌ None (no global dep) | ❌ None | **CRITICAL**: No auth at all on payment endpoints | HIGH |
| 10 | Features | `app/api/features.py` | Tenant | `tenant_id` path param | global only | `require_tenant_access` ✅ (Sprint 10) | ✅ Fixed | - |
| 11 | Accounts | `app/api/accounts.py` | Account.tenant_id | `tenant_id` field | global only | `require_account_tenant_access` ✅ (Sprint 10) | ✅ Fixed | - |
| 12 | Telegram Auth | `app/api/telegram_auth.py` | Account.tenant_id | `account_id` path | global only | `require_account_tenant_access` ✅ (Sprint 11 fix) | ✅ Fixed | - |

## Sprint 11 Scope: Fix items 1-7, 9

### Fix Pattern
For each account-scoped router, add:
1. `from app.api.deps import get_current_identity, Identity, require_account_tenant_access`
2. `identity: Identity = Depends(get_current_identity)` to each endpoint
3. `await require_account_tenant_access(account_id, db, identity)` after `_get_account_or_404()`

### Files to modify
- `app/api/reply_macro.py` — 7 endpoints
- `app/api/auto_reply.py` — 6 endpoints
- `app/api/broadcast.py` — 2 endpoints
- `app/api/group_search.py` — 5 endpoints
- `app/api/groups.py` — check
- `app/api/logs.py` — check
- `app/api/scheduler.py` — check
- `app/api/usdt_payment.py` — check