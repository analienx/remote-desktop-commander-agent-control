# Chrome Remote Desktop presence watchdog - "one permanent window" design.
#
# Architecture (verified empirically):
#   - chromoting service (AUTO_START, LocalSystem) = OS-level host anchor
#   - ONE minimized CRD PWA window = presence anchor (shows device online)
# This watchdog guarantees BOTH without ever creating duplicate windows:
#   1. service stopped  -> toast + logged (start needs admin; elevated repair manual)
#   2. PWA window gone  -> relaunch MINIMIZED (rate-limited 1x/30min)
# A window that already exists is NEVER touched - no focus stealing, no pops.
# Logs: %LOCALAPPDATA%\CRD-Watchdog\watchdog.log

param(
    [string]$AppId = 'cmkncekebbebpfilplodngbpllndjkfo'
)

$ErrorActionPreference = 'SilentlyContinue'

$logDir = Join-Path $env:LOCALAPPDATA 'CRD-Watchdog'
if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$log = Join-Path $logDir 'watchdog.log'

function Write-Log([string]$msg) {
    $line = ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
    Add-Content -LiteralPath $log -Value $line
    if ((Get-Item -LiteralPath $log -ErrorAction SilentlyContinue).Length -gt 256KB) {
        Set-Content -LiteralPath $log -Value ('{0} log rotated' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
    }
}

$crdProxy = Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge_proxy.exe'
$notify   = Join-Path $PSScriptRoot 'RdcNotify.ps1'

# ---------- 1. service anchor ----------
$svc = Get-Service -Name chromoting
if ($svc -and $svc.Status -ne 'Running') {
    Write-Log ("SERVICE NOT RUNNING (" + $svc.Status + ") - attempting start")
    Start-Service -Name chromoting
    Start-Sleep -Seconds 5
    $svc.Refresh()
    if ($svc.Status -eq 'Running') {
        Write-Log 'service started'
    } else {
        Write-Log 'WARN could not start service without elevation - alerting user'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $notify `
            -Title 'Chrome Remote Desktop' -Message 'Host service is not running - open Agent Status or restart PC' | Out-Null
    }
} else {
    Write-Log 'OK chromoting service running'
}

# ---------- 2. presence anchor: exactly ONE minimized PWA window ----------
# The PWA window lives inside the main msedge.exe process; msedge_proxy.exe is
# a transient launcher that exits after handoff. Detect by window title.
function Get-CrdPwa {
    $found = @()
    foreach ($p in (Get-Process -Name msedge,msedge_proxy)) {
        if ($p.MainWindowTitle -match 'Chrome Remote|Remote Access') { $found += $p }
    }
    return @($found)
}

$pwa = Get-CrdPwa
if ($pwa.Count -gt 0) {
    Write-Log ("OK CRD PWA present (PID " + (($pwa | ForEach-Object Id) -join ',') + ") - nothing to do")
    exit 0
}

# PWA gone -> relaunch minimized, rate-limited
$guardFile = Join-Path $logDir 'last-pwa-launch'
if (Test-Path -LiteralPath $guardFile) {
    $last = Get-Item -LiteralPath $guardFile | Select-Object -ExpandProperty LastWriteTime
    if (((Get-Date) - $last).TotalMinutes -lt 30) {
        Write-Log 'PWA relaunch skipped (rate limit 30 min)'
        exit 2
    }
}
Set-Content -LiteralPath $guardFile -Value (Get-Date -Format 'o')

if (-not (Test-Path -LiteralPath $crdProxy)) { Write-Log 'ERROR msedge_proxy.exe not found'; exit 1 }

$relaunchArgs = @(
    '--profile-directory=Default',
    ('--app-id=' + $AppId),
    '--app-url=https://remotedesktop.google.com/?entry-point=pwa&lfhs=2',
    '--app-run-on-os-login-mode=windowed',
    '--app-launch-source=19',
    '--start-minimized'
)
$started = Start-Process -FilePath $crdProxy -ArgumentList $relaunchArgs `
    -WorkingDirectory (Split-Path -Parent $crdProxy) -WindowStyle Minimized -PassThru
if ($started) {
    Write-Log ("Launched single minimized CRD PWA window (PID " + $started.Id + ")")
    exit 0
} else {
    Write-Log 'ERROR failed to launch CRD PWA'
    exit 1
}
