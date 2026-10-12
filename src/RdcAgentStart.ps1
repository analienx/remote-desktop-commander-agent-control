# RDC agent starter - FULLY HIDDEN design.
# No console window is ever created: the agent inherits the caller's hidden
# console and all output goes to log files. Visible surface = status dashboard.
# Logs: %LOCALAPPDATA%\RDC-Agent\remote-output-<installation-id>.log (+ .err.log)

$ErrorActionPreference = 'SilentlyContinue'

$logDir = Join-Path $env:LOCALAPPDATA 'RDC-Agent'
if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$log = Join-Path $logDir 'agent.log'

function Write-Log([string]$msg) {
    $line = ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
    Add-Content -LiteralPath $log -Value $line
    if ((Get-Item -LiteralPath $log -ErrorAction SilentlyContinue).Length -gt 256KB) {
        Set-Content -LiteralPath $log -Value ('{0} log rotated' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
    }
}

# 1) clean up any legacy visible 'cmd /k' host windows from the old design
foreach ($p in @(Get-CimInstance Win32_Process -Filter "Name = 'cmd.exe'" -ErrorAction SilentlyContinue)) {
    if ($p.CommandLine -match '(?i)\s/k\s' -and $p.CommandLine -match 'desktop-commander remote') {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Log ("closed legacy console host PID " + $p.ProcessId)
    }
}

# 2) dedup: only an agent from the pinned canonical installation counts.
$activeCfg = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'rdc-active-install.json') -Raw | ConvertFrom-Json
$indexJs = Join-Path $activeCfg.package_root 'dist\index.js'
foreach ($p in @(Get-CimInstance Win32_Process -Filter "Name = 'node.exe'" -ErrorAction Stop)) {
    $cmdline = $p.CommandLine
    if ($cmdline -match [regex]::Escape($indexJs) -and $cmdline -match '\sremote\b') {
        Write-Log ("OK agent already running (PID " + $p.ProcessId + ")")
        exit 0
    }
}

# 3) start hidden with captured output
$node = Join-Path $env:ProgramFiles 'nodejs\node.exe'
if (-not (Test-Path -LiteralPath $node))   { Write-Log 'ERROR node.exe not found'; exit 1 }
if (-not (Test-Path -LiteralPath $indexJs)){ Write-Log 'ERROR desktop-commander package not found'; exit 1 }
# Fail closed if the pinned runtime is not patched with routing and write auditing.
$hookPath = Join-Path $PSScriptRoot 'rdc_active_hook.py'
if (-not (Test-Path -LiteralPath $hookPath)) { Write-Log 'ERROR RDC active hook missing'; exit 1 }
& python.exe $hookPath status *> $null
if ($LASTEXITCODE -ne 0) { Write-Log 'ERROR active RDC hook unhealthy'; exit 1 }

$outLog = Join-Path $logDir ('remote-output-' + $activeCfg.installation_id + '.log')
$errLog = Join-Path $logDir ('remote-output-' + $activeCfg.installation_id + '.err.log')

$proc = Start-Process -FilePath $node `
    -ArgumentList ('"' + $indexJs + '" remote') `
    -NoNewWindow `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -PassThru

if ($proc) {
    Write-Log ("Started hidden agent PID " + $proc.Id + " (output -> " + $outLog + ")")
    exit 0
} else {
    Write-Log 'ERROR failed to start agent'
    exit 1
}
