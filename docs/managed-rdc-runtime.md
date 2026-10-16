# Managed RDC runtime (Windows)

The accepted runtime is pinned to **0.3.0** under:

    %LOCALAPPDATA%\RDC-Control\managed\desktop-commander-0.3.0\node_modules\@wonderwhy-er\desktop-commander

The active pointer is %LOCALAPPDATA%\RDC-Control\rdc-active-install.json:
schema=1, exact package_root, version=0.3.0, installation_id=managed-0.3.0.
The prior managed **0.2.52** installation is retained for rollback.
Ephemeral npm _npx directories are not permanent launchers.

rdc_active_hook.py validates package location, name/version and lockfile, then
checks process routing and native-write auditing for that exact installation.
Hook state and backups are scoped by installation ID. Starter and Guardian use
the active package and its versioned output log.

## Prepare and deploy safely

1. Resolve the stable npm release and integrity. Use the **Published RDC runtime**
   GitHub-hosted Windows workflow to validate the exact package: hook installation,
   idempotence, a real MCP handshake, request routing, native write/edit auditing
   and termination of a CI-owned process. Download the successful exact-commit
   artifact and compare its SHA256 with the hosted log. Do not build or package
   this public repository on the managed workstation.
2. Use the named private helper in analienx/config:
   supervisor/safety/upgrade_managed_rdc.py. Its version-specific source/archive
   pins must match the hosted candidate. Each operation requires an expiring
   immutable plan and exact plan hash. Raw copies into AppData or an unreviewed
   package install are not the deployment boundary.
3. The stage operation extracts into a NEW managed directory, validates archive
   paths/types, preserves control/configuration files, installs verified control
   sources, and checks the candidate's routing and audit hooks. The active
   pointer and running agent remain unchanged.
4. Finish or safely cancel active RDC process sessions before cutover. A hidden
   demand-only task runs the verified worker outside RDC's kill-on-close Job
   Object. It rejects active commands, validates process creation times,
   atomically switches the pointer, stops the old MCP child before its parent,
   and starts the candidate. Authentication state is preserved.
5. Verify the connector reports the new version **Online**, returns pong and
   executes a worktree-bound read-only command through TaskRouter and SLRunner.
   Require rdc_active_hook.py status to report both hook layers healthy.
   An incorrect project/worktree identity must remain rejected.
6. Confirm the next Guardian check preserves the new agent. Retain the previous
   installation, configuration backup and receipts. The rollback operation
   creates a fresh plan against the current exact processes; never replay an
   expired cutover plan.

## Accepted 0.3.0 candidate

Public source: b862da8e12e337fd7dec19ec3eca50854354cb36 (PR13).
Hosted run: 38888315208. Runtime archive SHA256:

    4c880b033efdca682f428dfc45d19be2a66d74a09592b68d7b9fb8ba825f3757

Private deployment and rollback receipts:

    C:\Workspace\.analienx\rdc-upgrade-0.3.0

The hidden task RDC-Upgrade-0.3.0-2ce47952 performed the cutover. It has no
recurring trigger. Guardian continues its existing scheduled checks.

## Troubleshooting

Inspect %LOCALAPPDATA%\RDC-Agent\guardian-status.json, guardian.log, and
remote-output-managed-0.3.0.log / .err.log. Verify:

    python "%LOCALAPPDATA%\RDC-Control\rdc_active_hook.py" status

An old Device ready line or an established socket does not prove current remote
reachability. Guardian distinguishes channel failure and unknown reachability;
connector ping and a real command receipt provide live acceptance.
Preserve session identity and private diagnostics during transport investigation.
Do not restart unrelated processes or issue device recovery commands as part of
RDC maintenance.
