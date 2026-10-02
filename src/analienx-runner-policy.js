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

function initiativeRegistryPath() {
  return process.env.ANALIENX_RDC_INITIATIVE_REGISTRY ||
    path.win32.join(workspaceRoot(), '.analienx', 'runner', 'initiatives.json');
}

function repoIndexPath() {
  return process.env.ANALIENX_RDC_REPO_INDEX ||
    path.win32.join(workspaceRoot(), '.analienx', 'runner', 'repo-index.json');
}

const INTERNAL_BRIDGE_RE = /(?:^|&&|\|\||[;&|])\s*"?(?:python(?:\.exe)?|py(?:\.exe)?)"?\s+[^&|;\r\n]*rdc_runner_bridge\.py/i;
const SAFE_INITIATIVE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/;
const HOST_OBSERVABILITY_RE = /(?:get-process|get-ciminstance\s+win32_process|get-scheduledtask|get-service|\btasklist\b|uiautomation)/i;
const MAX_ROUTING_FILE_BYTES = 4 * 1024 * 1024;
const MAX_ROUTING_ENTRIES = 5000;
const MAX_CAPABILITY_JSON_BYTES = 256 * 1024;
const CAPABILITY_ACTIONS = new Set([
  'read_many', 'list', 'search', 'repo_status',
  'snapshot', 'delta', 'system', 'processes',
]);
const CAPABILITY_FIELDS = new Set([
  'schema', 'action', 'paths', 'roots', 'query', 'max_results', 'max_bytes',
  'max_files', 'max_depth', 'snapshot_id', 'excludes', 'include_content',
]);

function parseCapabilityOptions(args) {
  if (args?.options == null) return null;
  const options = args.options;
  if (!options || typeof options !== 'object' || Array.isArray(options)) {
    throw new Error('ANALIENX_RDC_OPTIONS_INVALID: options must be an object');
  }
  const optionKeys = Object.keys(options);
  if (optionKeys.some((key) => key !== 'capability')) {
    throw new Error('ANALIENX_RDC_OPTIONS_INVALID: only options.capability is supported');
  }
  const capability = options.capability;
  if (!capability || typeof capability !== 'object' || Array.isArray(capability)) {
    throw new Error('ANALIENX_RDC_OPTIONS_INVALID: options.capability must be an object');
  }
  const unknown = Object.keys(capability).filter((key) => !CAPABILITY_FIELDS.has(key));
  if (unknown.length > 0) {
    throw new Error('ANALIENX_RDC_OPTIONS_INVALID: unknown capability fields: ' + unknown.sort().join(', '));
  }
  if (capability.schema !== 1) {
    throw new Error('ANALIENX_RDC_OPTIONS_INVALID: capability schema must be 1');
  }
  if (!CAPABILITY_ACTIONS.has(capability.action)) {
    throw new Error('ANALIENX_RDC_OPTIONS_INVALID: unsupported capability action');
  }
  const encoded = JSON.stringify(capability);
  if (Buffer.byteLength(encoded, 'utf8') > MAX_CAPABILITY_JSON_BYTES) {
    throw new Error('ANALIENX_RDC_OPTIONS_INVALID: capability request is too large');
  }
  return JSON.parse(encoded);
}

function readBoundedRoutingJson(file) {
  const stat = fs.statSync(file);
  if (!stat.isFile() || stat.size > MAX_ROUTING_FILE_BYTES) return null;
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

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

function resolveInitiativeContext(cwd, command = '') {
  const registry = initiativeRegistryPath();
  if (fs.existsSync(registry)) {
    try {
      const payload = readBoundedRoutingJson(registry);
      if (payload?.schema === 1 && Array.isArray(payload.bindings) &&
          payload.bindings.length <= MAX_ROUTING_ENTRIES) {
        const value = path.win32.resolve(cwd).toLowerCase();
        const matches = payload.bindings
          .filter((item) => item && typeof item.root === 'string' && typeof item.initiative_id === 'string')
          .map((item) => ({ item, root: path.win32.resolve(item.root).toLowerCase() }))
          .filter(({ item, root }) =>
            insideWorkspace(root) &&
            fs.existsSync(item.root) &&
            SAFE_INITIATIVE_RE.test(item.initiative_id) &&
            (item.project == null || (typeof item.project === 'string' && item.project.length > 0 && item.project.length <= 160)) &&
            (item.repository == null || (typeof item.repository === 'string' && item.repository.length > 0 && item.repository.length <= 160)) &&
            (item.stream == null || (typeof item.stream === 'string' && item.stream.length > 0 && item.stream.length <= 160)) &&
            (item.activity_type == null || (typeof item.activity_type === 'string' && item.activity_type.length > 0 && item.activity_type.length <= 120)) &&
            (value === root || value.startsWith(root + '\\')))
          .sort((a, b) => b.root.length - a.root.length);
        if (matches.length > 0) {
          return {
            initiative_id: matches[0].item.initiative_id,
            project: matches[0].item.project || null,
            repository: matches[0].item.repository || null,
            stream: matches[0].item.stream || null,
            activity_type: matches[0].item.activity_type || 'rdc-command',
            classified: true,
          };
        }
      }
    } catch {
      // Invalid registry must not be silently trusted. Fall through to a visible
      // unclassified bucket; the Runner UI makes this obvious to the operator.
    }
  }
  const resolvedCwd = path.win32.resolve(cwd);
  const repoIndex = repoIndexPath();
  if (fs.existsSync(repoIndex)) {
    try {
      const payload = readBoundedRoutingJson(repoIndex);
      if (payload?.schema === 1 && Array.isArray(payload.entries) &&
          payload.entries.length <= MAX_ROUTING_ENTRIES) {
        const value = resolvedCwd.toLowerCase();
        const matches = payload.entries
          .filter((item) => item && typeof item.root === 'string')
          .map((item) => ({ item, root: path.win32.resolve(item.root).toLowerCase() }))
          .filter(({ item, root }) =>
            insideWorkspace(root) &&
            fs.existsSync(item.root) &&
            typeof item.project === 'string' && item.project.length > 0 && item.project.length <= 160 &&
            (item.repository == null || (typeof item.repository === 'string' && item.repository.length <= 160)) &&
            (item.stream == null || (typeof item.stream === 'string' && item.stream.length <= 160)) &&
            (value === root || value.startsWith(root + '\\')))
          .sort((a, b) => b.root.length - a.root.length);
        if (matches.length > 0) {
          const item = matches[0].item;
          const slug = String(item.project).replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '');
          return {
            initiative_id: ('adhoc-' + (slug || 'repo')).slice(0, 120),
            project: item.project,
            repository: item.repository || null,
            stream: item.stream || null,
            activity_type: 'rdc-command',
            classified: true,
            inferred: true,
          };
        }
      }
    } catch {
      // A stale/malformed discovery index must never override explicit bindings.
    }
  }
  if (
    resolvedCwd.toLowerCase() === path.win32.resolve(workspaceRoot()).toLowerCase() &&
    HOST_OBSERVABILITY_RE.test(String(command || ''))
  ) {
    return {
      initiative_id: 'workstation-ops',
      project: 'supervisor-control-plane',
      repository: 'analienx/config',
      stream: 'runner-redesign',
      activity_type: 'host-observability',
      classified: true,
    };
  }
  const leaf = path.win32.basename(resolvedCwd) || 'workspace';
  const slug = leaf.replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '') || 'workspace';
  return {
    initiative_id: ('unclassified-' + slug).slice(0, 120),
    project: null,
    repository: null,
    stream: null,
    activity_type: 'rdc-command',
    classified: false,
  };
}

const DEFAULT_JOB_TIMEOUT_SECONDS = 86400;
const STALE_REQUEST_MS = 48 * 60 * 60 * 1000;

function cleanupStaleRequests(root) {
  const now = Date.now();
  try {
    for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
      if (!entry.isFile() || !entry.name.endsWith('.json')) continue;
      const candidate = path.win32.join(root, entry.name);
      try {
        const stat = fs.statSync(candidate);
        if (now - stat.mtimeMs > STALE_REQUEST_MS) fs.unlinkSync(candidate);
      } catch {
        // Best-effort hygiene only; request creation below remains fail-closed.
      }
    }
  } catch {
    // Directory read failure is surfaced later if the new request cannot be written.
  }
}


export async function routeAnalienxRunner(args, resolvedShell) {
  const original = String(args?.command ?? '');
  if (!original.trim()) {
    throw new Error('ANALIENX_RDC_RUNNER_REQUIRED: empty process command');
  }
  const capability = parseCapabilityOptions(args);
  if (INTERNAL_BRIDGE_RE.test(original)) {
    throw new Error('ANALIENX_RDC_RUNNER_REQUIRED: the internal Runner bridge cannot be invoked directly');
  }

  const bridge = bridgePath();
  if (!bridge || !fs.existsSync(bridge)) {
    throw new Error('ANALIENX_RDC_RUNNER_REQUIRED: RDC Runner bridge is not installed');
  }
  const originalShell = String(resolvedShell || args?.shell || process.env.COMSPEC || 'cmd.exe');
  const split = splitCmdWorkingDirectory(original, originalShell);
  const workspace = path.win32.resolve(workspaceRoot()).toLowerCase();
  const splitCwd = path.win32.resolve(split.cwd).toLowerCase();
  if (capability) {
    if (String(split.command || '').trim().toLowerCase() !== 'runner:capability') {
      throw new Error(
        'ANALIENX_RDC_OPTIONS_INVALID: capability options require command runner:capability'
      );
    }
    if (
      splitCwd === workspace &&
      capability.action !== 'system' &&
      capability.action !== 'processes'
    ) {
      throw new Error(
        'ANALIENX_RDC_CONTEXT_REQUIRED: repo capability calls require ' +
        'cd /d "<registered worktree>" && runner:capability'
      );
    }
  }
  const normalizedCommand = original.replaceAll('/', '\\').toLowerCase();
  const referencesRepoScope =
    normalizedCommand.includes(workspace + '\\worktrees\\') ||
    normalizedCommand.includes(workspace + '\\repos\\');
  if (splitCwd === workspace && referencesRepoScope) {
    throw new Error(
      'ANALIENX_RDC_CONTEXT_REQUIRED: repo-scoped RDC process commands must start with ' +
      'cd /d "<registered worktree>" && so Runner attribution is explicit'
    );
  }
  const initiative = (
    capability &&
    splitCwd === workspace &&
    (capability.action === 'system' || capability.action === 'processes')
  ) ? {
    initiative_id: 'workstation-ops',
    project: 'supervisor-control-plane',
    repository: 'analienx/config',
    stream: 'runner-redesign',
    activity_type: 'host-observability',
    classified: true,
  } : resolveInitiativeContext(split.cwd, split.command);
  const root = requestRoot();
  fs.mkdirSync(root, { recursive: true });
  cleanupStaleRequests(root);
  const requestPath = path.win32.join(root, crypto.randomUUID() + '.json');
  const payload = {
    schema: 'analienx.rdc-slrunner-request/v1',
    cwd: split.cwd,
    ...(capability ? { capability } : { command: split.command }),
    shell: originalShell,
    initiative_id: initiative.initiative_id,
    activity_type: initiative.activity_type,
    project: initiative.project,
    repository: initiative.repository,
    worktree: split.cwd,
    stream: initiative.stream,
    category: initiative.classified ? 'RDC' : 'RDC-UNCLASSIFIED',
    // Desktop Commander's timeout_ms controls how long start_process waits for
    // initial output; it is not a child-process lifetime. Keep Runner lifetime
    // independent so interactive and durable jobs survive the initial tool return.
    timeout_seconds: DEFAULT_JOB_TIMEOUT_SECONDS,
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
