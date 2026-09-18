# Pester tests for src/RdcFoundryV3.ps1 (RDC Foundry v3 transport/health boundary).
# Run: Invoke-Pester ./tests/RdcFoundryV3.Tests.ps1
# Compatible with Pester 3.4 (Windows PowerShell 5.1, no PSScriptAnalyzer needed).

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = Join-Path (Split-Path -Parent $here) 'src\RdcFoundryV3.ps1'
. $src

# Pester 3.4's legacy Should Throw assertion stores exception state in a scope
# that is not reliable under current PowerShell. This helper verifies an actual
# terminating exception and remains compatible with Windows PowerShell 5.1.
function Test-TerminatingError {
    param([scriptblock]$Action)
    try { & $Action | Out-Null; return $false } catch { return $true }
}

Describe 'RDC Foundry v3: closed vocabularies' {
    It 'exposes exactly the 8 FOUNDRY_CONTRACT bulk resources' {
        $r = $script:FoundryBulkResources | Sort-Object
        ($r -join ',') | Should Be 'activity,approvals,artifacts,attempts,evidence,health,jobs,projects'
    }

    It 'rejects unknown bulk resources via ValidateSet' {
        (Test-TerminatingError { Get-FoundryBulkPage -Resource 'schedules' -Transport { param($m, $u, $h, $b) return $null } }) | Should Be $true
    }

    It 'exposes a closed host-op set with no shell/filesystem op' {
        $ops = (Get-RdcHostOperations | Select-Object -ExpandProperty op) | Sort-Object
        ($ops -join ',') | Should Be 'crd.ensure_presence,host.read_health_snapshot,rdc.collect_diagnostics,rdc.restart_agent,rdc.run_guardian'
        # Pester 3.4's `Should Contain` treats its actual value as a file path.
        # Use PowerShell collection membership so this remains valid on PS 5.1+.
        ($ops -contains 'run_shell') | Should Be $false
        ($ops -contains 'submit_job') | Should Be $false
        ($ops -contains 'execute') | Should Be $false
    }

    It 'rejects unknown host operations' {
        (Test-TerminatingError { Invoke-RdcHostOperation -Op 'foundry.execute' -Params @{} }) | Should Be $true
    }

    It 'rejects extra params on a typed op' {
        (Test-TerminatingError { Invoke-RdcHostOperation -Op 'rdc.run_guardian' -Params @{ model = 'x' } }) | Should Be $true
        (Test-TerminatingError { Invoke-RdcHostOperation -Op 'rdc.run_guardian' -Params @{ bogus = 'x' } }) | Should Be $true
    }

    It 'validates typed host-operation parameter values before claiming work' {
        (Test-TerminatingError {
            Invoke-RdcHostOperation -Op 'rdc.collect_diagnostics' -Params @{ log_tail_lines = 0 } -IdempotencyKey 'bad-tail' -ReceiptDirectory $TestDrive
        }) | Should Be $true
        (Test-TerminatingError {
            Invoke-RdcHostOperation -Op 'rdc.restart_agent' -Params @{ reason = ('x' * 281) } -IdempotencyKey 'bad-reason' -ReceiptDirectory $TestDrive
        }) | Should Be $true
        @(Get-ChildItem -LiteralPath $TestDrive -Filter '*.json' -ErrorAction SilentlyContinue).Count | Should Be 0
    }
}

Describe 'RDC Foundry v3: forbidden-field guards' {
    It 'rejects model/account/cost/routing fields' {
        foreach ($f in @('model', 'model_id', 'account', 'account_alias', 'cost_tier', 'routing', 'route')) {
            (Test-TerminatingError { Test-FoundryForbiddenFields -Body @{ $f = 'x' } }) | Should Be $true
        }
    }

    It 'rejects scheduling/goal-state/job-lifecycle ownership fields' {
        foreach ($f in @('schedule', 'goal', 'goal_id', 'plan', 'subagent', 'command_profile', 'manifest')) {
            (Test-TerminatingError { Test-FoundryForbiddenFields -Body @{ $f = 'x' } }) | Should Be $true
        }
    }

    It 'accepts clean bodies' {
        (Test-FoundryForbiddenFields -Body @{ reason = 'ok'; log_tail_lines = 5 }) | Should Be $true
    }

    It 'gateway transport validates bodies before sending' {
        $called = @{ n = 0 }
        $t = { param($m, $u, $h, $b) $called.n++; return $null }.GetNewClosure()
        (Test-TerminatingError { Invoke-FoundryGateway -Method POST -Path '/v3/x' -Body @{ model = 'm' } -Transport $t }) | Should Be $true
        $called.n | Should Be 0
    }

    It 'rejects forbidden ownership fields nested in a request' {
        (Test-TerminatingError { Test-FoundryForbiddenFields -Body @{ payload = @{ route = 'paid' } } }) | Should Be $true
    }

    It 'allows only the documented read routes through the gateway helper' {
        $t = { param($m, $u, $h, $b) return @{ items = @() } }
        (Test-TerminatingError { Invoke-FoundryGateway -Method GET -Path '/v3/jobs/prepare' -Transport $t }) | Should Be $true
        (Test-TerminatingError { Invoke-FoundryGateway -Method GET -Path '/admin' -Transport $t }) | Should Be $true
    }

    It 'does not expose a generic process launcher' {
        (Get-Command Invoke-RdcHiddenChild -ErrorAction SilentlyContinue) | Should BeNullOrEmpty
        (Test-TerminatingError { Invoke-RdcOwnedScript -ScriptName 'cmd.exe' }) | Should Be $true
    }
}

Describe 'RDC Foundry v3: request hashing and idempotency' {
    It 'produces stable canonical hashes regardless of key order' {
        $a = Get-FoundryRequestHash -Body @{ op = 'x'; b = 1; a = 2 }
        $b = Get-FoundryRequestHash -Body @{ a = 2; b = 1; op = 'x' }
        $a | Should Be $b
        $a.Length | Should Be 64
    }

    It 'generates an idempotency key when none is supplied' {
        $r = Invoke-RdcHostOperation -Op 'rdc.collect_diagnostics' -Params @{ log_tail_lines = 1 } -ReceiptDirectory $TestDrive
        $r.idempotency_key.Length | Should Be 32
        $r.request_hash.Length | Should Be 64
    }

    It 'passes through a caller-supplied idempotency key' {
        $r = Invoke-RdcHostOperation -Op 'rdc.collect_diagnostics' -Params @{} -IdempotencyKey 'abc123' -ReceiptDirectory $TestDrive
        $r.idempotency_key | Should Be 'abc123'
    }

    It 'returns an explicit policy result on every write' {
        $r = Invoke-RdcHostOperation -Op 'rdc.collect_diagnostics' -Params @{} -IdempotencyKey 'k1' -ReceiptDirectory $TestDrive
        $r.protocol | Should Be 'rdc/host-op/v1'
        $r.op | Should Be 'rdc.collect_diagnostics'
        $r.policy.decision | Should Match 'read-only'
        $r.state | Should Not BeNullOrEmpty
    }

    It 'replays a completed receipt and rejects key reuse for changed params' {
        $first = Invoke-RdcHostOperation -Op 'rdc.collect_diagnostics' -Params @{ log_tail_lines = 1 } -IdempotencyKey 'same-key' -ReceiptDirectory $TestDrive
        $again = Invoke-RdcHostOperation -Op 'rdc.collect_diagnostics' -Params @{ log_tail_lines = 1 } -IdempotencyKey 'same-key' -ReceiptDirectory $TestDrive
        $first.duplicate | Should Be $false
        $again.duplicate | Should Be $true
        $again.request_hash | Should Be $first.request_hash
        (Test-TerminatingError {
            Invoke-RdcHostOperation -Op 'rdc.collect_diagnostics' -Params @{ log_tail_lines = 2 } -IdempotencyKey 'same-key' -ReceiptDirectory $TestDrive
        }) | Should Be $true
    }
}

Describe 'RDC Foundry v3: cursor/paginated bulk reads' {
    It 'builds GET /v3/{resource} with limit/cursor/job_id' {
        $seen = @{}
        $t = { param($m, $u, $h, $b) $seen['m'] = $m; $seen['u'] = $u; return @{ items = @(); next_cursor = $null } }.GetNewClosure()
        Get-FoundryBulkPage -Resource 'attempts' -Cursor '40' -Limit 25 -JobId 'j1' -BaseUrl 'http://127.0.0.1:9' -Transport $t | Out-Null
        $seen['m'] | Should Be 'GET'
        $seen['u'] | Should Match '/v3/attempts\?'
        $seen['u'] | Should Match 'limit=25'
        $seen['u'] | Should Match 'cursor=40'
        $seen['u'] | Should Match 'job_id=j1'
    }

    It 'clamps limit to 1..200' {
        $seen = @{}
        $t = { param($m, $u, $h, $b) $seen['u'] = $u; return @{ items = @(); next_cursor = $null } }.GetNewClosure()
        Get-FoundryBulkPage -Resource 'jobs' -Limit 9999 -BaseUrl 'http://127.0.0.1:9' -Transport $t | Out-Null
        $seen['u'] | Should Match 'limit=200'
    }

    It 'follows next_cursor until exhausted' {
        $calls = @{ n = 0 }
        $seen = @{ uris = @() }
        $t = {
            param($m, $u, $h, $b)
            $calls.n++
            $seen.uris += $u
            if ($calls.n -eq 1) { return @{ items = @(1, 2); next_cursor = '2' } }
            return @{ items = @(3); next_cursor = $null }
        }.GetNewClosure()
        $r = Get-FoundryBulkAll -Resource 'jobs' -Limit 2 -BaseUrl 'http://127.0.0.1:9' -Transport $t
        ($r.items -join ',') | Should Be '1,2,3'
        $r.pages | Should Be 2
        $r.truncated | Should Be $false
        $seen.uris[1] | Should Match 'cursor=2'
    }

    It 'marks truncation when MaxPages is hit' {
        $t = { param($m, $u, $h, $b) return @{ items = @(1); next_cursor = 'x' } }.GetNewClosure()
        $r = Get-FoundryBulkAll -Resource 'activity' -MaxPages 3 -BaseUrl 'http://127.0.0.1:9' -Transport $t
        $r.pages | Should Be 3
        $r.truncated | Should Be $true
    }

    It 'reads job status with after_cursor' {
        $seen = @{}
        $t = { param($m, $u, $h, $b) $seen['u'] = $u; return @{ job = @{}; events = @(); cursor = 7 } }.GetNewClosure()
        $r = Get-FoundryJobStatus -JobId 'job-1' -AfterCursor 5 -BaseUrl 'http://127.0.0.1:9' -Transport $t
        $seen['u'] | Should Match '/v3/jobs/job-1/status'
        $seen['u'] | Should Match 'after_cursor=5'
        $r.cursor | Should Be 7
    }

    It 'escapes job identifiers as one URL path segment' {
        $seen = @{ u = '' }
        $t = { param($m, $u, $h, $b) $seen.u = $u; return @{ state = 'running' } }.GetNewClosure()
        Get-FoundryJobStatus -JobId 'job/with?parts' -BaseUrl 'http://127.0.0.1:9' -Transport $t | Out-Null
        $seen.u | Should Match '/v3/jobs/job%2Fwith%3Fparts/status'
    }

    It 'reads terminal job results' {
        $seen = @{}
        $t = { param($m, $u, $h, $b) $seen['u'] = $u; return @{ state = 'succeeded' } }.GetNewClosure()
        $r = Get-FoundryJobResult -JobId 'job-1' -BaseUrl 'http://127.0.0.1:9' -Transport $t
        $seen['u'] | Should Match '/v3/jobs/job-1/result'
        $r.state | Should Be 'succeeded'
    }

    It 'knows terminal states without owning the machine' {
        (Test-FoundryJobTerminal -State 'succeeded') | Should Be $true
        (Test-FoundryJobTerminal -State 'quarantined') | Should Be $true
        (Test-FoundryJobTerminal -State 'running') | Should Be $false
    }
}

Describe 'RDC Foundry v3: no Foundry job writes' {
    It 'exposes no prepare/attach/execute/cancel/quarantine functions' {
        foreach ($fn in @('Invoke-FoundryPrepare', 'Submit-FoundryJob', 'Start-FoundryJob',
                          'Invoke-FoundryExecute', 'Invoke-FoundryCancel', 'Invoke-FoundryQuarantine',
                          'Invoke-FoundryAttach')) {
            (Get-Command $fn -ErrorAction SilentlyContinue) | Should BeNullOrEmpty
        }
    }
}

Describe 'RDC regression: existing behavior preserved' {
    $srcDir = Split-Path -Parent $src

    It 'all existing scripts still parse without syntax errors' {
        foreach ($f in @('RdcGuardian.ps1', 'RdcAgentStart.ps1', 'CrdWatchdog.ps1', 'RdcStatus.ps1', 'RdcNotify.ps1', 'RdcFoundryV3.ps1')) {
            $errs = $null
            $toks = $null
            [void][System.Management.Automation.PSParser]::Tokenize((Get-Content (Join-Path $srcDir $f) -Raw), [ref]$errs)
            (@($errs).Count) | Should Be 0
        }
    }

    It 'guardian exit-code contract is untouched' {
        $g = Get-Content (Join-Path $srcDir 'RdcGuardian.ps1') -Raw
        foreach ($code in @('exit 0', 'exit 1', 'exit 2', 'exit 3', 'exit 4', 'exit 5')) {
            $g | Should Match ([regex]::Escape($code))
        }
        $g | Should Match 'restartCooldownMin'
        $g | Should Match 'bootGraceSec'
        $g | Should Match 'NETWORK-DOWN'
    }

    It 'watchdog one-window and rate-limit semantics are untouched' {
        $w = Get-Content (Join-Path $srcDir 'CrdWatchdog.ps1') -Raw
        $w | Should Match 'MainWindowTitle'
        $w | Should Match '30'
        $w | Should Match 'chromoting'
    }

    It 'installer still registers the same two tasks' {
        $i = Get-Content (Join-Path (Split-Path -Parent $srcDir) 'install.ps1') -Raw
        $i | Should Match 'RDC-Agent-Guardian'
        $i | Should Match 'CRD-Watchdog'
    }
}
