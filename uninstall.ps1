# uninstall.ps1 - removes RDC Agent Control (tasks, shortcut, installed files).
# Keeps logs and agent state under %LOCALAPPDATA%\RDC-Agent unless -PurgeData.
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'RDC-Control'),
    [switch]$PurgeData
)
$ErrorActionPreference = 'SilentlyContinue'

Write-Host '==> Unregistering scheduled tasks'
foreach ($t in 'RDC-Agent-Guardian', 'CRD-Watchdog') {
    Unregister-ScheduledTask -TaskName $t -Confirm:$false
    Write-Host ('    removed task: ' + $t)
}

Write-Host '==> Removing desktop shortcut'
$desktop = [Environment]::GetFolderPath('Desktop')
foreach ($lnk in 'RDC Agent Control.lnk', 'Agent Status.lnk') {
    $p = Join-Path $desktop $lnk
    if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force; Write-Host ('    removed: ' + $lnk) }
}

Write-Host '==> Removing installed tooling'
if (Test-Path -LiteralPath $InstallDir) {
    Remove-Item -LiteralPath $InstallDir -Recurse -Force
    Write-Host ('    removed: ' + $InstallDir)
}

if ($PurgeData) {
    Write-Host '==> Purging logs and guardian state'
    foreach ($d in (Join-Path $env:LOCALAPPDATA 'RDC-Agent'), (Join-Path $env:LOCALAPPDATA 'CRD-Watchdog')) {
        if (Test-Path -LiteralPath $d) { Remove-Item -LiteralPath $d -Recurse -Force; Write-Host ('    removed: ' + $d) }
    }
} else {
    Write-Host '==> Logs/state kept in %LOCALAPPDATA%\RDC-Agent (use -PurgeData to remove)'
}

Write-Host ''
Write-Host 'Uninstalled. The npm package @wonderwhy-er/desktop-commander was left in place.' -ForegroundColor Green
Write-Host 'Remove it with: npm uninstall -g @wonderwhy-er/desktop-commander' -ForegroundColor White
