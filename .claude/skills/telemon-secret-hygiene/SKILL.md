---
name: telemon-secret-hygiene
description: Credential/secret handling rules for TeleMon, based on real leaks found in this repo (PAT in git remote URL, live admin API token committed in a JSON report, hardcoded prod admin password across multiple e2e test files). Load before writing test files that need auth, before committing anything that logs API responses, or before touching git remote config.
---

# TeleMon secret hygiene

Real incidents found in this repo — not hypothetical:
- `e2e/qa_report.json` contained a live admin API token in plaintext, committed to git.
- `e2e/prod-reply-macro.spec.ts` and five other files had the real production admin username/password hardcoded as fallback defaults.
- `origin` remote URL had a GitHub PAT embedded in plaintext (`https://user:TOKEN@github.com/...`), inherited by every worktree.
- A committed `.env` file (not `.env.example`) in one worktree.

## Rules

1. **Never write real credentials into test files, even as "fallback defaults."** Use `process.env.E2E_ADMIN_USERNAME` etc. and throw if unset — don't fall back to a real value "for convenience." A default that happens to be the real prod password is a leak the moment the file is committed.
2. **Never let a script dump raw API responses to a committed file** (e.g. a "QA report" json) without checking whether the response contains an auth token first. If a test/audit script logs full HTTP responses, redact `access_token`, `Authorization`, `password`, `api_key` fields before writing to disk.
3. **`.gitignore` must cover, in every repo in this project:** `*.env` (except `.env.example`), `*.key`, `*.pem`, `*.p12`, `credentials.json`, `*.session`, `*.db`. Check this whenever a new repo/worktree is created — it does not inherit from siblings.
4. **Git auth: never embed a token in a remote URL.** Use `gh auth login` (stored via OS credential manager) or SSH keys. If a token is ever found in a remote URL, treat it as already compromised — revoke on GitHub immediately, don't just edit the URL.
5. **If a credential leak is found:** report it, but do not rotate/revoke/delete anything without explicit user confirmation — rotating a live production credential is a user decision (it can break running sessions, other integrations, etc.), not something to do unprompted.

## Quick self-check before committing test/audit output

- Does this file contain anything matching `token`, `password`, `secret`, `key`, `Authorization`? If yes and it's a real value (not a placeholder), stop and redact before committing.
