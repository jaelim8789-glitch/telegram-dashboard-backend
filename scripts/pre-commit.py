#!/usr/bin/env python3
"""Pre-commit hook for TeleMon backend.

Checks:
1. Python import check (from app.main import app)
2. Alembic single-head check (prevent multi-head commits)
3. Secret scanning (staged files, Python fallback + gitleaks if installed)
4. Run related tests (detects changed files and runs matching tests)

Install:  cp scripts/pre-commit.py .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
"""

import subprocess
import sys
import os
import re
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)

errors = 0

ALEMBIC_DIR = REPO_ROOT / "alembic"

def check_alembic_single_head():
    """Alembic  : heads  1 ."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        return  # alembic    (CI ) 

    heads = [line for line in result.stdout.strip().split("\n") if line.strip()]
    if len(heads) != 1:
        print(f" [alembic-heads]   ({len(heads)} head):")
        for h in heads:
            print(f"   - {h.strip()}")
        print("   alembic merge    .")
        return False
    print(f" [alembic-heads]  head  ({heads[0].split()[0]})")
    return True

SECRET_PATTERNS = [
    re.compile(r"^(?:\s*(?:export\s+)?(?:[A-Za-z_][A-Za-z0-9_]*\.)?(?:ADMIN_PASSWORD|NOWPAYMENTS_API_KEY|NOWPAYMENTS_IPN_SECRET|REDIS_PASSWORD)\s*=\s*.+)$", re.IGNORECASE),
    re.compile(r"(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}['\"]?", re.IGNORECASE),
]

SKIP_WORDS = ("example", "placeholder", "your_", "test", "changeme", "xxx")

def check_secrets():
    """Scan staged files for likely secrets (dependency-free fallback)."""
    global errors
    result = subprocess.run(
        "git diff --cached --name-only --diff-filter=ACMR",
        shell=True, capture_output=True, text=True, cwd=REPO_ROOT,
    )
    staged_files = [f for f in result.stdout.strip().split("\n") if f.strip()]
    if not staged_files:
        print(" [secrets]  no staged files")
        return True

    found = 0
    for f in staged_files:
        path = REPO_ROOT / f
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:4096]:
            continue
        try:
            content = data.decode("utf-8", errors="replace")
        except Exception:
            continue
        for pattern in SECRET_PATTERNS:
            for lineno, line in enumerate(content.split("\n"), 1):
                lowered = line.lower()
                if any(w in lowered for w in SKIP_WORDS):
                    continue
                if pattern.search(line):
                    found += 1
                    print(f"   - {f}:{lineno}  pattern: {pattern.pattern[:60]}  (value masked)")
    if found:
        print(f" [secrets]  {found} suspicious secret(s) detected in staged files. Refusing to commit.")
        errors += 1
        return False
    print(" [secrets]  no suspicious secrets found in staged files")
    return True

def check_gitleaks():
    """Best-effort gitleaks scan; skip silently if gitleaks is not installed."""
    if not shutil.which("gitleaks"):
        return True
    result = subprocess.run(
        "gitleaks git --pre-commit --redact",
        shell=True, capture_output=True, text=True, cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        print(" [gitleaks]  secrets detected:")
        print(result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout)
        print(result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr)
        return False
    print(" [gitleaks]  clean")
    return True

def run(cmd, label):
    global errors
    print(f" [{label}] ...", flush=True)
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=REPO_ROOT)
    if result.returncode != 0:
        print(f" [{label}] :")
        print(result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout)
        print(result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr)
        errors += 1
        return False
    print(f" [{label}] ")
    return True

# 1. Import check
print("=" * 60)
run(
    f"{sys.executable} -c \"import sys; sys.path.insert(0, '.'); from app.main import app; print('app loaded OK')\"",
    "import-check",
)

# 1b. Alembic  
print("-" * 60)
if not check_alembic_single_head():
    errors += 1

# 1c. Secrets  
print("-" * 60)
if not check_secrets():
    errors += 1
if not check_gitleaks():
    errors += 1

# 2. Get changed .py files
staged = subprocess.run(
    "git diff --cached --name-only --diff-filter=ACMR | grep '\\.py$' || true",
    shell=True, capture_output=True, text=True, cwd=REPO_ROOT,
).stdout.strip().split("\n")

changed_files = [f for f in staged if f and f.endswith(".py")]

if changed_files:
    print(f"\n  Python : {len(changed_files)}")
    for f in changed_files:
        print(f"   - {f}")

    # 3. Run matching tests
    test_patterns = set()
    for f in changed_files:
        stem = Path(f).stem
        # Map: app/api/xxx.py  tests/test_xxx.py
        # Map: app/services/xxx.py  tests/test_xxx.py
        # Map: app/models/xxx.py  tests/test_xxx.py
        test_file = f"tests/test_{stem}.py"
        if (REPO_ROOT / test_file).exists():
            test_patterns.add(test_file)

        # Also try app/api prefix
        if "app/api/" in f:
            name = f.replace("app/api/", "").replace(".py", "")
            test_file = f"tests/test_{name}.py"
            if (REPO_ROOT / test_file).exists():
                test_patterns.add(test_file)

    if test_patterns:
        test_cmd = f"{sys.executable} -m pytest {' '.join(sorted(test_patterns))} -q --tb=short -x 2>&1 | tail -20"
        run(test_cmd, "pytest (related)")
    else:
        print("       ")
else:
    print("\n  Python     ")

print("=" * 60)
if errors > 0:
    print(f"\n {errors}     .")
    sys.exit(1)
else:
    print("\n      .")
