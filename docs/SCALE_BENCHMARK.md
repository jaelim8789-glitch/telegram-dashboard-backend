# TeleMon Scale Validation — Benchmark Report (Epic 23)

> Run BEFORE (baseline) and AFTER (post-optimization) and diff the two JSON
> reports. This file is the template — fill in the actual numbers.

## Environment

- Target: `[staging VPS | local docker]` (NEVER production)
- Date: `YYYY-MM-DD`
- Duration: `[10m | 30m | 6h]`
- Backend commit: `[sha]`

## Load profile (80% real / 20% mock)

| Scenario | Volume | Real/Mock |
|---|---|---|
| Telegram accounts | 5 | Real |
| Broadcast | 100 | Real |
| Auto Reply | 100 | Real (rules) |
| AI requests | 100 | Mock provider |
| Chat reads | continuous | Real API |
| WebSocket | 5 clients | Real |

## Metrics

### API latency (ms)

| | P50 | P95 | P99 | Max |
|---|---|---|---|---|
| Before | | | | |
| After | | | | |
| Delta | | | | |

### Success / Error budget

| | Requests | Errors | Success rate | Target |
|---|---|---|---|---|
| Before | | | | 99.9% |
| After | | | | 99.9% |

### WebSocket latency (ms)

| | P50 | P95 | Max |
|---|---|---|---|
| Before | | | |
| After | | | |

### Scheduler / Queue

| | Scheduler delay (s) | Queue length | Notes |
|---|---|---|---|
| Before | | | |
| After | | | |

### System saturation

| | CPU% | Mem% | Disk IO | Redis | PG | Network | WS conns | Workers |
|---|---|---|---|---|---|---|---|---|
| Before | | | | | | | | |
| After | | | | | | | | |

## Chaos test results

| Scenario | Degraded detected (s) | Recovered (s) | Incident cleared | Notes |
|---|---|---|---|---|
| Redis restart | | | | |
| DB restart | | | | |
| Worker kill | | | | |
| Telegram disconnect | | | | |

## Bottlenecks found

1. `[describe, e.g. N+1 query in /broadcasts]` — evidence: `[metric]`
2. `[redis key churn]` — evidence: `[metric]`
3. …

## Fixes applied (Low-risk only)

1. `[file:line]` — `[change]` — impact: `[metric delta]`
2. …

## Out-of-scope (deferred to separate Epic)

- `[high-risk or large refactor]`

## Commands

```bash
# seed volume
python scripts/seed_scale.py accounts=5 conversations=100 messages=10000

# baseline (Before)
python scripts/scale_test.py --base http://<staging>:8000 --login-token <token> \
  --duration 1800 --broadcasts 100 --ai 100 --out baseline.json

# chaos
python scripts/chaos_test.py --compose-dir /opt/telemon/backend \
  --base http://<staging>:8000 --token <token> --out chaos.json

# After optimizations — same command, new file → diff
python scripts/scale_test.py ... --out after.json
```
