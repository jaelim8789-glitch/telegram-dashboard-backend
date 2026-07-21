#!/usr/bin/env python3
"""Pre-commit hook for TeleMon backend.

Checks:
1. Python import check (from app.main import app)
2. Run related tests (detects changed files and runs matching tests)

Install:  cp scripts/pre-commit.py .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
"""

import subprocess
import sys
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)

errors = 0

def run(cmd, label):
    global errors
    print(f"🔍 [{label}] ...", flush=True)
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=REPO_ROOT)
    if result.returncode != 0:
        print(f"❌ [{label}] 실패:")
        print(result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout)
        print(result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr)
        errors += 1
        return False
    print(f"✅ [{label}] 통과")
    return True

# 1. Import check
print("=" * 60)
run(
    f"{sys.executable} -c \"import sys; sys.path.insert(0, '.'); from app.main import app; print('app loaded OK')\"",
    "import-check",
)

# 2. Get changed .py files
staged = subprocess.run(
    "git diff --cached --name-only --diff-filter=ACMR | grep '\\.py$' || true",
    shell=True, capture_output=True, text=True, cwd=REPO_ROOT,
).stdout.strip().split("\n")

changed_files = [f for f in staged if f and f.endswith(".py")]

if changed_files:
    print(f"\n📂 변경된 Python 파일: {len(changed_files)}개")
    for f in changed_files:
        print(f"   - {f}")

    # 3. Run matching tests
    test_patterns = set()
    for f in changed_files:
        stem = Path(f).stem
        # Map: app/api/xxx.py → tests/test_xxx.py
        # Map: app/services/xxx.py → tests/test_xxx.py
        # Map: app/models/xxx.py → tests/test_xxx.py
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
        print("📭 관련 테스트 파일 없음 — 테스트 스킵")
else:
    print("\n📭 변경된 Python 파일 없음 — 테스트 스킵")

print("=" * 60)
if errors > 0:
    print(f"\n❌ {errors}개 검사 실패 — 커밋이 차단되었습니다.")
    sys.exit(1)
else:
    print("\n🎉 모든 검사 통과 — 커밋 진행합니다.")
