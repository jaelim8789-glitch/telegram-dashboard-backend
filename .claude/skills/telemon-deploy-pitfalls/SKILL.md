---
name: telemon-deploy-pitfalls
description: Hard-won deployment/build pitfalls specific to TeleMon (Next.js frontend + FastAPI backend + Docker/nginx). Load before touching Docker builds, docker-compose networking, next.config.ts rewrites, service workers, or NEXT_PUBLIC_* env vars in this project.
---

# TeleMon deploy pitfalls

These are real bugs hit and fixed during actual deploys of this project — not theoretical. Check against this list before declaring a build/deploy "done".

## 1. `NEXT_PUBLIC_*` env vars are frozen at BUILD time, not runtime

`next.config.ts`'s `rewrites()`/`redirects()`/`headers()` are evaluated once during `next build` to produce `routes-manifest.json`. Setting `NEXT_PUBLIC_API_BASE_URL` via `docker run -e` on an already-built image has **no effect** — the value baked in at build time wins.

- Client bundles also inline `NEXT_PUBLIC_*` vars literally at build time — if a client component fetches `process.env.NEXT_PUBLIC_API_BASE_URL` directly, that value ships to the browser. Setting it to an internal Docker hostname (e.g. `http://backend:8000`) breaks every browser that isn't inside that Docker network.
- **Fix:** for nginx-fronted same-origin setups, build with `NEXT_PUBLIC_API_BASE_URL=""` (empty) so client code calls relative `/api/...` and nginx proxies it. Pass the real value via `--build-arg`, not `docker run -e`.

## 2. `useSearchParams()` needs a `<Suspense>` boundary or prod build fails outright

Any client component using `useSearchParams()` (directly, or via a shared component rendered in root `layout.tsx`, e.g. a page-transition progress bar) must be wrapped in `<Suspense fallback={...}>`. Without it, `next build` doesn't just warn — it **exits with an error and produces no build**, even though `next dev` may look fine.

## 3. `public/sw.js` runs in the browser — no Node globals

Files under `public/` are served as-is, never bundled/transpiled. `process.env.NODE_ENV` (or any `process.*`) inside `public/sw.js` throws `ReferenceError: process is not defined` at runtime, in every environment (dev and prod), because there's no `process` object in a service worker context. Guard with `typeof process !== 'undefined'` at minimum, or just don't reference it — a broken SW here silently breaks every subsequent page load with `net::ERR_FAILED` once the browser has cached the bad worker (see #5).

## 4. docker-compose: services on an `internal: true` network need EVERY consumer explicitly listed

If a network is declared `internal: true` (no outbound/host access), any service that needs to both (a) talk to another service on that network and (b) be reachable from the host (e.g. via a published port, or via nginx which itself needs a host-facing network too) must list **both** networks explicitly:
```yaml
networks:
  - internal
  - default
```
Forgetting this on `backend` broke DB connectivity; forgetting it on `nginx` broke backend proxying even after `backend` was fixed — two separate bugs from the same root cause.

## 5. A broken service worker persists across container rebuilds — browser caches it

If you fix a bug in `sw.js` and rebuild/redeploy, a browser that already registered the old broken worker keeps using it until it's unregistered or the origin/port changes. When debugging "still broken after rebuild," test on a fresh port or explicitly unregister the service worker — don't assume the rebuild didn't take effect.

## 6. Duplicate page files for the same route silently break routing

`src/app/page.tsx` and `src/app/(public)/page.tsx` both resolve to `/`. Next.js may not error loudly — the build can succeed with 39 routes verified, but the root path 404s at runtime. Route groups `(name)` are transparent to the URL; never have two page files resolving to the same path. If two exist, delete the stray one (check `git log --diff-filter=D -- src/app/page.tsx` on the main branch for precedent — this exact conflict was already resolved there once).

## 7. Never embed credentials in `git remote` URLs

A PAT embedded in `origin`'s URL (`https://user:TOKEN@github.com/...`) is inherited by every worktree of that repo and sits in plaintext in `.git/config`. Use `gh auth login` (stored in OS credential manager/keyring) instead. If a token was ever embedded this way, treat it as compromised — revoke and reissue, don't just remove it from the URL.

## Verification habit

After any Docker/deploy change: build the image, run it, and actually curl (or headless-browser-navigate) the real user-facing path — not just `/health`. A 200 on `/health` does not mean the app works; several of the bugs above only showed up when loading an actual page or submitting an actual form.
