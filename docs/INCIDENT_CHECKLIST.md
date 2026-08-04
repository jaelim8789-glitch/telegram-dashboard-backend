# TeleMon Incident Checklist (Epic 20)

On-call runbook for real service outages. Follow top-to-bottom.

## 0. Acknowledge

1. Check the incident banner in the Admin Operations Center (or `GET /api/admin/incidents`).
2. Note the incident name + opened time. The engine already sent a webhook alert.
3. Auto-recovery is running (5s → 15s → 30s exponential backoff). Watch for `Recovered`.

## 1. PostgreSQL Down (Critical)

Symptoms: incident `postgres`, health score database low, `/health` returns 503 database.

Steps:
- [ ] `docker compose ps db` — container up? exit code?
- [ ] `docker logs db --tail 50` — disk full? connection limit? OOM?
- [ ] Disk: `df -h /var/lib/docker` — if full: prune logs/images, restart db.
- [ ] If container crashed: `docker compose up -d db`
- [ ] If unhealthy but up: check `pg_isready -U telegram_dashboard`, look for `FATAL: terminating connection due to administrator command`.
- [ ] If DB won't start after fix: restore from backup (see §Recovery).
- [ ] Confirm `/health` database: healthy → engine auto-closes incident.

## 2. Redis Down (Critical)

Symptoms: incident `redis`, health score redis low.

Steps:
- [ ] `docker compose ps redis`
- [ ] `docker logs redis --tail 50` — auth failure? OOM?
- [ ] `docker compose up -d redis`
- [ ] Confirm `redis-cli -a $REDIS_PASSWORD ping` → PONG.
- [ ] Rate limiter falls back to in-memory automatically; session WS still works after reconnect.

## 3. Scheduler Down (Critical)

Symptoms: incident `scheduler`, `next_tick_at` stale in operations overview.

Steps:
- [ ] `docker compose ps backend`
- [ ] `docker logs backend --tail 100 | grep -i scheduler`
- [ ] If process alive but scheduler not running: `docker compose restart backend` (singleton lock re-acquired).
- [ ] Verify `GET /api/scheduler/status` → `scheduler_running: true`.

## 4. Queue Stall (Critical — 5 min zero throughput)

Symptoms: incident `queue`, broadcast count frozen.

Steps:
- [ ] `docker logs backend --tail 100 | grep -i "dispatch\|broadcast"`
- [ ] Check Telethon pool: are accounts connected? `GET /api/accounts/sync-progress`
- [ ] FloodWait burst? Accounts globally rate-limited? Back off, let retry backoff recover.
- [ ] `docker compose restart backend` if dispatcher wedged.

## 5. Telegram API Down / All Accounts Fail (Critical)

Symptoms: incident `telegram`, all accounts need-login/disconnected.

Steps:
- [ ] Confirm it's Telegram-side, not our creds: try a manual `get_me()` via a connected account.
- [ ] Telethon errors (FloodWait / AuthKeyInvalid) — bulk re-auth via Maintenance drawer.
- [ ] If session keys rotated: `docker compose restart backend` re-runs `restore_all`.

## 6. Slow / Degraded (Warning)

Symptoms: warning incidents, latency/error-rate banners.

Steps:
- [ ] Redis latency > 100ms: check slow keys, `redis-cli --latency`.
- [ ] DB query > 500ms: `EXPLAIN` hot queries, add index.
- [ ] Queue > 1000: scale workers / pause low-priority broadcasts.
- [ ] FloodWait > 30% accounts: pause auto-reply, stagger dispatch.
- [ ] API error rate > 5%: check recent errors in operations overview.

## Recovery (restore from backup)

Only if data loss occurred (DB volume corrupted).

```sh
# Latest auto-backup
ls -la /backups/telemon/latest.sql.gz
# Restore (stops app writes first)
docker compose stop backend
docker compose exec -T db psql -U telegram_dashboard < <(gunzip -c /backups/telemon/latest.sql.gz)
docker compose start backend
```

## Notifications

- Auto: incident open (CRITICAL/WARNING) + recovery (RECOVERED) → `ALERT_WEBHOOK_URL`.
- Manual DR: `./scripts/dr_recovery.sh <service>` sends info alerts.
- All alerts include server label `telemon-prod-01` + downtime.

## Escalation

- If recovery fails after 3 retries → engine logs `incident_recovery_failed` + alert.
- Escalate to: #ops channel with incident id + `/api/admin/incidents/history` snippet.
