# TeleMon pre-push hook installer.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\setup-hooks.ps1
# Writes a backend pre-push hook (frontend already runs pre-push checks via pnpm).

$ErrorActionPreference = "Stop"

function Write-Hook([string]$repo, [string]$hook, [string]$body) {
    $hookPath = Join-Path $repo ".git\hooks\$hook"
    New-Item -ItemType Directory -Path (Split-Path $hookPath) -Force | Out-Null
    # UTF-8 WITHOUT BOM: a BOM before #!/bin/sh makes git fail to spawn the hook.
    [System.IO.File]::WriteAllText($hookPath, $body, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "[hooks] wrote $hookPath"
}

$backendHook = @'
#!/bin/sh
# TeleMon pre-push: quick import sanity check before pushing to origin.
root="$(git rev-parse --show-toplevel)" || exit 1
cd "$root" || exit 1
if command -v python >/dev/null 2>&1; then
    PYTHONPATH=. python -c "import app.main" 2>/dev/null || { echo "[pre-push] backend import check FAILED"; exit 1; }
    echo "[pre-push] backend import OK"
fi
exit 0
'@

Write-Hook "C:\Dev\telegram-dashboard-backend" "pre-push" $backendHook
Write-Host "[hooks] done. Frontend already has pre-push checks via pnpm."
