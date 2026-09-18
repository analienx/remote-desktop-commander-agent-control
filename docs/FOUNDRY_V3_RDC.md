# RDC under Foundry v3: transport/health boundary + bulk MCP consumer

Status: **implemented** (`src/RdcFoundryV3.ps1`, tests in `tests/RdcFoundryV3.Tests.ps1`).
Linked issue: analienx/remote-desktop-commander-agent-control#1.
Architectural context: analienx/config#37 (`FOUNDRY_EXECUTION_SUBSTRATE_V3`,
`FOUNDRY_CONTRACT.md`, `foundry-resources.schema.json`), analienx/agent-foundry#2
(minimal isolated-execution contract), analienx/agent-interop-gateway#6 (typed
delegation + bulk reads). This document is the RDC-side scope record; it changes
no ownership stated in those PRs.

## 1. Role

Remote Desktop Commander stays a **transport and specialist-control surface for
the Windows host**. Under Foundry v3 it is:

- a **transport/health boundary** — the existing guardian (`RdcGuardian.ps1`),
  agent starter (`RdcAgentStart.ps1`), CRD watchdog (`CrdWatchdog.ps1`),
  dashboard (`RdcStatus.ps1`) and notifier (`RdcNotify.ps1`) are unchanged and
  remain the local-health authority (exit codes 0–5, 10-minute restart cooldown,
  120-second boot grace, offline no-thrash, one minimized PWA window);
- a **bulk MCP consumer** — normal Foundry/supervisor reads go through the
  Interop Gateway `/v3/*` API with cursor/paginated bulk reads instead of screen
  scraping or repeated remote shell calls;
- a **narrow typed-write surface** — writes go only through the closed named
  host-operation set in `RdcFoundryV3.ps1`, each with explicit validation, an
  idempotency key, and a policy result.

## 2. Bulk reads (through the gateway, never local Foundry state)

`Get-FoundryBulkPage` / `Get-FoundryBulkAll` speak `GET /v3/{resource}` with
`cursor`, `limit` (clamped 1–200, gateway default 50) and `job_id` (accepted for
`attempts`, `activity`, `artifacts`, `evidence`). `Get-FoundryBulkAll` follows
`next_cursor` until exhausted, bounded by `-MaxPages` (default 20) after which
it reports `truncated: true` instead of looping forever.

| Resource | Route | Cursor | Row fields (from `foundry-resources.schema.json`) |
|---|---|---|---|
| `projects` | `GET /v3/projects` | page cursor | project, execution_mode, repository, ref, readiness, required_artifact, last_job |
| `jobs` | `GET /v3/jobs` | page cursor | job_id, objective, agent, runtime, project, state, progress, elapsed_ms, cancellation, evidence |
| `attempts` | `GET /v3/attempts[?job_id]` | page cursor | attempt_id, job_id, generation, state, actor, source_digest, artifact_digests, policy_hash, cursor |
| `activity` | `GET /v3/activity[?job_id]` | monotonic event cursor | timestamp, project, job_id, attempt_id, agent, source, action, outcome, correlation_id |
| `artifacts` | `GET /v3/artifacts[?job_id]` | page cursor | schema, kind, payload_digest, payload_bytes, producer, source_repo, source_commit, lock_digest, platform, arch, toolchain, built_at, retention, lifecycle_policy, provenance, verify |
| `approvals` | `GET /v3/approvals` | page cursor | summary, target, payload_hash, reason, effect, rollback, expiry, requester |
| `health` | `GET /v3/health` | none (point read) | plane, available, build, policy_revision, reason |
| `evidence` | `GET /v3/evidence[?job_id]` | page cursor | job_id, name, digest, reference |

Read-only job inspection: `Get-FoundryJobStatus -JobId -AfterCursor`
(`GET /v3/jobs/{id}/status?after_cursor`, monotonic event cursor) and
`Get-FoundryJobResult -JobId` (`GET /v3/jobs/{id}/result`, terminal states
only). `Test-FoundryJobTerminal` interprets states; RDC owns no state machine.

RDC keeps **no authoritative Foundry state**: no local job store, no scheduling,
no goal state, no model/account routing. Gateway error codes surface as typed
failures: `404` unknown job/resource, `409` idempotency conflict / stale
generation / illegal transition / not-ready, `422` invalid payload or
model-routing fields.

Configuration: `Get-RdcFoundryConfig` reads explicit args, then
`RDC_FOUNDRY_GATEWAY_URL` / `RDC_FOUNDRY_TOKEN`, then
`%LOCALAPPDATA%\RDC-Agent\foundry-gateway.json` (`{ "base_url": ...,
"token_file": ... }`), then the loopback default. The token is resolved only
into the `Authorization: Bearer` header and is never written to logs, results,
or evidence.

The low-level transport accepts only the documented `GET /v3/*` read routes.
It rejects POST, request bodies, URL credentials, unknown paths, and malformed
job identifiers before opening a connection. Job IDs are encoded as a single
URL path segment.

## 3. Writes: named typed host operations only

`Invoke-RdcHostOperation -Op -Params [-IdempotencyKey]` is the only write path.
Closed set (`Get-RdcHostOperations`):

| Op | Effect |
|---|---|
| `rdc.run_guardian` | Run the guardian heal cycle now (existing `RdcGuardian.ps1`) |
| `rdc.restart_agent` | Stop stale/duplicate agent processes, start one hidden agent (`RdcAgentStart.ps1`); optional `reason` ≤ 280 chars |
| `rdc.collect_diagnostics` | Read-only snapshot (guardian status, agent PID/relay/uptime, log tails); optional `log_tail_lines` 1–100 |
| `crd.ensure_presence` | Run the CRD watchdog once (existing `CrdWatchdog.ps1`) |
| `host.read_health_snapshot` | Local health plus gateway `GET /v3/health` when reachable |

Every write returns `protocol: rdc/host-op/v1` with `op`, `idempotency_key`
(caller-supplied or generated GUID), `request_hash` (SHA-256 over canonical
JSON), `duplicate`, `policy` (explicit allow/deny decision), `state`, and
`evidence`. Durable per-user receipts under
`%LOCALAPPDATA%\RDC-Agent\host-op-receipts` make a completed retry return its
stored result. Reusing a key for different parameters is rejected. A claim
left incomplete by a crash is reported as `unknown` and is never automatically
replayed, because its side effects may already have happened.
Unknown ops, extra params, and forbidden fields throw before anything runs.

RDC exposes **no** Foundry job writes (`prepare`/`attach`/`execute`/`cancel`/
`quarantine`), **no** shell/filesystem mutation tool, and **no**
model/account/cost/routing fields — requests carrying `model*`, `account*`,
`cost*`, `price`, `quota`, `routing`, `route`, `schedule*`, `goal*`, `plan`,
`subagent`, `command_profile`, `source_digest`, or `manifest` are rejected
client-side, mirroring the gateway's `422`.

## 4. What RDC does not own

- Model/account routing → Cline Model Optimizer alone.
- Scheduling, goal state, plans, subagents → Pi/Codex.
- Job lifecycle, attempt state, artifact verification → Agent Foundry.
- General filesystem/shell mutation → not an RDC tool.

## 5. Files

- `src/RdcFoundryV3.ps1` — boundary module (functions only, no action on load).
- `src/RdcGuardian.ps1`, `RdcAgentStart.ps1`, `CrdWatchdog.ps1`, `RdcStatus.ps1`,
  `RdcNotify.ps1` — unchanged behavior (see regression tests).
- `tests/RdcFoundryV3.Tests.ps1` — Pester 3.4 suite (contract + regression).
- `.supervisor/project.yaml` — execution declaration (`host_specialist`).

## 6. Evidence and rollback

- Evidence: `Invoke-Pester ./tests/RdcFoundryV3.Tests.ps1` (all passing),
  `PSParser` syntax check over `src/*.ps1`, `git diff --check` clean.
- Rollback: this change is additive. `src/RdcFoundryV3.ps1`, `tests/`,
  `docs/FOUNDRY_V3_RDC.md`, and `.supervisor/project.yaml` are new files;
  existing scripts, installer task names, and uninstall targets are untouched.
  Roll back with `git revert` of the PR merge (or delete the four new paths);
  no scheduled task, log, state file, or installed copy changes until
  `install.ps1` is re-run (it copies `src/*` verbatim).
