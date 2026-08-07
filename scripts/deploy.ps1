# TeleMon local-Docker deploy helper (this PC IS the production server).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Service backend
#   powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Service frontend
#   powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Service all
#   powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Service frontend -Rollback
#   powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Service all -Push
#
# Design:
#   - Reuses a stable git worktree (C:\Dev\_build_backend / _build_frontend)
#     instead of creating+removing a fresh worktree per deploy, so Docker
#     layer caches (pip store, pnpm store, Next.js .next/cache) survive and
#     only changed files rebuild.
#   - Tags images as <repo>:<shortsha>-<timestamp>, :latest, :prev for easy
#     one-command rollback.
#   - Clears Redis singleton locks right before `compose up` so the old
#     container's still-renewing workers can't starve the new ones of the
#     scheduler/bot locks (the "scheduler never started" incident).
#   - Reloads nginx via HUP (no container restart / no dropped sockets).
#   - Auto-verifies health + key feature markers after deploy.
#   - Optionally pushes to GHCR (-Push).

param(
    [ValidateSet("backend", "frontend", "all")]
    [string]$Service = "all",
    [switch]$Rollback,
    [switch]$Push,
    [switch]$SkipVerify
)

# Continue (not Stop): native tools (git/docker) write normal chatter to
# stderr, and Stop turns every such line into a terminating NativeCommandError.
# Critical steps check $LASTEXITCODE explicitly instead.
$ErrorActionPreference = "Continue"
$env:DOCKER_BUILDKIT = "1"

$ComposeDir  = "C:\Dev\telegram-dashboard-backend"
$FrontendDir = "C:\Dev\TeleMon-kiro"
$BuildBack   = "C:\Dev\_build_backend"
$BuildFront  = "C:\Dev\_build_frontend"
$Nginx       = "telegram-dashboard-backend-nginx-1"
$Redis       = "telegram-dashboard-backend-redis-1"
$BackImage   = "ghcr.io/jaelim8789-glitch/telemon-backend"
$FrontImage  = "ghcr.io/jaelim8789-glitch/telemon-frontend"

$RedisPass = ""
if (Test-Path "$ComposeDir\.env") {
    $m = Select-String -Path "$ComposeDir\.env" -Pattern '^REDIS_PASSWORD=(.*)$'
    if ($m) { $RedisPass = $m.Matches[0].Groups[1].Value.Trim() }
}

function Stopwatch-Show([string]$label, [System.Diagnostics.Stopwatch]$sw) {
    $sec = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    Write-Host "[timer] $label = ${sec}s"
    return $sec
}

function Update-BuildDir([string]$repo, [string]$dir) {
    if (-not (Test-Path $dir)) {
        git -C $repo worktree add --detach $dir origin/master | Out-Null
        Write-Host "  created build dir $dir"
    }
    git -C $repo worktree prune | Out-Null
    git -C $dir fetch origin master 2>$null
    git -C $dir checkout -f --detach origin/master 2>$null
    git -C $dir clean -fd | Out-Null
}

function Clear-SingletonLocks {
    if (-not $RedisPass) { Write-Host "  (no redis password found, skipping lock clear)"; return }
    $keys = docker exec $Redis redis-cli -a $RedisPass --no-auth-warning --scan --pattern "singleton_lock:*" 2>$null
    foreach ($k in $keys) {
        docker exec $Redis redis-cli -a $RedisPass --no-auth-warning DEL $k | Out-Null
    }
    Write-Host "  cleared $($keys.Count) singleton lock(s)"
}

function Nginx-Reload {
    docker kill -s HUP $Nginx | Out-Null
    Write-Host "  nginx reloaded (HUP)"
}

function Deploy-Backend {
    git -C $ComposeDir fetch origin master 2>$null
    $sha = git -C $ComposeDir rev-parse --short=7 origin/master
    $ts  = Get-Date -Format "yyyyMMdd-HHmmss"
    $tag = "$sha-$ts"

    Write-Host "=== backend $sha ($tag) ==="
    Update-BuildDir $ComposeDir $BuildBack

    # tag current latest as prev BEFORE building new
    if (-not $Rollback) {
        docker tag "$BackImage`:latest" "$BackImage`:prev" 2>$null
    }

    if ($Rollback) {
        Write-Host "  ROLLBACK: restoring :prev -> :latest"
        docker tag "$BackImage`:prev" "$BackImage`:latest" | Out-Null
    } else {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        docker build -t "$BackImage`:$tag" -t "$BackImage`:latest" `
            --label "commit=$sha" --label "built_at=$ts" $BuildBack
        if ($LASTEXITCODE -ne 0) { throw "backend build FAILED (exit $LASTEXITCODE)" }
        Stopwatch-Show "backend build" $sw
    }

    Clear-SingletonLocks
    $sw2 = [System.Diagnostics.Stopwatch]::StartNew()
    Push-Location $ComposeDir
    docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps --wait backend
    if ($LASTEXITCODE -ne 0) { throw "backend compose up FAILED (exit $LASTEXITCODE)" }
    Pop-Location
    Stopwatch-Show "backend compose up --wait" $sw2

    Nginx-Reload

    if (-not $SkipVerify) {
        & "$PSScriptRoot\verify.ps1" -Service backend
    }
    if ($Push) {
        docker push "$BackImage`:latest" | Out-Null
        docker push "$BackImage`:$tag" | Out-Null
        Write-Host "  pushed $BackImage ($tag)"
    }
    Write-Host "=== backend done: $($BackImage):$($tag) ==="
}

function Deploy-Frontend {
    git -C $FrontendDir fetch origin master 2>$null
    $sha = git -C $FrontendDir rev-parse --short=7 origin/master
    $ts  = Get-Date -Format "yyyyMMdd-HHmmss"
    $tag = "$sha-$ts"

    Write-Host "=== frontend $sha ($tag) ==="
    Update-BuildDir $FrontendDir $BuildFront

    if (-not $Rollback) {
        docker tag "$FrontImage`:latest" "$FrontImage`:prev" 2>$null
    }

    if ($Rollback) {
        Write-Host "  ROLLBACK: restoring :prev -> :latest"
        docker tag "$FrontImage`:prev" "$FrontImage`:latest" | Out-Null
    } else {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        docker build -t "$FrontImage`:$tag" -t "$FrontImage`:latest" `
            --label "commit=$sha" --label "built_at=$ts" $BuildFront
        if ($LASTEXITCODE -ne 0) { throw "frontend build FAILED (exit $LASTEXITCODE)" }
        Stopwatch-Show "frontend build" $sw
    }

    $sw2 = [System.Diagnostics.Stopwatch]::StartNew()
    Push-Location $ComposeDir
    docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps --wait frontend
    if ($LASTEXITCODE -ne 0) { throw "frontend compose up FAILED (exit $LASTEXITCODE)" }
    Pop-Location
    Stopwatch-Show "frontend compose up --wait" $sw2

    Nginx-Reload

    if (-not $SkipVerify) {
        & "$PSScriptRoot\verify.ps1" -Service frontend
    }
    if ($Push) {
        docker push "$FrontImage`:latest" | Out-Null
        docker push "$FrontImage`:$tag" | Out-Null
        Write-Host "  pushed $FrontImage ($tag)"
    }
    Write-Host "=== frontend done: $($FrontImage):$($tag) ==="
}

switch ($Service) {
    "backend"  { Deploy-Backend }
    "frontend" { Deploy-Frontend }
    "all"      { Deploy-Backend; Deploy-Frontend }
}

Write-Host "DEPLOY COMPLETE"

