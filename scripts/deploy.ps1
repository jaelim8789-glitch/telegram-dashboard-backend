# TeleMon local-Docker deploy helper (this PC IS the production server).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Service all            # build+deploy both (parallel build)
#   powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Service backend        # build+deploy one
#   powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Service frontend -Stage  # build+tag+push only (no live change)
#   powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Service frontend -Promote # deploy pre-staged :latest
#   powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Service all -Rollback   # restore :prev -> live
#   powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Service all -BlueGreen  # auto-rollback if verify fails
#
# Design highlights:
#   - Reused git worktrees (C:\Dev\_build_*) so Docker layer caches survive.
#   - -Service all builds backend+frontend in PARALLEL (Start-Job).
#   - Skips `compose up` when the freshly built image digest == running image (no-op).
#   - Version tags (<sha>-<ts>, :latest, :prev) + one-command rollback.
#   - Clears Redis singleton locks before `compose up` (avoids the scheduler-not-starting incident).
#   - Reloads nginx via HUP only when a service actually changed.
#   - Auto-verifies health + feature markers; -BlueGreen rolls back to :prev on failure.
#   - Optional Telegram/webhook alert via ALERT_WEBHOOK_URL in .env.
#   - Inline build-cache export so a future cold build reuses embedded cache.

param(
    [ValidateSet("backend", "frontend", "all")]
    [string]$Service = "all",
    [switch]$Rollback,
    [switch]$Push,
    [switch]$SkipVerify,
    [switch]$Stage,
    [switch]$Promote,
    [switch]$BlueGreen
)

# Continue (not Stop): native tools (git/docker) write normal chatter to stderr.
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

# --- .env helpers ---
$EnvVars = @{}
if (Test-Path "$ComposeDir\.env") {
    Get-Content "$ComposeDir\.env" | ForEach-Object {
        if ($_ -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { $EnvVars[$matches[1]] = $matches[2].Trim() }
    }
}
$RedisPass = $EnvVars["REDIS_PASSWORD"]
$AlertUrl  = $EnvVars["ALERT_WEBHOOK_URL"]

function Stopwatch-Show([string]$label, [System.Diagnostics.Stopwatch]$sw) {
    Write-Host "[timer] $label = $([math]::Round($sw.Elapsed.TotalSeconds,1))s"
}

function Notify([string]$msg) {
    if ($AlertUrl) {
        try { Invoke-RestMethod -Uri $AlertUrl -Method Post -ContentType "application/json" -Body (@{ text = "TeleMon $msg"; username = "TeleMon CI" } | ConvertTo-Json) -TimeoutSec 10 | Out-Null } catch { Write-Host "  (alert failed)" }
    }
}

function Update-BuildDir([string]$repo, [string]$dir) {
    if (-not (Test-Path $dir)) {
        git -C $repo worktree add --detach $dir origin/master | Out-Null
        Write-Host "  created build dir $dir"
    }
    git -C $repo worktree prune | Out-Null
    git -C $dir fetch origin master 2>$null | Out-Null
    git -C $dir checkout -f --detach origin/master 2>$null | Out-Null
    git -C $dir clean -fd | Out-Null
}

function Clear-SingletonLocks {
    if (-not $RedisPass) { return }
    $keys = docker exec $Redis redis-cli -a $RedisPass --no-auth-warning --scan --pattern "singleton_lock:*" 2>$null
    foreach ($k in $keys) { docker exec $Redis redis-cli -a $RedisPass --no-auth-warning DEL $k | Out-Null }
}

function Nginx-Reload {
    docker kill -s HUP $Nginx | Out-Null
}

function Running-Image([string]$container) {
    $id = docker inspect $container --format "{{.Image}}" 2>$null
    return $id
}

function Build-Image([string]$image, [string]$tag, [string]$ts, [string]$sha, [string]$dir) {
    # cache-to=inline embeds build cache so a cold/future build reuses it.
    docker build -t "$image`:$tag" -t "$image`:latest" `
        --label "commit=$sha" --label "built_at=$ts" `
        --cache-to=type=inline,mode=max `
        $dir
    return $LASTEXITCODE
}

function Deploy-Service([string]$service, [string]$repo, [string]$image, [string]$container, [string]$dir, [switch]$SkipBuild) {
    git -C $repo fetch origin master 2>$null | Out-Null
    $sha = git -C $repo rev-parse --short=7 origin/master
    $ts  = Get-Date -Format "yyyyMMdd-HHmmss"
    $tag = "$sha-$ts"

    Write-Host "=== $service $sha ($tag) ==="
    if (-not $SkipBuild) { Update-BuildDir $repo $dir }

    if (-not $Rollback -and -not $Promote -and -not $SkipBuild) {
        docker tag "$image`:latest" "$image`:prev" 2>$null
    }

    $builtId = $null
    if ($Rollback) {
        Write-Host "  ROLLBACK: :prev -> :latest"
        docker tag "$image`:prev" "$image`:latest" | Out-Null
    } elseif ($Promote) {
        Write-Host "  PROMOTE: deploying staged :latest"
    } elseif (-not $SkipBuild) {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $code = Build-Image $image $tag $ts $sha $dir
        if ($code -ne 0) { throw "$service build FAILED (exit $code)" }
        Stopwatch-Show "$service build" $sw
    }

    # ③ no-op: skip compose up when the running container is already on this
    # commit (the built_at timestamp label makes image IDs differ every build,
    # so compare the commit label instead).
    $changed = $true
    if (-not $Rollback -and -not $Promote) {
        $runCommit = (docker inspect $container --format "{{index .Config.Labels \"commit\"}}" 2>$null)
        if ($runCommit -eq $sha) {
            Write-Host "  no-op: running commit $sha already deployed"
            $changed = $false
        }
    }

    if ($Stage) {
        Write-Host "  STAGE: built+tagged, not deployed to live"
        if ($Push) { docker push "$image`:latest" | Out-Null; Write-Host "  pushed (staged)" }
        return
    }

    if ($changed) {
        Clear-SingletonLocks
        $sw2 = [System.Diagnostics.Stopwatch]::StartNew()
        Push-Location $ComposeDir
        docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps --wait $service
        if ($LASTEXITCODE -ne 0) { throw "$service compose up FAILED (exit $LASTEXITCODE)" }
        Pop-Location
        Stopwatch-Show "$service compose up --wait" $sw2
        Nginx-Reload
    }

    if (-not $SkipVerify) {
        & "$PSScriptRoot\verify.ps1" -Service $service
        if ($LASTEXITCODE -ne 0) {
            if ($BlueGreen) {
                Write-Host "  VERIFY FAILED — BlueGreen auto-rollback to :prev"
                docker tag "$image`:prev" "$image`:latest" | Out-Null
                docker compose -f "$ComposeDir\docker-compose.yml" -f "$ComposeDir\docker-compose.prod.yml" up -d --no-deps --wait $service 2>$null | Out-Null
                Notify "verify FAILED on $service — rolled back"
                throw "rolled back to :prev after verify failure"
            }
            throw "verify failed for $service"
        }
    }

    if ($Push) { docker push "$image`:latest" | Out-Null; Write-Host "  pushed $image ($tag)" }
    Write-Host "=== $service done: $($image):$($tag) ==="
}

$swAll = [System.Diagnostics.Stopwatch]::StartNew()

switch ($Service) {
    "backend" {
        Deploy-Service "backend" $ComposeDir $BackImage "telegram-dashboard-backend-backend-1" $BuildBack
    }
    "frontend" {
        Deploy-Service "frontend" $FrontendDir $FrontImage "telegram-dashboard-backend-frontend-1" $BuildFront
    }
    "all" {
        # ② parallel build: run both docker builds concurrently, then deploy sequentially.
        $repoB  = $ComposeDir; $imgB  = $BackImage;  $dirB  = $BuildBack
        $repoF  = $FrontendDir; $imgF  = $FrontImage; $dirF  = $BuildFront

        git -C $repoB fetch origin master 2>$null | Out-Null
        git -C $repoF fetch origin master 2>$null | Out-Null
        $shaB = git -C $repoB rev-parse --short=7 origin/master
        $shaF = git -C $repoF rev-parse --short=7 origin/master
        $ts   = Get-Date -Format "yyyyMMdd-HHmmss"
        Update-BuildDir $repoB $dirB
        Update-BuildDir $repoF $dirF
        docker tag "$imgB`:latest" "$imgB`:prev" 2>$null
        docker tag "$imgF`:latest" "$imgF`:prev" 2>$null

        $codeFileB = Join-Path $env:TEMP "build_code_backend.txt"
        $codeFileF = Join-Path $env:TEMP "build_code_frontend.txt"
        Remove-Item $codeFileB, $codeFileF -Force -ErrorAction SilentlyContinue

        $jb = Start-Job -ScriptBlock {
            param($image, $tag, $ts, $sha, $dir, $codeFile)
            docker build -t "$image`:$tag" -t "$image`:latest" --label "commit=$sha" --label "built_at=$ts" --cache-to=type=inline,mode=max $dir
            Set-Content -Path $codeFile -Value $LASTEXITCODE
        } -ArgumentList $imgB, "$shaB-$ts", $ts, $shaB, $dirB, $codeFileB

        $jf = Start-Job -ScriptBlock {
            param($image, $tag, $ts, $sha, $dir, $codeFile)
            docker build -t "$image`:$tag" -t "$image`:latest" --label "commit=$sha" --label "built_at=$ts" --cache-to=type=inline,mode=max $dir
            Set-Content -Path $codeFile -Value $LASTEXITCODE
        } -ArgumentList $imgF, "$shaF-$ts", $ts, $shaF, $dirF, $codeFileF

        $swB = [System.Diagnostics.Stopwatch]::StartNew()
        Wait-Job $jb, $jf | Out-Null
        Stopwatch-Show "parallel build (both)" $swB
        Remove-Job $jb, $jf -Force
        $codeB = if (Test-Path $codeFileB) { [int](Get-Content $codeFileB) } else { 1 }
        $codeF = if (Test-Path $codeFileF) { [int](Get-Content $codeFileF) } else { 1 }
        if ($codeB -ne 0) { throw "backend parallel build FAILED (exit $codeB)" }
        if ($codeF -ne 0) { throw "frontend parallel build FAILED (exit $codeF)" }

        # deploy sequentially (compose up must not overlap)
        Deploy-Service "backend"  $repoB $imgB "telegram-dashboard-backend-backend-1" $dirB -SkipBuild
        Deploy-Service "frontend" $repoF $imgF "telegram-dashboard-backend-frontend-1" $dirF -SkipBuild
    }
}

Stopwatch-Show "TOTAL deploy" $swAll
Notify "deploy complete ($Service)"
Write-Host "DEPLOY COMPLETE"
