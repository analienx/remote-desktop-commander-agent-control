# RDC Agent Control

**Self-healing keep-alive, watchdog and status dashboard for the
[Desktop Commander Remote agent](https://github.com/desktop-commander/remote-desktop-commander)
(`desktop-commander remote`) and Chrome Remote Desktop on Windows.**

If you use the Remote Desktop Commander agent so LLM clients can reach your PC from
anywhere, you have probably hit the classic failure modes: the agent silently dies after
a reboot, it loses its relay connection and just *looks* online, it gets stuck on a
pairing prompt nobody notices, or Chrome Remote Desktop's window never comes back after
logon. This toolset fixes all of that automatically.

```
┌────────────┐   every 5 min   ┌──────────────────────────────────────────┐
│ Scheduled  │────────────────▶│ RDC Guardian                             │
│ Task       │                 │ 1. environment check (package, network)  │
└────────────┘                 │ 2. process check (dedup, restart)        │
                               │ 3. relay connection check (TCP, grace)   │
┌────────────┐   every 15 min  │ 4. session readiness ("Device ready")    │
│ Scheduled  │────────────────▶└───────────────┬──────────────────────────┘
│ Task       │                 │ L1: start if missing                        │
└────────────┘                 │ L2: restart if unhealthy (cooldown)         │
                               │ L3: toast if only a human can fix it        │
                               └───────────────┬─────────────────────────────┘
                                               ▼
                              ┌─────────────────────────────────────────┐
                              │  GUI dashboard  (RdcStatus.ps1)         │
                              │  status · uptime · failures · log tail  │
                              └─────────────────────────────────────────┘
```

## What you get

| Component | File | Purpose |
|---|---|---|
| **Guardian** | `src/RdcGuardian.ps1` | Full-stack health check + tiered self-healing (start → restart → toast), with restart cooldown, boot grace and offline protection so it never thrashes |
| **Agent starter** | `src/RdcAgentStart.ps1` | Starts the `desktop-commander remote` agent fully hidden with output captured to logs; deduplicates; cleans up legacy console hosts |
| **CRD watchdog** | `src/CrdWatchdog.ps1` | Guarantees the `chromoting` service runs and **exactly one** minimized Chrome Remote Desktop PWA window exists — never steals focus, never spawns duplicates |
| **Dashboard** | `src/RdcStatus.ps1` | Dark-themed WPF control panel: live status, agent PID, relay connections, uptime, failure count, guardian log tail, one-click restart/heal |
| **Notifier** | `src/RdcNotify.ps1` | Native Windows toast notifications for actions that genuinely need a human |
| **Foundry v3 boundary** | `src/RdcFoundryV3.ps1` | RDC as transport/health boundary + bulk MCP consumer: cursor/paginated reads (projects, jobs, attempts, activity, approvals, artifacts/evidence, health) through the Interop gateway; writes only via named typed host operations. See `docs/FOUNDRY_V3_RDC.md` |

## Requirements

- Windows 10/11
- [Node.js](https://nodejs.org/) 18+ (`node.exe` at `%ProgramFiles%\nodejs\node.exe`)
- [`@wonderwhy-er/desktop-commander`](https://www.npmjs.com/package/@wonderwhy-er/desktop-commander) (the installer sets this up for you)
- Optional: Chrome Remote Desktop host installed (for the CRD watchdog part)
- No admin rights required — everything runs per-user

## Quick start

One-liner (PowerShell):

```powershell
irm https://raw.githubusercontent.com/analienx/remote-desktop-commander-agent-control/main/install.ps1 | iex
```

Or clone and install:

```powershell
git clone https://github.com/analienx/remote-desktop-commander-agent-control.git
cd remote-desktop-commander-agent-control
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The installer:

1. Verifies Node.js and the `desktop-commander` package (installs the package if missing)
2. Copies the tooling to `%LOCALAPPDATA%\RDC-Control`
3. Registers two per-user scheduled tasks:
   - **RDC-Agent-Guardian** — at logon + every 5 minutes
   - **CRD-Watchdog** — at logon + every 15 minutes (skippable with `-SkipCrdWatchdog`)
4. Creates an **RDC Agent Control** shortcut on your desktop
5. Runs the guardian once so your first status check is already green

Then run the agent's first-time pairing once, from a visible console:

```powershell
desktop-commander remote
```

Follow the verification prompt in your browser. After that, the guardian keeps the
session alive for good — reboots, crashes, stale relays and all.

## Daily use

| Want to... | Do this |
|---|---|
| See current health | Double-click **RDC Agent Control** on the desktop |
| Restart the agent | Dashboard → **Restart agent** |
| Force a heal cycle | Dashboard → **Run guardian now** |
| Check what happened | `%LOCALAPPDATA%\RDC-Agent\guardian.log`, `remote-output.log` |
| Check CRD watchdog | `%LOCALAPPDATA%\CRD-Watchdog\watchdog.log` |
| Remove everything | `powershell -File .\uninstall.ps1` |

## How the guardian heals

| Level | Trigger | Action |
|---|---|---|
| **L1** | No agent process | Start it, wait up to 90 s for `Device ready` |
| **L2** | Agent process but no relay connection, or connected but not session-ready | Kill + restart (10 min cooldown, 120 s boot grace) |
| **L3** | Stuck on device-verification prompt, or 3+ consecutive failures | Toast notification — only a human can pair or inspect |

Exit codes are script-friendly for CI/automation:
`0` healthy · `1` started · `2` restarted · `3` network down (no thrash) ·
`4` needs user action · `5` unrecoverable.

State persists in `%LOCALAPPDATA%\RDC-Agent\guardian-state.json`, so the guardian
remembers failures across runs and never restarts more often than the cooldown allows.

## Why "one permanent window" for Chrome Remote Desktop

The `chromoting` service (LocalSystem, auto-start) is the OS-level host anchor, but the
**presence anchor** — the thing that shows your machine as online and ready in your
device list — is the CRD PWA window. This toolset's watchdog:

1. Detects an existing CRD window **by title inside `msedge.exe`** (the `msedge_proxy.exe`
   is just a transient launcher that exits after handoff — checking for it causes the
   infamous "window spawn storm")
2. If a window exists → does nothing. No focus stealing, no pops, ever.
3. If it's genuinely gone → relaunches it **minimized**, rate-limited to once per 30 min
4. If the service is down → tries to start it, toasts if elevation is required

## Unattended / CI notes

- All scripts are idempotent and safe to run on a schedule
- Guardian is network-aware: it will not restart the agent while the relay is unreachable
- Tasks run with `-WindowStyle Hidden` and `RunLevel: Limited` — no elevation, no prompts
- Logs auto-rotate at 256 KB

## Troubleshooting

**Agent starts but dashboard shows `NEEDS-USER-VERIFICATION`**
Run `desktop-commander remote` in a console once and complete the browser verification.
The guardian detects this state and deliberately stops restarting (no thrash).

**`FATAL-PACKAGE-MISSING`**
Reinstall the npm package:
`npm install -g --allow-scripts=@wonderwhy-er/desktop-commander,sharp,puppeteer @wonderwhy-er/desktop-commander`

**CRD device shows online but connections time out**
That's a relay/session mismatch — run the guardian manually (`Run guardian now`) and
watch `guardian.log`; if the agent restart doesn't clear it, re-pair via
`desktop-commander remote`.

**Watchdog toasts about the CRD service**
Start an elevated PowerShell and run `Start-Service chromoting` once; the watchdog
handles the rest.

## Contributing

Issues and PRs welcome — the healing heuristics are deliberately conservative and
well-commented so they're easy to tune for other environments.

## Foundry v3 role

Under the Foundry v3 redesign (issue #1; context: analienx/config#37,
analienx/agent-foundry#2, analienx/agent-interop-gateway#6), RDC is a
**transport/health boundary and bulk MCP consumer**: it reads projects, jobs,
attempts, activity, approvals, artifacts/evidence and health through the
Interop gateway with efficient cursor/paginated reads, and performs writes only
through named typed host operations with explicit validation, idempotency keys
and durable no-blind-replay receipts. RDC owns no model/account routing,
scheduling, goal state,
or Foundry job state. Implementation: `src/RdcFoundryV3.ps1`; contract and
rollback notes: `docs/FOUNDRY_V3_RDC.md`; tests: `tests/RdcFoundryV3.Tests.ps1`.

## License

[MIT](LICENSE) © 2026 analienx
