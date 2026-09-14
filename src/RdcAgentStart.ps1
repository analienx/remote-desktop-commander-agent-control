# RDC agent starter - FULLY HIDDEN design.
# No console window is ever created: the agent inherits the caller's hidden
# console and all output goes to log files. Visible surface = status dashboard.
# Logs: %LOCALAPPDATA%\RDC-Agent\remote-output.log (+ .err.log)

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
foreach ($p in (Get-Process -Name cmd)) {
    $c = (Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $p.Id)).CommandLine
    if ($c -match 'desktop-commander remote') {
        Stop-Process -Id $p.Id -Force
        Write-Log ("closed legacy console host PID " + $p.Id)
    }
}

# 2) dedup: already running?
foreach ($p in (Get-Process -Name node)) {
    $cmdline = (Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $p.Id)).CommandLine
    if ($cmdline -match 'desktop-commander' -and $cmdline -match '\sremote\b') {
        Write-Log ("OK agent already running (PID " + $p.Id + ")")
        exit 0
    }
}

# 3) start hidden with captured output
$node = Join-Path $env:ProgramFiles 'nodejs\node.exe'
$indexJs = Join-Path $env:APPDATA 'npm\node_modules\@wonderwhy-er\desktop-commander\dist\index.js'
if (-not (Test-Path -LiteralPath $node))   { Write-Log 'ERROR node.exe not found'; exit 1 }
if (-not (Test-Path -LiteralPath $indexJs)){ Write-Log 'ERROR desktop-commander package not found'; exit 1 }

$outLog = Join-Path $logDir 'remote-output.log'
$errLog = Join-Path $logDir 'remote-output.err.log'

$proc = Start-Process -FilePath $node `
    -ArgumentList ('"' + $indexJs + '" remote') `
    -NoNewWindow `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -PassThru

if ($proc) {
    Write-Log ("Started hidden agent PID " + $proc.Id + " (output -> remote-output.log)")
    exit 0
} else {
    Write-Log 'ERROR failed to start agent'
    exit 1
}
