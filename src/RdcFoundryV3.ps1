# RdcFoundryV3 - RDC as Foundry v3 transport/health boundary and bulk MCP consumer.
#
# Role (see docs/FOUNDRY_V3_RDC.md and analienx/config PR #37 / FOUNDRY_CONTRACT.md):
#   - RDC is a TRANSPORT + local-health boundary and a BULK READ consumer of the
#     Agent Interop Gateway (analienx/agent-interop-gateway PR #6) Foundry v3 API.
#   - Reads: cursor/paginated bulk reads for projects, jobs, attempts, activity,
#     approvals, artifacts, evidence and health through the gateway /v3/* routes.
#     RDC caches nothing authoritative and duplicates no Foundry job state.
#   - Writes: ONLY named typed host operations in the closed set $RdcHostOps
#     (local guardian/agent/CRD/diagnostics actions). Each write validates its
#     typed params, carries an idempotency key, and returns an explicit policy
#     result. General shell/filesystem mutation is NOT exposed here.
#   - RDC never owns: model/account routing (Cline Model Optimizer), scheduling,
#     goal state (Pi/Codex), or Foundry job lifecycle (prepare/attach/execute/
#     cancel/quarantine belong to Foundry via the gateway, not to RDC).
#
# This file defines functions only (dot-source it). It performs no action on load
# and changes none of the existing guardian/watchdog/starter/dashboard behavior.
#
#   . (Join-Path $PSScriptRoot 'RdcFoundryV3.ps1')
#
# Requires Windows PowerShell 5.1+.

# ---------- closed vocabularies (authoritative) ----------

# Bulk-read surfaces from supervisor/mcp/FOUNDRY_CONTRACT.md (config PR #37),
# served by the gateway as GET /v3/{resource}?cursor&limit&job_id.
$script:FoundryBulkResources = @(
    'projects', 'jobs', 'attempts', 'activity',
    'approvals', 'artifacts', 'evidence', 'health'
)

# Row fields per resource, from supervisor/mcp/foundry-resources.schema.json.
# Used to document/validate bulk-read results; RDC never invents new columns.
$script:FoundryResourceFields = @{
    projects  = @('project', 'execution_mode', 'repository', 'ref', 'readiness', 'required_artifact', 'last_job')
    jobs      = @('job_id', 'objective', 'agent', 'runtime', 'project', 'state', 'progress', 'elapsed_ms', 'cancellation', 'evidence')
    attempts  = @('attempt_id', 'job_id', 'generation', 'state', 'actor', 'source_digest', 'artifact_digests', 'policy_hash', 'cursor')
    activity  = @('timestamp', 'project', 'job_id', 'attempt_id', 'agent', 'source', 'action', 'outcome', 'correlation_id')
    artifacts = @('schema', 'kind', 'payload_digest', 'payload_bytes', 'producer', 'source_repo', 'source_commit', 'lock_digest', 'platform', 'arch', 'toolchain', 'built_at', 'retention', 'lifecycle_policy', 'provenance', 'verify')
    approvals = @('summary', 'target', 'payload_hash', 'reason', 'effect', 'rollback', 'expiry', 'requester')
    health    = @('plane', 'available', 'build', 'policy_revision', 'reason')
    evidence  = @('job_id', 'name', 'digest', 'reference')
}

# Foundry v3 terminal states (agent-foundry PR #2). Read-only reference so RDC
# can interpret job status without owning the state machine.
$script:FoundryTerminalStates = @(
    'succeeded', 'failed', 'cancelled', 'interrupted', 'unknown_outcome', 'quarantined'
)

# Fields owned by Cline Model Optimizer. Rejected on every RDC request body,
# mirroring the gateway's 422 (FORBIDDEN_V3_FIELDS in foundry.py).
$script:ForbiddenRoutingFields = @(
    'model', 'model_name', 'model_id',
    'account', 'account_alias', 'account_id',
    'cost_tier', 'cost', 'price', 'quota',
    'routing', 'route'
)

# Extra fields RDC must never accept: scheduling / goal-state / Foundry job
# lifecycle ownership that belongs to Pi/Codex/Foundry, not to RDC.
$script:ForbiddenOwnershipFields = @(
    'schedule', 'scheduler', 'cron',
    'goal', 'goal_id', 'plan', 'subagent',
    'command_profile', 'source_digest', 'manifest'
)

# Closed set of named typed host operations RDC may perform as writes.
# Each entry: description, allowed param names, handler function name.
$script:RdcHostOps = @{
    'rdc.run_guardian' = @{
        Description = 'Run the RDC guardian heal cycle now (L1 start / L2 restart / L3 toast).'
        Params      = @()
        Handler     = 'Invoke-RdcOpRunGuardian'
    }
    'rdc.restart_agent' = @{
        Description = 'Stop duplicate/stale agent processes and start one hidden agent via RdcAgentStart.'
        Params      = @('reason')
        Handler     = 'Invoke-RdcOpRestartAgent'
    }
    'rdc.collect_diagnostics' = @{
        Description = 'Read-only snapshot: guardian status, agent PID/relay/uptime, log tails.'
        Params      = @('log_tail_lines')
        Handler     = 'Invoke-RdcOpCollectDiagnostics'
    }
    'crd.ensure_presence' = @{
        Description = 'Run the CRD watchdog once (service anchor + one minimized PWA window).'
        Params      = @()
        Handler     = 'Invoke-RdcOpEnsureCrdPresence'
    }
    'host.read_health_snapshot' = @{
        Description = 'Local health point-read: guardian report plus gateway /v3/health when reachable.'
        Params      = @()
        Handler     = 'Invoke-RdcOpHealthSnapshot'
    }
}

# ---------- config ----------

function Get-RdcFoundryConfig {
    <#
    .SYNOPSIS
        Resolve gateway connection settings. Pure read, no network I/O.
        Precedence: explicit args > env vars > %LOCALAPPDATA%\RDC-Agent\foundry-gateway.json > defaults.
    #>
    param(
        [string]$BaseUrl = '',
        [string]$Token = '',
        [string]$ConfigFile = ''
    )
    if (-not $ConfigFile) {
        $ConfigFile = Join-Path $env:LOCALAPPDATA 'RDC-Agent\foundry-gateway.json'
    }
    $fileBase = ''; $fileTokenPath = ''
    if (Test-Path -LiteralPath $ConfigFile) {
        try {
            $j = Get-Content -LiteralPath $ConfigFile -Raw | ConvertFrom-Json
            if ($j.base_url) { $fileBase = [string]$j.base_url }
            if ($j.token_file) { $fileTokenPath = [string]$j.token_file }
        } catch { }
    }
    if (-not $BaseUrl) { $BaseUrl = $env:RDC_FOUNDRY_GATEWAY_URL }
    if (-not $BaseUrl) { $BaseUrl = $fileBase }
    if (-not $BaseUrl) { $BaseUrl = 'http://127.0.0.1:8000' }
    $BaseUrl = $BaseUrl.TrimEnd('/')
    if (-not $Token) { $Token = $env:RDC_FOUNDRY_TOKEN }
    if (-not $Token -and $fileTokenPath -and (Test-Path -LiteralPath $fileTokenPath)) {
        try { $Token = (Get-Content -LiteralPath $fileTokenPath -Raw).Trim() } catch { }
    }
    return [pscustomobject]@{
        BaseUrl    = $BaseUrl
        HasToken   = (-not [string]::IsNullOrEmpty($Token))
        ConfigFile = $ConfigFile
    }
}

function Get-RdcFoundryToken {
    <#
    .SYNOPSIS
        Resolve the bearer token without ever logging it. Internal helper.
    #>
    param([string]$Token = '', [string]$ConfigFile = '')
    if ($Token) { return $Token }
    if ($env:RDC_FOUNDRY_TOKEN) { return $env:RDC_FOUNDRY_TOKEN }
    if (-not $ConfigFile) { $ConfigFile = Join-Path $env:LOCALAPPDATA 'RDC-Agent\foundry-gateway.json' }
    if (Test-Path -LiteralPath $ConfigFile) {
        try {
            $j = Get-Content -LiteralPath $ConfigFile -Raw | ConvertFrom-Json
            if ($j.token_file -and (Test-Path -LiteralPath ([string]$j.token_file))) {
                return (Get-Content -LiteralPath ([string]$j.token_file) -Raw).Trim()
            }
        } catch { }
    }
    return ''
}

# ---------- validation / hashing ----------

function Test-FoundryForbiddenFields {
    <#
    .SYNOPSIS
        Throw when a request body carries router-owned or non-RDC-owned fields.
        Returns $true when clean.
    #>
    param([hashtable]$Body = @{})
    $hits = New-Object 'System.Collections.Generic.List[string]'
    function Find-ForbiddenField {
        param($Value, [string]$Prefix, $Hits)
        if ($Value -is [System.Collections.IDictionary]) {
            foreach ($k in $Value.Keys) {
                $name = [string]$k
                $path = if ($Prefix) { $Prefix + '.' + $name } else { $name }
                if (($script:ForbiddenRoutingFields -contains $name) -or
                    ($script:ForbiddenOwnershipFields -contains $name)) {
                    [void]$Hits.Add($path)
                }
                Find-ForbiddenField -Value $Value[$k] -Prefix $path -Hits $Hits
            }
        } elseif ($Value -is [System.Collections.IEnumerable] -and -not ($Value -is [string])) {
            foreach ($item in $Value) { Find-ForbiddenField -Value $item -Prefix $Prefix -Hits $Hits }
        } elseif ($null -ne $Value -and
                  ($Value.PSObject.TypeNames -contains 'System.Management.Automation.PSCustomObject')) {
            foreach ($p in $Value.PSObject.Properties) {
                $path = if ($Prefix) { $Prefix + '.' + $p.Name } else { $p.Name }
                if (($script:ForbiddenRoutingFields -contains $p.Name) -or
                    ($script:ForbiddenOwnershipFields -contains $p.Name)) {
                    [void]$Hits.Add($path)
                }
                Find-ForbiddenField -Value $p.Value -Prefix $path -Hits $Hits
            }
        }
    }
    Find-ForbiddenField -Value $Body -Prefix '' -Hits $hits
    $hit = @($hits | Sort-Object -Unique)
    if ($hit.Count -gt 0) {
        throw ("RDC rejects forbidden fields ({0}); model/account routing belongs to " -f ($hit -join ', ') +
            'Cline Model Optimizer, scheduling/goal state to Pi/Codex, job lifecycle to Foundry.')
    }
    return $true
}

function Get-CanonicalJson {
    <#
    .SYNOPSIS
        Canonical JSON (sorted keys, compact) for stable request hashes.
    #>
    param($Value)
    if ($null -eq $Value) { return 'null' }
    if ($Value -is [string]) {
        return ($Value | ConvertTo-Json -Compress)
    }
    if ($Value -is [bool]) { if ($Value) { return 'true' } else { return 'false' } }
    if ($Value -is [int] -or $Value -is [long] -or $Value -is [double] -or $Value -is [decimal]) {
        if ($Value -is [double] -and ([double]::IsNaN($Value) -or [double]::IsInfinity($Value))) {
            throw 'Canonical JSON does not accept NaN or Infinity.'
        }
        return [System.Convert]::ToString($Value, [System.Globalization.CultureInfo]::InvariantCulture)
    }
    if ($Value -is [System.Collections.IDictionary]) {
        $parts = @()
        $keys = @($Value.Keys | ForEach-Object { [string]$_ })
        [Array]::Sort($keys, [StringComparer]::Ordinal)
        foreach ($k in $keys) {
            $parts += ((Get-CanonicalJson $k) + ':' + (Get-CanonicalJson $Value[$k]))
        }
        return '{' + ($parts -join ',') + '}'
    }
    if ($Value -is [System.Collections.IEnumerable] -and -not ($Value -is [string])) {
        $parts = @()
        foreach ($v in $Value) { $parts += (Get-CanonicalJson $v) }
        return '[' + ($parts -join ',') + ']'
    }
    if ($Value -is [psobject]) {
        $h = @{}
        foreach ($p in $Value.PSObject.Properties) { $h[$p.Name] = $p.Value }
        return (Get-CanonicalJson $h)
    }
    return (Get-CanonicalJson ([string]$Value))
}

function Get-FoundryRequestHash {
    <#
    .SYNOPSIS
        SHA-256 hex of the canonical JSON of a request body (idempotency support).
    #>
    param([hashtable]$Body = @{})
    $canon = Get-CanonicalJson $Body
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($canon)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash($bytes)
    } finally {
        $sha.Dispose()
    }
    return ([BitConverter]::ToString($hash) -replace '-', '').ToLowerInvariant()
}

function New-FoundryIdempotencyKey {
    param([string]$Key = '')
    if ($Key) {
        if ($Key.Length -gt 128 -or $Key -match '[\x00-\x1f\x7f]') {
            throw 'idempotency key must be at most 128 characters with no control characters.'
        }
        return $Key
    }
    return [guid]::NewGuid().ToString('N')
}

function ConvertTo-FoundryPathSegment {
    param([Parameter(Mandatory = $true)][string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value.Length -gt 128 -or $Value -match '[\x00-\x1f\x7f]') {
        throw 'Foundry path identifiers must be 1..128 characters with no control characters.'
    }
    return [uri]::EscapeDataString($Value)
}

function Test-FoundryReadPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $bulk = '(projects|jobs|attempts|activity|approvals|artifacts|evidence|health)'
    if ($Path -match ('^/v3/' + $bulk + '$')) { return $true }
    if ($Path -match '^/v3/jobs/[^/?#]+/(status|result)$') { return $true }
    throw ("RDC gateway access is read-only and path '{0}' is outside the Foundry read contract." -f $Path)
}

# ---------- transport ----------

function Invoke-FoundryGateway {
    <#
    .SYNOPSIS
        Low-level authenticated gateway call. Internal; prefer the typed readers below.
        -Method GET, -Path '/v3/jobs', -Query hashtable. POST and request
        bodies are rejected so this helper cannot become a Foundry write path.
        -Transport is an injectable scriptblock for tests:
            param($Method, $Uri, $Headers, $BodyJson) -> psobject result.
    #>
    param(
        [ValidateSet('GET')][string]$Method = 'GET',
        [string]$Path = '/',
        [hashtable]$Query = @{},
        [hashtable]$Body = $null,
        [string]$BaseUrl = '',
        [string]$Token = '',
        [string]$ConfigFile = '',
        [scriptblock]$Transport = $null
    )
    if ($Body -ne $null) {
        [void](Test-FoundryForbiddenFields -Body $Body)
        throw 'RDC gateway access is read-only; request bodies are not accepted.'
    }
    [void](Test-FoundryReadPath -Path $Path)
    $cfg = Get-RdcFoundryConfig -BaseUrl $BaseUrl -Token 'x' -ConfigFile $ConfigFile
    $base = $cfg.BaseUrl
    if ($BaseUrl) { $base = $BaseUrl.TrimEnd('/') }
    elseif ($env:RDC_FOUNDRY_GATEWAY_URL) { $base = $env:RDC_FOUNDRY_GATEWAY_URL.TrimEnd('/') }
    $parsedBase = $null
    if (-not [uri]::TryCreate($base, [UriKind]::Absolute, [ref]$parsedBase) -or
        $parsedBase.Scheme -notin @('http', 'https') -or $parsedBase.UserInfo -or
        $parsedBase.Query -or $parsedBase.Fragment) {
        throw 'Foundry gateway base URL must be an absolute http(s) URL without credentials, query, or fragment.'
    }
    $base = $parsedBase.AbsoluteUri.TrimEnd('/')
    $uri = $base + $Path
    if ($Query.Count -gt 0) {
        $pairs = @()
        foreach ($k in ($Query.Keys | Sort-Object)) {
            if ($null -ne $Query[$k] -and [string]$Query[$k] -ne '') {
                $pairs += ([uri]::EscapeDataString([string]$k) + '=' + [uri]::EscapeDataString([string]$Query[$k]))
            }
        }
        if ($pairs.Count -gt 0) { $uri += '?' + ($pairs -join '&') }
    }
    $token = Get-RdcFoundryToken -Token $Token -ConfigFile $ConfigFile
    $headers = @{ 'Accept' = 'application/json' }
    if ($token) { $headers['Authorization'] = ('Bearer ' + $token) }
    if ($Transport -ne $null) {
        $bodyJson = $null
        if ($Body -ne $null) { $bodyJson = (Get-CanonicalJson $Body) }
        return (& $Transport $Method $uri $headers $bodyJson)
    }
    try {
        return Invoke-RestMethod -Uri $uri -Headers $headers -Method Get -TimeoutSec 30
    } catch {
        $status = $null
        try { $status = $_.Exception.Response.StatusCode.value__ } catch { }
        $msg = $_.Exception.Message
        throw ("Foundry gateway call failed: {0} {1} (status {2}): {3}" -f $Method, $Path, $status, $msg)
    }
}

# ---------- bulk MCP reads (cursor/paginated) ----------

function Get-FoundryBulkPage {
    <#
    .SYNOPSIS
        One bulk-read page: GET /v3/{resource}?cursor&limit&job_id.
        Resource is one of the 8 FOUNDRY_CONTRACT surfaces. Health is a point read.
    #>
    param(
        [ValidateSet('projects', 'jobs', 'attempts', 'activity', 'approvals', 'artifacts', 'evidence', 'health')]
        [string]$Resource = 'projects',
        [string]$Cursor = '',
        [int]$Limit = 50,
        [string]$JobId = '',
        [string]$BaseUrl = '',
        [string]$Token = '',
        [string]$ConfigFile = '',
        [scriptblock]$Transport = $null
    )
    if ($Limit -lt 1) { $Limit = 1 }
    if ($Limit -gt 200) { $Limit = 200 }
    $q = @{ limit = [string]$Limit }
    if ($Cursor) { $q['cursor'] = $Cursor }
    if ($JobId -and $Resource -in @('attempts', 'activity', 'artifacts', 'evidence')) {
        $q['job_id'] = $JobId
    }
    $p = @{
        BaseUrl = $BaseUrl; Token = $Token; ConfigFile = $ConfigFile
    }
    if ($Transport -ne $null) { $p['Transport'] = $Transport }
    return (Invoke-FoundryGateway -Method GET -Path ('/v3/' + $Resource) -Query $q @p)
}

function Get-FoundryBulkAll {
    <#
    .SYNOPSIS
        Follow next_cursor pages until exhausted (bounded by -MaxPages).
        Returns @{ resource, items, pages, truncated }.
    #>
    param(
        [ValidateSet('projects', 'jobs', 'attempts', 'activity', 'approvals', 'artifacts', 'evidence', 'health')]
        [string]$Resource = 'projects',
        [int]$Limit = 50,
        [string]$JobId = '',
        [int]$MaxPages = 20,
        [string]$BaseUrl = '',
        [string]$Token = '',
        [string]$ConfigFile = '',
        [scriptblock]$Transport = $null
    )
    $items = @()
    $cursor = ''
    $pages = 0
    $truncated = $false
    if ($MaxPages -lt 1) { $MaxPages = 1 }
    do {
        $p = @{
            Resource = $Resource; Limit = $Limit
            BaseUrl = $BaseUrl; Token = $Token; ConfigFile = $ConfigFile
        }
        if ($cursor) { $p['Cursor'] = $cursor }
        if ($JobId) { $p['JobId'] = $JobId }
        if ($Transport -ne $null) { $p['Transport'] = $Transport }
        $page = Get-FoundryBulkPage @p
        $rows = @()
        if ($page -ne $null) {
            if ($page.items -ne $null) { $rows = @($page.items) }
            elseif ($page -is [array]) { $rows = @($page) }
        }
        $items += $rows
        $pages++
        $next = ''
        if ($page -ne $null -and $page.next_cursor -ne $null) { $next = [string]$page.next_cursor }
        if ($next -and $pages -ge $MaxPages) { $truncated = $true; break }
        $cursor = $next
    } while ($cursor)
    return [pscustomobject]@{
        resource  = $Resource
        items     = $items
        pages     = $pages
        truncated = $truncated
    }
}

function Get-FoundryJobStatus {
    <#
    .SYNOPSIS
        Read-only job status with monotonic event cursor: GET /v3/jobs/{id}/status?after_cursor.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$JobId,
        [int]$AfterCursor = 0,
        [string]$BaseUrl = '',
        [string]$Token = '',
        [string]$ConfigFile = '',
        [scriptblock]$Transport = $null
    )
    $p = @{ BaseUrl = $BaseUrl; Token = $Token; ConfigFile = $ConfigFile }
    if ($Transport -ne $null) { $p['Transport'] = $Transport }
    $jobSegment = ConvertTo-FoundryPathSegment -Value $JobId
    return (Invoke-FoundryGateway -Method GET -Path ('/v3/jobs/' + $jobSegment + '/status') `
        -Query @{ after_cursor = [string]$AfterCursor } @p)
}

function Get-FoundryJobResult {
    <#
    .SYNOPSIS
        Read-only terminal result read: GET /v3/jobs/{id}/result.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$JobId,
        [string]$BaseUrl = '',
        [string]$Token = '',
        [string]$ConfigFile = '',
        [scriptblock]$Transport = $null
    )
    $p = @{ BaseUrl = $BaseUrl; Token = $Token; ConfigFile = $ConfigFile }
    if ($Transport -ne $null) { $p['Transport'] = $Transport }
    $jobSegment = ConvertTo-FoundryPathSegment -Value $JobId
    return (Invoke-FoundryGateway -Method GET -Path ('/v3/jobs/' + $jobSegment + '/result') -Query @{} @p)
}

function Test-FoundryJobTerminal {
    <#
    .SYNOPSIS
        Read-only helper: is this Foundry state terminal? (reference only; RDC owns no state)
    #>
    param([string]$State = '')
    return ($script:FoundryTerminalStates -contains $State)
}

# ---------- local health boundary (read-only) ----------

function Get-RdcLocalHealth {
    <#
    .SYNOPSIS
        Local transport/health point-read. Reads guardian-status.json plus live
        agent facts. Never starts, stops, or mutates anything.
    #>
    param([int]$LogTailLines = 10)
    $dcDir = Join-Path $env:LOCALAPPDATA 'RDC-Agent'
    $report = Join-Path $dcDir 'guardian-status.json'
    $outLog = Join-Path $dcDir 'remote-output.log'
    $gLog = Join-Path $dcDir 'guardian.log'
    $h = [ordered]@{
        plane       = 'rdc-local'
        available   = $false
        status      = 'UNKNOWN'
        agent_pid   = '-'
        relay_conns = 0
        uptime      = '-'
        last_check  = ''
        failures    = 0
        last_restart = ''
    }
    if (Test-Path -LiteralPath $report) {
        try {
            $st = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
            $h['status'] = [string]$st.Status
            $h['last_check'] = [string]$st.Timestamp
            $h['failures'] = [int]$st.ConsecutiveFailures
            $h['last_restart'] = [string]$st.LastRestart
            if ($h['status'] -like 'OK*') { $h['available'] = $true }
        } catch { }
    }
    foreach ($p in (Get-Process -Name node -ErrorAction SilentlyContinue)) {
        try { $c = (Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $p.Id) -ErrorAction Stop).CommandLine }
        catch { $c = '' }
        if ($c -match 'desktop-commander' -and $c -match '\sremote\b') {
            $h['agent_pid'] = $p.Id
            try { $h['uptime'] = ('{0:mm\m\ ss\s}' -f ((Get-Date) - $p.StartTime)) } catch { }
            try { $h['relay_conns'] = @(Get-NetTCPConnection -OwningProcess $p.Id -State Established -ErrorAction SilentlyContinue).Count } catch { }
        }
    }
    $tails = @{}
    foreach ($lp in @($outLog, $gLog)) {
        if (Test-Path -LiteralPath $lp) {
            try { $tails[$lp] = (Get-Content -LiteralPath $lp -Tail $LogTailLines) -join "`n" }
            catch { $tails[$lp] = '(unreadable)' }
        } else {
            $tails[$lp] = '(not created yet)'
        }
    }
    $h['log_tails'] = $tails
    return [pscustomobject]$h
}

# ---------- named typed host operations (writes) ----------

function Get-RdcHostOpReceiptPath {
    param(
        [Parameter(Mandatory = $true)][string]$IdempotencyKey,
        [string]$ReceiptDirectory = ''
    )
    if (-not $ReceiptDirectory) {
        $ReceiptDirectory = Join-Path $env:LOCALAPPDATA 'RDC-Agent\host-op-receipts'
    }
    [void][System.IO.Directory]::CreateDirectory($ReceiptDirectory)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($IdempotencyKey)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $name = ([BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    return (Join-Path $ReceiptDirectory ($name + '.json'))
}

function New-RdcHostOpClaim {
    param(
        [Parameter(Mandatory = $true)][string]$IdempotencyKey,
        [Parameter(Mandatory = $true)][string]$RequestHash,
        [string]$ReceiptDirectory = ''
    )
    $path = Get-RdcHostOpReceiptPath -IdempotencyKey $IdempotencyKey -ReceiptDirectory $ReceiptDirectory
    $claim = [ordered]@{
        protocol = 'rdc/host-op-receipt/v1'
        request_hash = $RequestHash
        status = 'in_progress'
        created_at = [DateTimeOffset]::UtcNow.ToString('o')
        response = $null
    }
    try {
        $stream = New-Object System.IO.FileStream(
            $path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        try {
            $json = $claim | ConvertTo-Json -Depth 20 -Compress
            $writer = New-Object System.IO.StreamWriter($stream, (New-Object System.Text.UTF8Encoding($false)))
            try { $writer.Write($json); $writer.Flush() } finally { $writer.Dispose() }
        } finally {
            if ($null -ne $stream) { $stream.Dispose() }
        }
        return [pscustomobject]@{ IsNew = $true; Path = $path; Response = $null }
    } catch [System.IO.IOException] {
        try { $existing = Get-Content -LiteralPath $path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop }
        catch { throw 'Host-operation idempotency receipt is unreadable; outcome is unknown and will not be replayed.' }
        if ([string]$existing.request_hash -ne $RequestHash) {
            throw 'Host-operation idempotency key was reused for a different request.'
        }
        if ([string]$existing.status -eq 'completed' -and $null -ne $existing.response) {
            return [pscustomobject]@{ IsNew = $false; Path = $path; Response = $existing.response }
        }
        throw 'A prior host-operation attempt has no confirmed result; outcome is unknown and will not be replayed.'
    }
}

function Complete-RdcHostOpClaim {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$RequestHash,
        [Parameter(Mandatory = $true)]$Response
    )
    $receipt = [ordered]@{
        protocol = 'rdc/host-op-receipt/v1'
        request_hash = $RequestHash
        status = 'completed'
        completed_at = [DateTimeOffset]::UtcNow.ToString('o')
        response = $Response
    }
    [System.IO.File]::WriteAllText(
        $Path,
        ($receipt | ConvertTo-Json -Depth 20 -Compress),
        (New-Object System.Text.UTF8Encoding($false))
    )
}

function Test-RdcHostOpParams {
    param(
        [Parameter(Mandatory = $true)][string]$Op,
        [hashtable]$Params = @{}
    )
    if ($Op -eq 'rdc.restart_agent' -and $Params.ContainsKey('reason')) {
        $reason = [string]$Params['reason']
        if ($reason.Length -gt 280 -or $reason -match '[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]') {
            throw 'reason must be at most 280 characters with no control characters.'
        }
    }
    if ($Op -eq 'rdc.collect_diagnostics' -and $Params.ContainsKey('log_tail_lines')) {
        $parsed = 0
        if (-not [int]::TryParse([string]$Params['log_tail_lines'], [ref]$parsed) -or
            $parsed -lt 1 -or $parsed -gt 100) {
            throw 'log_tail_lines must be an integer from 1 through 100.'
        }
    }
    return $true
}

function Invoke-RdcHostOperation {
    <#
    .SYNOPSIS
        The ONLY RDC write path under Foundry v3: a named op from the closed set
        $RdcHostOps with explicit validation, idempotency key, and policy result.
        Unknown ops, extra params, and forbidden fields are rejected, never run.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Op,
        [hashtable]$Params = @{},
        [string]$IdempotencyKey = '',
        [string]$BaseUrl = '',
        [string]$Token = '',
        [string]$ConfigFile = '',
        [string]$ReceiptDirectory = ''
    )
    [void](Test-FoundryForbiddenFields -Body $Params)
    if (-not $script:RdcHostOps.ContainsKey($Op)) {
        throw ("Unknown host operation '{0}'. Closed set: {1}." -f $Op, (($script:RdcHostOps.Keys | Sort-Object) -join ', '))
    }
    $spec = $script:RdcHostOps[$Op]
    $extra = @()
    foreach ($k in $Params.Keys) {
        if ($spec.Params -notcontains $k) { $extra += $k }
    }
    if ($extra.Count -gt 0) {
        throw ("Operation '{0}' rejects params ({1}); allowed: {2}." -f $Op, ($extra -join ', '), ($spec.Params -join ', '))
    }
    [void](Test-RdcHostOpParams -Op $Op -Params $Params)
    $key = New-FoundryIdempotencyKey -Key $IdempotencyKey
    $request = [ordered]@{ op = $Op; params = [ordered]@{} }
    foreach ($k in ($Params.Keys | Sort-Object)) { $request.params[$k] = $Params[$k] }
    $hash = Get-FoundryRequestHash -Body $request
    $claim = New-RdcHostOpClaim -IdempotencyKey $key -RequestHash $hash -ReceiptDirectory $ReceiptDirectory
    if (-not $claim.IsNew) {
        $replay = $claim.Response
        $replay.duplicate = $true
        return $replay
    }
    $handler = $spec.Handler
    $result = (& $handler -Params $Params -BaseUrl $BaseUrl -Token $Token -ConfigFile $ConfigFile)
    $response = [pscustomobject]@{
        protocol        = 'rdc/host-op/v1'
        op              = $Op
        idempotency_key = $key
        request_hash    = $hash
        duplicate       = $false
        policy          = $result.policy
        state           = $result.state
        evidence        = $result.evidence
    }
    Complete-RdcHostOpClaim -Path $claim.Path -RequestHash $hash -Response $response
    return $response
}

function Get-RdcHostOperations {
    <#
    .SYNOPSIS
        List the closed host-operation set (name + description). Read-only.
    #>
    $out = @()
    foreach ($k in ($script:RdcHostOps.Keys | Sort-Object)) {
        $out += [pscustomobject]@{
            op          = $k
            description = $script:RdcHostOps[$k].Description
            params      = @($script:RdcHostOps[$k].Params)
        }
    }
    return $out
}

# ----- op handlers (each returns @{ policy, state, evidence }) -----

function Invoke-RdcOpRunGuardian {
    param([hashtable]$Params, [string]$BaseUrl, [string]$Token, [string]$ConfigFile)
    $guardian = Join-Path $PSScriptRoot 'RdcGuardian.ps1'
    if (-not (Test-Path -LiteralPath $guardian)) {
        throw 'RdcGuardian.ps1 not found next to RdcFoundryV3.ps1.'
    }
    $r = Invoke-RdcOwnedScript -ScriptName 'RdcGuardian.ps1'
    $decision = 'allow: guardian is the owned local-health actuator'
    return @{
        policy   = @{ decision = $decision; exit_code = $r.ExitCode }
        state    = ('guardian exited ' + $r.ExitCode)
        evidence = @{ output_tail = (Truncate-Tail $r.Output 2000); error_tail = (Truncate-Tail $r.Error 2000) }
    }
}

function Invoke-RdcOpRestartAgent {
    param([hashtable]$Params, [string]$BaseUrl, [string]$Token, [string]$ConfigFile)
    $reason = ''
    if ($Params.ContainsKey('reason')) { $reason = [string]$Params['reason'] }
    $starter = Join-Path $PSScriptRoot 'RdcAgentStart.ps1'
    if (-not (Test-Path -LiteralPath $starter)) {
        throw 'RdcAgentStart.ps1 not found next to RdcFoundryV3.ps1.'
    }
    foreach ($p in (Get-Process -Name node -ErrorAction SilentlyContinue)) {
        try { $c = (Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $p.Id) -ErrorAction Stop).CommandLine }
        catch { $c = '' }
        if ($c -match 'desktop-commander' -and $c -match '\sremote\b') {
            Stop-Process -Id $p.Id -Force
        }
    }
    Start-Sleep -Seconds 3
    $r = Invoke-RdcOwnedScript -ScriptName 'RdcAgentStart.ps1'
    return @{
        policy   = @{ decision = 'allow: agent process lifecycle is RDC-owned transport'; reason = $reason }
        state    = ('starter exited ' + $r.ExitCode)
        evidence = @{ starter_log = (Truncate-Tail $r.Output 2000) }
    }
}

function Invoke-RdcOpCollectDiagnostics {
    param([hashtable]$Params, [string]$BaseUrl, [string]$Token, [string]$ConfigFile)
    $tail = 10
    if ($Params.ContainsKey('log_tail_lines')) {
        $tail = [int]$Params['log_tail_lines']
    }
    $snap = Get-RdcLocalHealth -LogTailLines $tail
    return @{
        policy   = @{ decision = 'allow: read-only diagnostics' }
        state    = [string]$snap.status
        evidence = @{ snapshot = $snap }
    }
}

function Invoke-RdcOpEnsureCrdPresence {
    param([hashtable]$Params, [string]$BaseUrl, [string]$Token, [string]$ConfigFile)
    $watchdog = Join-Path $PSScriptRoot 'CrdWatchdog.ps1'
    if (-not (Test-Path -LiteralPath $watchdog)) {
        throw 'CrdWatchdog.ps1 not found next to RdcFoundryV3.ps1.'
    }
    $r = Invoke-RdcOwnedScript -ScriptName 'CrdWatchdog.ps1'
    return @{
        policy   = @{ decision = 'allow: CRD presence anchor is RDC-owned transport'; exit_code = $r.ExitCode }
        state    = ('watchdog exited ' + $r.ExitCode)
        evidence = @{ output_tail = (Truncate-Tail $r.Output 2000) }
    }
}

function Invoke-RdcOpHealthSnapshot {
    param([hashtable]$Params, [string]$BaseUrl, [string]$Token, [string]$ConfigFile)
    $local = Get-RdcLocalHealth -LogTailLines 5
    $remote = $null
    $remoteError = ''
    try {
        $p = @{ BaseUrl = $BaseUrl; Token = $Token; ConfigFile = $ConfigFile }
        $remote = Get-FoundryBulkPage -Resource health -Limit 50 @p
    } catch {
        $remoteError = $_.Exception.Message
    }
    return @{
        policy   = @{ decision = 'allow: health point-read (local + gateway /v3/health)' }
        state    = [string]$local.status
        evidence = @{ local = $local; gateway_health = $remote; gateway_error = $remoteError }
    }
}

# ---------- shared hidden child launcher (same contract as guardian) ----------

function Invoke-RdcOwnedScript {
    <#
    .SYNOPSIS
        Start one allowlisted RDC script headless (no conhost/cmd flash).
        Returns @{ ExitCode, Output, Error }.
    #>
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('RdcGuardian.ps1', 'RdcAgentStart.ps1', 'CrdWatchdog.ps1')]
        [string]$ScriptName
    )
    try {
        $scriptPath = Join-Path $PSScriptRoot $ScriptName
        if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
            throw ("Owned RDC script '{0}' was not found." -f $ScriptName)
        }
        $powershellExe = Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
        if (-not (Test-Path -LiteralPath $powershellExe -PathType Leaf)) {
            throw 'Windows PowerShell executable was not found in the Windows system directory.'
        }
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $powershellExe
        $psi.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $scriptPath + '"'
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $proc = [System.Diagnostics.Process]::Start($psi)
        $outTask = $proc.StandardOutput.ReadToEndAsync()
        $errTask = $proc.StandardError.ReadToEndAsync()
        if (-not $proc.WaitForExit(180000)) {
            try { $proc.Kill() } catch { }
            try { $proc.WaitForExit() } catch { }
            return @{ ExitCode = -1; Output = [string]$outTask.Result; Error = 'Owned RDC script timed out after 180 seconds.' }
        }
        $proc.WaitForExit()
        return @{ ExitCode = $proc.ExitCode; Output = [string]$outTask.Result; Error = [string]$errTask.Result }
    } catch {
        return @{ ExitCode = -1; Output = ''; Error = $_.Exception.Message }
    }
}

function Truncate-Tail {
    param([string]$Text = '', [int]$MaxChars = 2000)
    if ($Text -eq $null) { return '' }
    if ($Text.Length -le $MaxChars) { return $Text }
    return $Text.Substring($Text.Length - $MaxChars)
}
