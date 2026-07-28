#!/bin/sh
# Fast API verification without spinning up a browser.
# Usage: ./scripts/quick-api-check.sh [base_url] [path]
#   ./scripts/quick-api-check.sh                              -> logs in, hits /api/accounts
#   ./scripts/quick-api-check.sh https://telemon.online /api/accounts/summary
#
# Reads admin creds from env (ADMIN_USERNAME/ADMIN_PASSWORD), falling back to
# the local dev defaults documented in .env.example. Never hardcode real
# prod credentials here — see .claude/skills/telemon-secret-hygiene.

BASE_URL="${1:-https://telemon.online}"
PATH_TO_CHECK="${2:-/api/accounts}"
USERNAME="${ADMIN_USERNAME:?set ADMIN_USERNAME env var}"
PASSWORD="${ADMIN_PASSWORD:?set ADMIN_PASSWORD env var}"

TOKEN=$(curl -s -X POST "$BASE_URL/api/admin/login" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\"}" \
  | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)

if [ -z "$TOKEN" ]; then
  echo "Login failed — no token returned. Check credentials / base URL."
  exit 1
fi

echo "== $BASE_URL$PATH_TO_CHECK =="
curl -s -H "Authorization: Bearer $TOKEN" "$BASE_URL$PATH_TO_CHECK" -w "\nHTTP %{http_code} in %{time_total}s\n"
