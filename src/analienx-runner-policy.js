import fs from 'fs';
import path from 'path';
import crypto from 'crypto';

function workspaceRoot() {
  return process.env.ANALIENX_RDC_WORKSPACE_ROOT || 'C:\\Workspace';
}

function requestRoot() {
  return process.env.ANALIENX_RDC_REQUEST_ROOT ||
    path.win32.join(workspaceRoot(), '.analienx', 'rdc-requests');
}

function bridgePath() {
  return process.env.ANALIENX_RDC_BRIDGE ||
    path.win32.join(process.env.LOCALAPPDATA || '', 'RDC-Control', 'rdc_runner_bridge.py');
}

const INTERNAL_BRIDGE_RE = /rdc_runner_bridge\.py/i;

function insideWorkspace(candidate) {
  const root = path.win32.resolve(workspaceRoot()).toLowerCase();
  const value = path.win32.resolve(candidate).toLowerCase();
  return value === root || value.startsWith(root + '\\');
}

export function splitCmdWorkingDirectory(command, shell) {
  const base = path.win32.basename(shell || '').toLowerCase();
  if (base !== 'cmd.exe' && base !== 'cmd') {
    return { cwd: workspaceRoot(), command };
  }
  const match = command.match(/^\s*cd\s+\/d\s+(?:"([^"]+)"|([^&]+?))\s*&&\s*([\s\S]+)$/i);
  if (!match) {
    return { cwd: workspaceRoot(), command };
  }
  const cwd = String(match[1] || match[2] || '').trim();
  if (!insideWorkspace(cwd)) {
    return { cwd: workspaceRoot(), command };
  }
  return { cwd: path.win32.resolve(cwd), command: match[3] };
}

function timeoutSeconds(value) {
  const ms = Number(value);
  if (!Number.isFinite(ms) || ms <= 0) return 300;
  return Math.max(1, Math.min(86400, Math.ceil(ms / 1000)));
}

export async function routeAnalienxRunner(args, resolvedShell) {
  const original = String(args?.command ?? '');
  if (!original.trim()) {
    throw new Error('ANALIENX_RDC_RUNNER_REQUIRED: empty process command');
  }
  if (INTERNAL_BRIDGE_RE.test(original)) {
    throw new Error('ANALIENX_RDC_RUNNER_REQUIRED: the internal Runner bridge cannot be invoked directly');
  }

  const bridge = bridgePath();
  if (!bridge || !fs.existsSync(bridge)) {
    throw new Error('ANALIENX_RDC_RUNNER_REQUIRED: RDC Runner bridge is not installed');
  }
  const originalShell = String(resolvedShell || args?.shell || process.env.COMSPEC || 'cmd.exe');
  const split = splitCmdWorkingDirectory(original, originalShell);
  const root = requestRoot();
  fs.mkdirSync(root, { recursive: true });
  const requestPath = path.win32.join(root, crypto.randomUUID() + '.json');
  const payload = {
    schema: 'analienx.rdc-slrunner-request/v1',
    cwd: split.cwd,
    command: split.command,
    shell: originalShell,
    category: 'RDC',
    timeout_seconds: timeoutSeconds(args?.timeout_ms),
    heartbeat_seconds: 15,
  };
  fs.writeFileSync(requestPath, JSON.stringify(payload, null, 2) + '\n', {
    encoding: 'utf8',
    flag: 'wx',
  });
  return {
    command: 'python.exe "' + bridge + '" --request "' + requestPath + '"',
    shell: 'cmd.exe',
    requestPath,
    originalCommand: original,
    originalShell,
  };
}
