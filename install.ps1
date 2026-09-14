# install.ps1 - one-shot installer for RDC Agent Control.
# Installs tooling per-user (no elevation), registers scheduled tasks and
# creates a desktop shortcut for the status dashboard.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -SkipCrdWatchdog

param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'RDC-Control'),
    [switch]$SkipCrdWatchdog,
    [switch]$NoShortcut
)

$ErrorActionPreference = 'Stop'
$srcDir = Join-Path $PSScriptRoot 'src'

function Step([string]$msg) { Write-Host ("==> " + $msg) -ForegroundColor Cyan }
function Ok([string]$msg)   { Write-Host ("    " + $msg) -ForegroundColor Green }
function Warn([string]$msg) { Write-Host ("    " + $msg) -ForegroundColor Yellow }

if ($PSVersionTable.PSVersion.Major -lt 5 -or -not $IsWindows -and $PSVersionTable.PSVersion.Major -lt 6) { }
if (-not ($env:OS -eq 'Windows_NT')) { throw 'This installer runs on Windows only.' }

# ---- 1. node ----
Step 'Checking Node.js'
$node = Join-Path $env:ProgramFiles 'nodejs\node.exe'
if (-not (Test-Path -LiteralPath $node)) {
    Warn 'node.exe not found at %ProgramFiles%\nodejs\node.exe'
    Write-Host '    Install Node.js 18+ from https://nodejs.org and re-run this installer.' -ForegroundColor Yellow
    throw 'Node.js missing'
}
Ok ('Node found: ' + (& $node --version))

# ---- 2. desktop-commander package ----
Step 'Checking @wonderwhy-er/desktop-commander package'
$pkgIndex = Join-Path $env:APPDATA 'npm\node_modules\@wonderwhy-er\desktop-commander\dist\index.js'
if (Test-Path -LiteralPath $pkgIndex) {
    Ok 'package already installed'
} else {
    Write-Host '    installing (this can take a minute)...' -ForegroundColor DarkGray
    & npm install -g --allow-scripts=@wonderwhy-er/desktop-commander,sharp,puppeteer '@wonderwhy-er/desktop-commander'
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pkgIndex)) {
        throw 'npm install failed - install manually: npm i -g @wonderwhy-er/desktop-commander'
    }
    Ok 'package installed'
}

# ---- 3. copy tooling ----
Step ('Copying tooling to ' + $InstallDir)
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
Copy-Item -Path (Join-Path $srcDir '*') -Destination $InstallDir -Recurse -Force
$installed = Get-ChildItem -Path $InstallDir -File | Select-Object -ExpandProperty Name
Ok ('installed: ' + ($installed -join ', '))

# ---- 4. scheduled tasks ----
function Register-KeepAliveTask {
    param([string]$Name, [string]$Script, [int]$IntervalMin, [int]$ExecLimitMin, [string]$Description)
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $Script + '"')
    $logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $repTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $IntervalMin) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes $ExecLimitMin) `
        -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger @($logonTrigger, $repTrigger) `
        -Settings $settings -Principal $principal -Description $Description -Force | Out-Null
    Ok ('task registered: ' + $Name + ' (logon + every ' + $IntervalMin + ' min)')
}

Step 'Registering scheduled tasks (per-user, no elevation)'
Register-KeepAliveTask -Name 'RDC-Agent-Guardian' `
    -Script (Join-Path $InstallDir 'RdcGuardian.ps1') -IntervalMin 5 -ExecLimitMin 6 `
    -Description 'RDC Guardian: health check + self-healing for the desktop-commander remote agent.'
if (-not $SkipCrdWatchdog) {
    Register-KeepAliveTask -Name 'CRD-Watchdog' `
        -Script (Join-Path $InstallDir 'CrdWatchdog.ps1') -IntervalMin 15 -ExecLimitMin 6 `
        -Description 'Chrome Remote Desktop watchdog: chromoting service + one permanent minimized PWA window.'
}

# ---- 5. desktop shortcut ----
if (-not $NoShortcut) {
    Step 'Creating desktop shortcut'
    $desktop = [Environment]::GetFolderPath('Desktop')
    $wshell = New-Object -ComObject WScript.Shell
    $lnkPath = Join-Path $desktop 'RDC Agent Control.lnk'
    $sc = $wshell.CreateShortcut($lnkPath)
    $sc.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $sc.Arguments = ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + (Join-Path $InstallDir 'RdcStatus.ps1') + '"')
    $icon = Join-Path $InstallDir 'rdc-icon.ico'
    if (Test-Path -LiteralPath $icon) { $sc.IconLocation = ($icon + ',0') }
    $sc.Description = 'RDC Agent Control dashboard'
    $sc.Save()
    Ok ('shortcut created: ' + $lnkPath)
}

# ---- 6. first guardian run ----
Step 'Running guardian once (initial health check)'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $InstallDir 'RdcGuardian.ps1')
Ok ('guardian exit code: ' + $LASTEXITCODE + '  (0=healthy 1=started 2=restarted 3=offline 4=needs-you 5=unrecoverable)')

Write-Host ''
Write-Host 'RDC Agent Control installed.' -ForegroundColor Green
Write-Host 'First-time pairing (once):  desktop-commander remote' -ForegroundColor White
Write-Host 'Dashboard:                  "RDC Agent Control" on your desktop' -ForegroundColor White
if (-not $SkipCrdWatchdog) {
    Write-Host 'CRD: watch for one minimized "Chrome Remote Desktop" Edge window.' -ForegroundColor White
}
