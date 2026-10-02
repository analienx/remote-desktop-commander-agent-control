import { createHash, randomUUID } from 'crypto';
import { spawnSync } from 'child_process';
import path from 'path';

const AUDIT_SCRIPT = path.join(
    process.env.LOCALAPPDATA || '',
    'Analienx', 'SLRunner', 'audit_log.py'
);
const AUDIT_ROOT = 'C:\\ProgramData\\Analienx\\runner\\audit';

function invoke(payload, strict) {
    const python = process.env.PYTHON || 'python';
    const result = spawnSync(
        python,
        [AUDIT_SCRIPT, 'append-native', '--root', AUDIT_ROOT],
        {
            input: JSON.stringify(payload),
            encoding: 'utf8',
            windowsHide: true,
            shell: false,
            timeout: 5000,
        },
    );
    if (result.error || result.status !== 0) {
        const detail = result.error?.message
            || String(result.stdout || result.stderr || 'audit helper failed').trim();
        if (strict) {
            throw new Error('ANALIENX_RDC_WRITE_AUDIT_REQUIRED: ' + detail);
        }
        return false;
    }
    return true;
}

export function beginNativeWrite(operation, paths, details = {}) {
    const jobId = randomUUID();
    const base = {
        operation,
        job_id: jobId,
        sequence: 0,
        paths,
        ...details,
    };
    invoke({ ...base, phase: 'received' }, true);
    return { jobId, operation, paths, details };
}

export function finishNativeWrite(context, details = {}) {
    invoke({
        operation: context.operation,
        job_id: context.jobId,
        sequence: 1,
        paths: context.paths,
        ...context.details,
        ...details,
        phase: 'succeeded',
    }, false);
}

export function failNativeWrite(context, error) {
    if (!context) return;
    invoke({
        operation: context.operation,
        job_id: context.jobId,
        sequence: 1,
        paths: context.paths,
        ...context.details,
        phase: 'failed',
        message: error instanceof Error ? error.message : String(error),
    }, false);
}

function boundedControlDetails(details = {}) {
    const pid = Number(details.pid);
    if (!Number.isInteger(pid) || pid <= 0) {
        throw new Error('ANALIENX_RDC_PROCESS_AUDIT_REQUIRED: invalid pid');
    }
    const safe = { pid };
    if (typeof details.input === 'string') {
        safe.input_sha256 = createHash('sha256').update(details.input, 'utf8').digest('hex');
        safe.input_bytes = Buffer.byteLength(details.input, 'utf8');
    }
    if (typeof details.termination_kind === 'string') {
        safe.termination_kind = details.termination_kind;
    }
    return safe;
}

export function beginNativeControl(operation, details = {}) {
    const jobId = randomUUID();
    const safe = boundedControlDetails(details);
    invoke({
        operation,
        job_id: jobId,
        sequence: 0,
        ...safe,
        phase: 'received',
    }, true);
    return { jobId, operation, details: safe, finished: false };
}

export function finishNativeControl(context) {
    if (!context || context.finished) return;
    context.finished = true;
    invoke({
        operation: context.operation,
        job_id: context.jobId,
        sequence: 1,
        ...context.details,
        phase: 'succeeded',
    }, false);
}

export function failNativeControl(context, error) {
    if (!context || context.finished) return;
    context.finished = true;
    invoke({
        operation: context.operation,
        job_id: context.jobId,
        sequence: 1,
        ...context.details,
        phase: 'failed',
        message: error instanceof Error ? error.message : String(error),
    }, false);
}
