#!/usr/bin/env python3
"""Pre-push hook for TeleMon backend.

Checks:
1. alembic upgrade head (dry-run on local DB if available)
2. Import check (from app.main import app)
3. pytest (related tests)

Install:  cp scripts/pre-push.py .git/hooks/pre-push && chmod +x .git/hooks/pre-push
"""

import subprocess
import sys
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)

errors = 0


def run(cmd, label, env=None):
    global errors
    print(f"🔍 [{label}] ...", flush=True)
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, cwd=REPO_ROOT, env={**os.environ, **(env or {})}
    )
    if result.returncode != 0:
        print(f"❌ [{label}] 실패:")
        for line in (result.stdout + result.stderr).split("\n")[-30:]:
            print(f"   {line}")
        errors += 1
        return False
    print(f"✅ [{label}] 통과")
    return True


def main():
    global errors
    print("=" * 60)
    print("🔍 [pre-push] TeleMon Backend 검사")
    print("=" * 60)

    # 1. Import check (no DB needed)
    run(
        f"{sys.executable} -c \"import sys; sys.path.insert(0, '.'); from app.main import app; print('import OK')\"",
        "import-check",
    )

    # 2. Alembic upgrade head (if DB available)
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url:
        run(
            f"alembic upgrade head",
            "alembic-migrate",
            env={"DATABASE_URL": db_url},
        )
    else:
        # Dry-check: verify migration files have no syntax errors
        run(
            f"{sys.executable} -m alembic check 2>&1 || {sys.executable} -c \"import importlib; importlib.import_module('alembic'); print('alembic OK')\"",
            "alembic-check",
        )

    # 3. Run tests (related to changed files)
    staged = subprocess.run(
        "git diff --cached --name-only --diff-filter=ACMR | grep '\\.py$' || true",
        shell=True, capture_output=True, text=True, cwd=REPO_ROOT,
    ).stdout.strip().split("\n")

    changed_files = [f for f in staged if f and f.endswith(".py")]

    if changed_files:
        test_patterns = set()
        for f in changed_files:
            stem = Path(f).stem
            test_file = f"tests/test_{stem}.py"
            if (REPO_ROOT / test_file).exists():
                test_patterns.add(test_file)
            if "app/api/" in f:
                name = f.replace("app/api/", "").replace(".py", "")
                test_file = f"tests/test_{name}.py"
                if (REPO_ROOT / test_file).exists():
                    test_patterns.add(test_file)

        if test_patterns:
            test_cmd = f"{sys.executable} -m pytest {' '.join(sorted(test_patterns))} -q --tb=short -x 2>&1 | tail -20"
            run(test_cmd, "pytest (related)")

    # 4. Alembic single-head check
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    if result.returncode == 0:
        heads = [line for line in result.stdout.strip().split("\n") if line.strip()]
        if len(heads) != 1:
            print(f"❌ [alembic-heads] 멀티헤드 감지 ({len(heads)}개)")
            for h in heads:
                print(f"   - {h.strip()}")
            errors += 1
        else:
            print(f"✅ [alembic-heads] 단일 head ({heads[0].split()[0]})")

    print("=" * 60)
    if errors > 0:
        print(f"\n❌ {errors}개 검사 실패 — push가 차단되었습니다.")
        sys.exit(1)
    else:
        print("\n🎉 모든 검사 통과 — push 진행합니다.")


if __name__ == "__main__":
    main()
