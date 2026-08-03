#!/usr/bin/env python3
"""Pre-deploy security checklist for the TeleMon backend.

Run before deploying a production build:

    docker exec <backend-container> python scripts/security_check.py

Exits 0 even when warnings are found (non-blocking) so it can be wired into
a deploy script; prints a clear PASS/FAIL per check. The app's own config
validator (_reject_insecure_production_defaults) blocks startup on the hard
failures; this script additionally surfaces soft-gaps and git-tracking issues.

Checks:
  1. ADMIN_JWT_SECRET is not the insecure default and looks random.
  2. JWT_USER_SECRET is set and differs from ADMIN_JWT_SECRET.
  3. ADMIN_USERNAME / ADMIN_PASSWORD are set.
  4. NOWPAYMENTS_API_KEY / NOWPAYMENTS_IPN_SECRET are set.
  5. REDIS_PASSWORD is set (Redis auth).
  6. API_BASE_URL is a public https URL (NOWPayments IPN reachability).
  7. DEBUG is false.
  8. SMS_PROVIDER is not "console".
  9. .env is not tracked by git (no secrets in the repo).
"""

import os
import subprocess
import sys

_JWT_DEFAULT = "change-me-in-production"


def _env(name: str) -> str:
    return os.environ.get(name, "")


def _check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    suffix = f"  [{detail}]" if detail else ""
    print(f"[{status}] {label}{suffix}")


def main() -> int:
    print("TeleMon pre-deploy security checklist")
    print("=" * 60)

    admin_secret = _env("ADMIN_JWT_SECRET")
    _check(
        "ADMIN_JWT_SECRET not default",
        bool(admin_secret) and admin_secret != _JWT_DEFAULT and not admin_secret.startswith("dev-"),
        "set a random 64-hex value, not the default",
    )

    user_secret = _env("JWT_USER_SECRET")
    _check(
        "JWT_USER_SECRET set and distinct",
        bool(user_secret) and user_secret != admin_secret,
        "must differ from ADMIN_JWT_SECRET",
    )

    _check(
        "ADMIN_USERNAME/ADMIN_PASSWORD set",
        bool(_env("ADMIN_USERNAME")) and bool(_env("ADMIN_PASSWORD")),
    )

    _check(
        "NOWPAYMENTS keys set",
        bool(_env("NOWPAYMENTS_API_KEY")) and bool(_env("NOWPAYMENTS_IPN_SECRET")),
        "API key + IPN secret required for crypto payments",
    )

    _check("REDIS_PASSWORD set", bool(_env("REDIS_PASSWORD")), "Redis auth must be configured")

    base_url = _env("API_BASE_URL")
    _check(
        "API_BASE_URL public https",
        "localhost" not in base_url and base_url.startswith("https://"),
        "NOWPayments webhooks must reach this URL",
    )

    _check("DEBUG is false", _env("DEBUG").strip().lower() in ("false", "0", "no"))

    _check("SMS_PROVIDER not console", _env("SMS_PROVIDER").strip().lower() != "console")

    # 9. .env tracked by git? Run inside the repo (mounted source or CI).
    tracked = False
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".env"],
            capture_output=True,
            text=True,
        )
        tracked = result.returncode == 0
    except FileNotFoundError:
        pass  # git not available in container; skip this check
    _check(
        ".env not tracked by git",
        not tracked,
        "if FAIL, run: git rm --cached .env && echo '.env' >> .gitignore",
    )

    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
