# TeleMon deploy verification (health + key feature markers).
# Usage: powershell -ExecutionPolicy Bypass -File scripts\verify.ps1 [-Service backend|frontend|all]

param(
    [ValidateSet("backend", "frontend", "all")]
    [string]$Service = "all"
)

$ErrorActionPreference = "Continue"
$fail = 0

function Check([string]$label, [scriptblock]$block) {
    try {
        & $block | Out-Null
        Write-Host "  [ok]   $label"
    } catch {
        Write-Host "  [FAIL] $label -> $($_.Exception.Message)"
        $script:fail++
    }
}

function Verify-Backend {
    Write-Host "=== verify backend ==="
    $repo = "C:\Dev\telegram-dashboard-backend"
    Check "health endpoint 200" {
        $h = docker exec telegram-dashboard-backend-backend-1 python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)" 2>$null
        if ($LASTEXITCODE -ne 0) { throw "health not ok" }
    }
    Check "RANDOM_REPLY interval matches origin/master" {
        $origin = (git -C $repo show "origin/master:app/scheduler/scheduler.py" 2>$null | Select-String "RANDOM_REPLY_INTERVAL_MINUTES = (\d+)").Matches[0].Groups[1].Value
        $dep = ShExec "telegram-dashboard-backend-backend-1" "grep -oE 'RANDOM_REPLY_INTERVAL_MINUTES = [0-9]+' app/scheduler/scheduler.py | head -1"
        $depNum = ($dep -replace "[^0-9]", "")
        if (-not $origin -or $depNum -ne $origin) { throw "deployed=$depNum origin=$origin" }
    }
    Check "keep_alive present" {
        $n = docker exec telegram-dashboard-backend-backend-1 grep -c "keep_alive" app/services/ai_chat_v2_service.py 2>$null
        if ([int]$n -lt 1) { throw "keep_alive missing" }
    }
    Check "exception handler present" {
        $n = docker exec telegram-dashboard-backend-backend-1 grep -c "_unhandled_exception_handler" app/main.py 2>$null
        if ([int]$n -lt 1) { throw "handler missing" }
    }
    Check "scheduler started in logs" {
        $logs = docker logs --since 5m telegram-dashboard-backend-backend-1 2>&1 | Out-String
        if ($logs -notmatch "scheduler_started") { throw "scheduler_started not found" }
    }
}

function ShExec([string]$container, [string]$script) {
    # Runs a POSIX script inside a container via a temp file (avoids the
    # PowerShell<->sh quoting/encoding minefield).
    $tmp = Join-Path $env:TEMP ("verify_" + [guid]::NewGuid().ToString("N").Substring(0, 8) + ".sh")
    [System.IO.File]::WriteAllText($tmp, $script, (New-Object System.Text.UTF8Encoding($false)))
    docker cp $tmp "$container`:/tmp/verify_run.sh" | Out-Null
    Remove-Item $tmp -Force
    $out = docker exec $container sh /tmp/verify_run.sh 2>$null
    return ($out -join "`n")
}

function Verify-Frontend {
    Write-Host "=== verify frontend ==="
    Check "GET / 200" {
        $r = Invoke-WebRequest -Uri "http://localhost/" -UseBasicParsing -TimeoutSec 15
        if ($r.StatusCode -ne 200) { throw "status $($r.StatusCode)" }
    }
    Check "GET /ai 200" {
        $r = Invoke-WebRequest -Uri "http://localhost/ai" -UseBasicParsing -TimeoutSec 15
        if ($r.StatusCode -ne 200) { throw "status $($r.StatusCode)" }
    }
    Check "ai-shell marker (continue banner) in bundle" {
        $n = ShExec "telegram-dashboard-backend-frontend-1" "grep -rl onContinueSession /app/.next/static 2>/dev/null | head -1"
        if (-not $n) { throw "marker missing" }
    }
    Check "landing /ai CTA in bundle" {
        $n = ShExec "telegram-dashboard-backend-frontend-1" "grep -rl '/ai' /app/.next/static/chunks/app/\(public\)/page-*.js 2>/dev/null | head -1"
        if (-not $n) { throw "CTA missing" }
    }
    Check "SmsOtpLoginForm fix (pl-4, no py-2.5)" {
        $c = ShExec "telegram-dashboard-backend-frontend-1" "grep -rlF 'min-w-[86px]' /app/.next/static/chunks 2>/dev/null | head -1"
        if (-not $c) { throw "chunk not found" }
        $old = ShExec "telegram-dashboard-backend-frontend-1" "grep -o 'pl-3 pr-7 py-2.5' '$c' 2>/dev/null"
        if ($old) { throw "old padding still present" }
        $new = ShExec "telegram-dashboard-backend-frontend-1" "grep -o 'pl-4 pr-7' '$c' 2>/dev/null"
        if (-not $new) { throw "new padding missing" }
    }
}

switch ($Service) {
    "backend"  { Verify-Backend }
    "frontend" { Verify-Frontend }
    "all"      { Verify-Backend; Verify-Frontend }
}

if ($fail -gt 0) {
    Write-Host "VERIFY FAILED: $fail check(s) failed"
    exit 1
}
Write-Host "VERIFY OK"
