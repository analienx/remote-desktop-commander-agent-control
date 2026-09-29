import path from 'path';

const REQUEST_RE = /^C:\\Workspace\\\.analienx\\rdc-requests\\[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\.json$/;
const COMMAND_RE = /^python(?:\.exe)? "%LOCALAPPDATA%\\RDC-Control\\rdc_runner_bridge\.py" --request "(C:\\Workspace\\\.analienx\\rdc-requests\\[0-9a-fA-F-]{36}\.json)"$/i;

/**
 * Mandatory RDC execution boundary.
 *
 * Direct start_process is not an execution plane. The only accepted command
 * starts the fixed Python bridge with a UUID-named request file under the
 * workspace request root. The bridge then submits typed workspace_exec to the
 * local Analienx Runner. Native RDC read tools do not pass through this hook.
 */
export function enforceAnalienxRunnerRouting(args) {
    const command = String(args?.command ?? '').trim();
    const shell = path.win32.basename(String(args?.shell ?? '')).toLowerCase();
    if (shell && shell !== 'cmd.exe' && shell !== 'cmd') {
        throw new Error('ANALIENX_RDC_RUNNER_REQUIRED: start_process must use cmd.exe only to launch the fixed Python Runner bridge');
    }
    const match = command.match(COMMAND_RE);
    if (!match || !REQUEST_RE.test(match[1])) {
        throw new Error(
            'ANALIENX_RDC_RUNNER_REQUIRED: direct process execution is disabled; ' +
            'write a typed request under C:\\Workspace\\.analienx\\rdc-requests and invoke the fixed rdc_runner_bridge.py'
        );
    }
    return { requestPath: match[1] };
}
