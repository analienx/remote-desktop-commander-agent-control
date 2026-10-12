# Managed RDC runtime (Windows)

The canonical Desktop Commander package is pinned to **0.2.52** under:

    %LOCALAPPDATA%\RDC-Control\managed\desktop-commander-0.2.52\node_modules\@wonderwhy-er\desktop-commander

Never use an ephemeral npm _npx cache as the permanent launcher. The active
pointer lives in %LOCALAPPDATA%\RDC-Control\rdc-active-install.json and includes
schema=1, the exact package_root, version=0.2.52, and installation_id=managed-0.2.52.

The source-controlled rdc_active_hook.py validates exact package location,
package name/version, and the lockfile; it invokes the existing Runner process
hook and native write-audit hook against that exact installation. Hook state and
backups are scoped by installation_id. The agent starter refuses an unpatched
or invalid runtime. Guardian watches only agents from the canonical package,
uses a single process enumeration, and has version-scoped agent output logs.

## Deploy or upgrade safely

1. Install an exact package version into a NEW managed directory, retaining
   package-lock.json. Example (from cmd.exe):

       npm install --prefix "%LOCALAPPDATA%\RDC-Control\managed\desktop-commander-0.2.52" --save-exact --ignore-scripts --no-audit --no-fund @wonderwhy-er/desktop-commander@0.2.52

2. Copy these source-controlled files to %LOCALAPPDATA%\RDC-Control:
   rdc_active_hook.py, rdc_runner_hook.py, rdc_write_audit_hook.py,
   analienx-runner-policy.js, and analienx-write-audit.js.
3. Write rdc-active-install.candidate.json under RDC-Control containing the
   new exact package_root, matching semantic version and installation_id.
   Run Python rdc_active_hook.py package, ensure, and status with
   --config <candidate path>. Require healthy=true for the process hook and
   native-write audit and check the resolved package version/path.
4. Preserve the prior active config in content-addressed backups. Atomically
   replace rdc-active-install.json with the verified candidate only after
   preflight. Never modify the package while it is running.
5. Start the pinned agent with RdcAgentStart.ps1 (or scheduled Guardian).
   Independently verify the live worker process path, relay readiness and
   a read-only runner:exec -> TaskRouter -> SLRunner command.
6. Retire older remote agents only after the pinned agent is actually live,
   using verified process identities, not blanket termination of node.exe.
   Keep rollback files until the cutover survives another Guardian check.

For future upgrades review the upstream package against signature-anchored
hook tests before activation. The Guardian status checker must validate the
ACTIVE installation, not merely the presence of a patched old package.

## Troubleshooting

Check %LOCALAPPDATA%\RDC-Agent\guardian-status.json, guardian.log, and
remote-output-managed-0.2.52.log. Run:

    python "%LOCALAPPDATA%\RDC-Control\rdc_active_hook.py" status

An old npx cache is permitted only as a previously approved fallback, not
an automatic alternative. Do not restart unrelated processes or flash Zigbee
hardware as part of RDC recovery.
