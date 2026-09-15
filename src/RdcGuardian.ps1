# RDC Guardian - full-stack health check + self-healing for the Remote Desktop Commander agent.
#
# Checks: environment -> process (dedup) -> relay connection -> session readiness
# Healing: L1 start-if-missing, L2 restart-if-unhealthy (cooldown), L3 alert-if-needs-user
# State:   %LOCALAPPDATA%\RDC-Agent\guardian-state.json
# Exit codes: 0=healthy 1=started 2=restarted 3=network-down 4=needs-user-action 5=unrecoverable

$ErrorActionPreference = 'SilentlyContinue'

# ---------- config ----------
$dcDir     = Join-Path $env:LOCALAPPDATA 'RDC-Agent'
$guardLog  = Join-Path $dcDir 'guardian.log'
$outLog    = Join-Path $dcDir 'remote-output.log'
$stateFile = Join-Path $dcDir 'guardian-state.json'
$report    = Join-Path $dcDir 'guardian-status.json'
$starter   = Join-Path $PSScriptRoot 'RdcAgentStart.ps1'
$notify    = Join-Path $PSScriptRoot 'RdcNotify.ps1'
$pkgIndex  = Join-Path $env:APPDATA 'npm\node_modules\@wonderwhy-er\desktop-commander\dist\index.js'
$relayHost = 'mcp.desktopcommander.app'
$restartCooldownMin = 10
$bootGraceSec       = 120

if (-not (Test-Path $dcDir)) { New-Item -ItemType Directory -Path $dcDir -Force | Out-Null }

function Write-Log([string]$msg) {
    Add-Content -LiteralPath $guardLog -Value ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
    if ((Get-Item -LiteralPath $guardLog -ErrorAction SilentlyContinue).Length -gt 256KB) {
        Set-Content -LiteralPath $guardLog -Value ('{0} log rotated' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
    }
}

# ---------- state ----------
$state = @{ ConsecutiveFailures = 0; LastRestart = [datetime]::MinValue; LastStatus = 'init'; LastCheck = (Get-Date) }
if (Test-Path -LiteralPath $stateFile) {
    try { $s = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
          $state.ConsecutiveFailures = [int]$s.ConsecutiveFailures
          $state.LastRestart = [datetime]$s.LastRestart
          $state.LastStatus = $s.LastStatus } catch { }
}
function Save-State([string]$status) {
    $state.LastStatus = $status
    $state.LastCheck = Get-Date
    $state | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Force
    try {
        @{ Timestamp = (Get-Date -Format 'o'); Status = $status
           ConsecutiveFailures = $state.ConsecutiveFailures
           LastRestart = $state.LastRestart.ToString('o') } |
            ConvertTo-Json | Set-Content -LiteralPath $report -Force
    } catch { }
}
function Try-Restart([string]$reason) {
    if (((Get-Date) - $state.LastRestart).TotalMinutes -lt $restartCooldownMin) {
        Write-Log ("RESTART SKIPPED (cooldown): " + $reason)
        return $false
    }
    Write-Log ("RESTART: " + $reason)
    foreach ($p in (Get-Process -Name node)) {
        $c = (Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $p.Id)).CommandLine
        if ($c -match 'desktop-commander') { Stop-Process -Id $p.Id -Force }
    }
    foreach ($p in (Get-Process -Name cmd)) {
        $c = (Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $p.Id)).CommandLine
        if ($c -match 'desktop-commander remote') { Stop-Process -Id $p.Id -Force }
    }
    Start-Sleep -Seconds 3
    Invoke-RdcHidden -File 'powershell.exe' -Arguments @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $starter + '"')) | Out-Null
    $state.LastRestart = Get-Date
    return $true
}

# Shared headless child-process launcher.
# Child powershell.exe invocations MUST NOT flash a console (conhost/cmd) window.
# The '&' operator inherits the caller's window style, which is unreliable when
# launched from Task Scheduler or from a non-console parent - so child processes
# are started explicitly headless here (the fix for the random cmd-window pops).
# Returns a small object: ExitCode / Output / Error.
function Invoke-RdcHidden {
    param([string]$File, [string[]]$Arguments = @())
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $File
        $psi.Arguments = ($Arguments -join ' ')
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        $out = $p.StandardOutput.ReadToEnd()
        $err = $p.StandardError.ReadToEnd()
        $p.WaitForExit(180000) | Out-Null
        return [pscustomobject]@{ ExitCode = $p.ExitCode; Output = $out; Error = $err }
    } catch {
        return [pscustomobject]@{ ExitCode = -1; Output = ''; Error = $_.Exception.Message }
    }
}

# ---------- 1. environment ----------
if (-not (Test-Path -LiteralPath $pkgIndex)) {
    Write-Log 'FATAL desktop-commander package missing - reinstall needed'
    Save-State 'FATAL-PACKAGE-MISSING'; exit 5
}
$tcp = New-Object Net.Sockets.TcpClient
$netOk = $tcp.ConnectAsync($relayHost, 443).Wait(5000) -and $tcp.Connected
$tcp.Close()
if (-not $netOk) {
    Write-Log 'NETWORK-DOWN: relay unreachable - no restart thrash while offline'
    Save-State 'NETWORK-DOWN'; exit 3
}

# ---------- 2. process (dedup + presence) ----------
function Get-Agents {
    $found = @()
    foreach ($p in (Get-Process -Name node)) {
        $c = (Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $p.Id)).CommandLine
        if ($c -match 'desktop-commander' -and $c -match '\sremote\b') { $found += $p }
    }
    return @($found)
}

$agents = Get-Agents
if ($agents.Count -gt 1) {
    Write-Log ("DEDUP: " + $agents.Count + " agents found - keeping newest")
    foreach ($old in ($agents | Select-Object -First ($agents.Count - 1))) {
        Stop-Process -Id $old.Id -Force
        Write-Log ("  killed duplicate PID " + $old.Id)
    }
    $agents = Get-Agents
}

if ($agents.Count -eq 0) {
    Write-Log 'AGENT MISSING - starting'
    Invoke-RdcHidden -File 'powershell.exe' -Arguments @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $starter + '"')) | Out-Null
    $state.LastRestart = Get-Date
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 10
        if ((Test-Path -LiteralPath $outLog) -and (Select-String -LiteralPath $outLog -Pattern 'Device ready' -Quiet)) {
            Write-Log 'RECOVERED (started, session ready)'
            $state.ConsecutiveFailures = 0; Save-State 'OK-STARTED'; exit 1
        }
    }
    $state.ConsecutiveFailures++; Save-State 'STARTED-BUT-NOT-READY'
    Write-Log 'WARN started but not ready in 90s - will re-check next cycle'
    exit 2
}

# ---------- 3. relay connection ----------
$agent = $agents[-1]
$conns = Get-NetTCPConnection -OwningProcess $agent.Id -State Established -ErrorAction SilentlyContinue
$uptimeMin = ((Get-Date) - $agent.StartTime).TotalMinutes

if (-not $conns) {
    if ($uptimeMin -lt ($bootGraceSec / 60)) {
        Write-Log ("agent PID " + $agent.Id + " booting - grace")
        Save-State 'BOOTING'; exit 0
    }
    $state.ConsecutiveFailures++
    if (Try-Restart ("agent PID " + $agent.Id + " has NO relay connection after " + [math]::Round($uptimeMin,1) + " min")) {
        Save-State 'RESTARTED-NO-CONNECTION'; exit 2
    }
    Save-State 'NO-CONNECTION-COOLDOWN'; exit 2
}

# ---------- 4. session readiness ----------
$ready = $false; $pairingNeeded = $false
if (Test-Path -LiteralPath $outLog) {
    $ready = Select-String -LiteralPath $outLog -Pattern 'Device ready' -Quiet
    $pairingNeeded = Select-String -LiteralPath $outLog -Pattern 'Verify your device|Verify Device|pairing code' -Quiet
}
if ($ready) {
    if ($state.ConsecutiveFailures -gt 0) { Write-Log 'agent recovered - resetting failure counter' }
    $state.ConsecutiveFailures = 0
    Save-State 'OK'
    Write-Log ("OK agent PID " + $agent.Id + " connected + session ready")
    exit 0
}
if ($uptimeMin -lt ($bootGraceSec / 60)) {
    Write-Log "agent connected, still booting - grace"
    Save-State 'BOOTING'; exit 0
}
if ($pairingNeeded) {
    if ($state.ConsecutiveFailures -eq 0 -and (Try-Restart 'agent stuck at pairing prompt')) {
        $state.ConsecutiveFailures++
        Save-State 'NEEDS-USER-VERIFICATION-RESTARTED'
        Write-Log 'NEEDS USER ACTION: pairing prompt - user must verify code in browser/console'
        Invoke-RdcHidden -File 'powershell.exe' -Arguments @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $notify + '"'), '-Title', '"RDC Agent"', '-Message', '"Device verification needed - click the toast or open Agent Status"') | Out-Null
        exit 4
    }
    Save-State 'NEEDS-USER-VERIFICATION'
    Write-Log 'NEEDS USER ACTION: pairing still unverified (restart did not help) - no thrash'
    exit 4
}
$state.ConsecutiveFailures++
if ($state.ConsecutiveFailures -ge 3) {
    Write-Log ('UNRECOVERABLE after ' + $state.ConsecutiveFailures + ' failures - manual inspection needed')
    Invoke-RdcHidden -File 'powershell.exe' -Arguments @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $notify + '"'), '-Title', '"RDC Agent"', '-Message', '"Automatic recovery failed - open Agent Status for details"') | Out-Null
    Save-State 'UNRECOVERABLE'; exit 5
}
if (Try-Restart ("agent connected but not ready (" + [math]::Round($uptimeMin,1) + " min uptime, log lacks Device ready)")) {
    Save-State 'RESTARTED-NOT-READY'; exit 2
}
Save-State 'UNHEALTHY-COOLDOWN'; exit 2
