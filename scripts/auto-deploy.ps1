# TeleMon auto-deploy: polls origin/master and deploys on new commits.
# Usage:
#   -CheckOnce   : check now and deploy if origin/master moved (for Task Scheduler)
#   (no switch)  : loop forever, checking every 180s (for background process)
#
# Scheduled task (recommended, no admin needed for a user task):
#   schtasks /create /tn "TeleMonAutoDeploy" /tr "powershell -ExecutionPolicy Bypass -File C:\Dev\telegram-dashboard-backend\scripts\auto-deploy.ps1 -CheckOnce" /sc minute /mo 5 /f

param([switch]$CheckOnce)

$ErrorActionPreference = "Continue"
$backRepo  = "C:\Dev\telegram-dashboard-backend"
$frontRepo = "C:\Dev\TeleMon-kiro"
$stateFile = Join-Path $env:TEMP "telemon-last-deploy.json"

function Load-State {
    $s = @{ backend = ""; frontend = "" }
    if (Test-Path $stateFile) {
        try {
            $o = Get-Content $stateFile -Raw | ConvertFrom-Json
            if ($o.backend)  { $s.backend  = [string]$o.backend }
            if ($o.frontend) { $s.frontend = [string]$o.frontend }
        } catch { }
    }
    return $s
}

function Save-State([hashtable]$s) {
    $s | ConvertTo-Json | Set-Content $stateFile
}

function Get-Sha([string]$repo) {
    git -C $repo fetch origin master -q 2>$null | Out-Null
    return (git -C $repo rev-parse --short=7 origin/master)
}

function Deploy-IfChanged([string]$service, [string]$repo, [hashtable]$state) {
    $sha = Get-Sha $repo
    $last = $state.$service
    if ($last -ne $sha) {
        Write-Host "[auto] $service changed: $last -> $sha ; deploying"
        & "$backRepo\scripts\deploy.ps1" -Service $service -Push -SkipVerify
        if ($?) {
            $state.$service = $sha
            Save-State $state
            Write-Host "[auto] $service deployed at $sha"
        } else {
            Write-Host "[auto] $service deploy FAILED — state kept, will retry"
        }
    } else {
        Write-Host "[auto] $service up to date ($sha)"
    }
}

$state = Load-State

if ($CheckOnce) {
    Deploy-IfChanged "backend"  $backRepo  $state
    Deploy-IfChanged "frontend" $frontRepo $state
} else {
    while ($true) {
        $state = Load-State
        Deploy-IfChanged "backend"  $backRepo  $state
        Deploy-IfChanged "frontend" $frontRepo $state
        Start-Sleep -Seconds 180
    }
}
