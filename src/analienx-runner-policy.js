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
const MAX_EXECUTION_JSON_BYTES = 256 * 1024;
const SAFE_SHA256_RE = /^[0-9a-f]{64}$/;
const SAFE_AUTH_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
// The published RDC start_process interface exposes command/shell but not its
// installed `options` extension. This strict *routing-only* prefix carries an
// explicit owner through that public interface into the existing v2 contract.
// It is removed before shell execution and cannot grant authorization.
const INLINE_EXEC_PREFIX = /^\s*runner:exec\b/i;
const INLINE_EXEC_RE = /^runner:exec[ \t]+--project-id[ \t]+([A-Za-z0-9][A-Za-z0-9._-]{0,159})(?:[ \t]+--stream-id[ \t]+([A-Za-z0-9][A-Za-z0-9._-]{0,159}))?(?:[ \t]+--repository[ \t]+([A-Za-z0-9._-]+\/[A-Za-z0-9._-]+))?[ \t]+--worktree[ \t]+"([^"\r\n]+)"[ \t]+--[ \t]+([^\r\n][\s\S]*)$/;

function inlineExecutionDirective(original) {
  if (!INLINE_EXEC_PREFIX.test(original)) return null;
  const match = INLINE_EXEC_RE.exec(original);
  if (!match || !match[5].trim() || INLINE_EXEC_PREFIX.test(match[5])) {
    throw new Error(
      'ANALIENX_RDC_EXECUTION_IDENTITY_INVALID: expected ' +
      'runner:exec --project-id ID [--stream-id ID] [--repository owner/repo] ' +
      '--worktree "C:\\Workspace\\worktrees\\name" -- COMMAND'
    );
  }
  const [, project_id, stream_id, repository, worktree, command] = match;
  // No shell expansions, control tokens, or line breaks in the identity path.
  if (/[%!^&|<>;\r\n]/.test(worktree)) {
    throw new Error('ANALIENX_RDC_EXECUTION_IDENTITY_INVALID: unsafe worktree path');
  }
  const identity = { project_id, worktree };
  if (stream_id) identity.stream_id = stream_id;
  if (repository) identity.repository = repository;
  return {
    command,
    execution: parseRunnerOptions({ options: { execution: { schema: 1, identity } } }).execution,
  };
}

const CAPABILITY_ACTIONS = new Set([
  'read_many', 'list', 'search', 'repo_status',
  'snapshot', 'delta', 'system', 'processes',
]);
const CAPABILITY_FIELDS = new Set([
  'schema', 'action', 'paths', 'roots', 'query', 'max_results', 'max_bytes',
  'max_files', 'max_depth', 'snapshot_id', 'excludes', 'include_content',
]);

function boundedString(value, name, limit, optional = false) {
  if (value == null && optional) return null;
  if (typeof value !== 'string' || value.length < 1 || value.length > limit) {
    throw new Error('ANALIENX_RDC_OPTIONS_INVALID: ' + name + ' must be a non-empty string up to ' + limit + ' characters');
  }
  return value;
}

function parseRunnerOptions(args) {
  if (args?.options == null) return { capability: null, execution: null };
  const options = args.options;
  if (!options || typeof options !== 'object' || Array.isArray(options)) {
    throw new Error('ANALIENX_RDC_OPTIONS_INVALID: options must be an object');
  }
  const optionKeys = Object.keys(options);
  const unknownOptions = optionKeys.filter((key) => key !== 'capability' && key !== 'execution');
  if (unknownOptions.length > 0) {
    throw new Error('ANALIENX_RDC_OPTIONS_INVALID: unknown options fields: ' + unknownOptions.sort().join(', '));
  }

  let capability = null;
  if (options.capability != null) {
    capability = options.capability;
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
    capability = JSON.parse(encoded);
  }

  let execution = null;
  if (options.execution != null) {
    execution = options.execution;
    if (!execution || typeof execution !== 'object' || Array.isArray(execution)) {
      throw new Error('ANALIENX_RDC_OPTIONS_INVALID: options.execution must be an object');
    }
    const allowedExecution = new Set(['schema', 'identity', 'operation', 'authorization']);
    const unknown = Object.keys(execution).filter((key) => !allowedExecution.has(key));
    if (unknown.length > 0) {
      throw new Error('ANALIENX_RDC_OPTIONS_INVALID: unknown execution fields: ' + unknown.sort().join(', '));
    }
    if (execution.schema !== 1) {
      throw new Error('ANALIENX_RDC_OPTIONS_INVALID: execution schema must be 1');
    }
    const identity = execution.identity;
    if (!identity || typeof identity !== 'object' || Array.isArray(identity)) {
      throw new Error('ANALIENX_RDC_OPTIONS_INVALID: execution.identity must be an object');
    }
    const allowedIdentity = new Set(['project_id', 'worktree', 'repository', 'stream_id']);
    const unknownIdentity = Object.keys(identity).filter((key) => !allowedIdentity.has(key));
    if (unknownIdentity.length > 0) {
      throw new Error('ANALIENX_RDC_OPTIONS_INVALID: unknown execution.identity fields: ' + unknownIdentity.sort().join(', '));
    }
    boundedString(identity.project_id, 'execution.identity.project_id', 160);
    boundedString(identity.worktree, 'execution.identity.worktree', 512);
    if (!path.win32.isAbsolute(identity.worktree) || !insideWorkspace(identity.worktree) || !fs.existsSync(identity.worktree)) {
      throw new Error('ANALIENX_RDC_OPTIONS_INVALID: execution.identity.worktree must be an existing absolute workspace path');
    }
    if (identity.repository != null) boundedString(identity.repository, 'execution.identity.repository', 160);
    if (identity.stream_id != null) boundedString(identity.stream_id, 'execution.identity.stream_id', 160);

    if (execution.operation != null) {
      const operation = execution.operation;
      if (!operation || typeof operation !== 'object' || Array.isArray(operation)) {
        throw new Error('ANALIENX_RDC_OPTIONS_INVALID: execution.operation must be an object');
      }
      const unknownOperation = Object.keys(operation).filter((key) => key !== 'name' && key !== 'parameters');
      if (unknownOperation.length > 0) {
        throw new Error('ANALIENX_RDC_OPTIONS_INVALID: unknown execution.operation fields: ' + unknownOperation.sort().join(', '));
      }
      boundedString(operation.name, 'execution.operation.name', 120);
      if (operation.parameters != null &&
          (!operation.parameters || typeof operation.parameters !== 'object' || Array.isArray(operation.parameters))) {
        throw new Error('ANALIENX_RDC_OPTIONS_INVALID: execution.operation.parameters must be an object');
      }
    }

    if (execution.authorization != null) {
      const auth = execution.authorization;
      if (!auth || typeof auth !== 'object' || Array.isArray(auth)) {
        throw new Error('ANALIENX_RDC_OPTIONS_INVALID: execution.authorization must be an object');
      }
      const allowedAuth = new Set([
        'schema', 'kind', 'authorization_id', 'expires_at', 'max_attempts',
        'target', 'artifact_sha256', 'helper_sha256',
      ]);
      const unknownAuth = Object.keys(auth).filter((key) => !allowedAuth.has(key));
      if (unknownAuth.length > 0) {
        throw new Error('ANALIENX_RDC_OPTIONS_INVALID: unknown execution.authorization fields: ' + unknownAuth.sort().join(', '));
      }
      if (auth.schema !== 1 || !['user', 'supervisor', 'delegation'].includes(auth.kind)) {
        throw new Error('ANALIENX_RDC_OPTIONS_INVALID: invalid execution authorization schema/kind');
      }
      if (typeof auth.authorization_id !== 'string' || !SAFE_AUTH_ID_RE.test(auth.authorization_id)) {
        throw new Error('ANALIENX_RDC_OPTIONS_INVALID: invalid execution.authorization.authorization_id');
      }
      boundedString(auth.expires_at, 'execution.authorization.expires_at', 64);
      if (!Number.isInteger(auth.max_attempts) || auth.max_attempts < 1 || auth.max_attempts > 100) {
        throw new Error('ANALIENX_RDC_OPTIONS_INVALID: execution.authorization.max_attempts must be 1..100');
      }
      if (auth.target != null) boundedString(auth.target, 'execution.authorization.target', 512);
      for (const field of ['artifact_sha256', 'helper_sha256']) {
        if (auth[field] != null && (typeof auth[field] !== 'string' || !SAFE_SHA256_RE.test(auth[field]))) {
          throw new Error('ANALIENX_RDC_OPTIONS_INVALID: ' + field + ' must be lowercase SHA-256');
        }
      }
    }

    const encoded = JSON.stringify(execution);
    if (Buffer.byteLength(encoded, 'utf8') > MAX_EXECUTION_JSON_BYTES) {
      throw new Error('ANALIENX_RDC_OPTIONS_INVALID: execution contract is too large');
    }
    execution = JSON.parse(encoded);
  }

  if (capability && execution?.operation) {
    throw new Error('ANALIENX_RDC_OPTIONS_INVALID: capability and named operation are mutually exclusive');
  }
  return { capability, execution };
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
  const match = command.match(/^[^\S\r\n]*cd[^\S\r\n]+\/d[^\S\r\n]+(?:"([^"\r\n]+)"|([^&\r\n]+?))[^\S\r\n]*&&[^\S\r\n]*([\s\S]+)$/i);
  if (!match) {
    return { cwd: workspaceRoot(), command };
  }
  const cwd = String(match[1] || match[2] || '').trim();
  if (!path.win32.isAbsolute(cwd) || /[%!^\r\n]/.test(cwd) || !insideWorkspace(cwd)) {
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
  const parsed = parseRunnerOptions(args);
  const directive = inlineExecutionDirective(original);
  if (directive && (parsed.execution || parsed.capability)) {
    throw new Error('ANALIENX_RDC_EXECUTION_IDENTITY_INVALID: inline identity cannot be combined with typed options');
  }
  const { capability } = parsed;
  const execution = directive?.execution || parsed.execution;
  const command = directive?.command || original;
  if (INTERNAL_BRIDGE_RE.test(command)) {
    throw new Error('ANALIENX_RDC_RUNNER_REQUIRED: the internal Runner bridge cannot be invoked directly');
  }

  const bridge = bridgePath();
  if (!bridge || !fs.existsSync(bridge)) {
    throw new Error('ANALIENX_RDC_RUNNER_REQUIRED: RDC Runner bridge is not installed');
  }
  const originalShell = String(resolvedShell || args?.shell || process.env.COMSPEC || 'cmd.exe');
  const split = splitCmdWorkingDirectory(command, originalShell);
  const workspace = path.win32.resolve(workspaceRoot()).toLowerCase();
  const splitCwd = path.win32.resolve(split.cwd).toLowerCase();
  let effectiveCwd = split.cwd;
  if (execution) {
    const declaredWorktree = path.win32.resolve(execution.identity.worktree);
    const declaredKey = declaredWorktree.toLowerCase();
    if (splitCwd === workspace) {
      effectiveCwd = declaredWorktree;
    } else if (!(splitCwd === declaredKey || splitCwd.startsWith(declaredKey + '\\'))) {
      throw new Error('ANALIENX_RDC_EXECUTION_IDENTITY_MISMATCH: command cwd is outside declared worktree');
    }
    if (execution.operation) {
      if (String(split.command || '').trim().toLowerCase() !== 'runner:operation') {
        throw new Error('ANALIENX_RDC_OPTIONS_INVALID: named operation requires command runner:operation');
      }
    } else if (String(split.command || '').trim().toLowerCase() === 'runner:operation') {
      throw new Error('ANALIENX_RDC_OPTIONS_INVALID: runner:operation requires execution.operation');
    }
  }
  const effectiveKey = path.win32.resolve(effectiveCwd).toLowerCase();
  if (capability) {
    if (String(split.command || '').trim().toLowerCase() !== 'runner:capability') {
      throw new Error(
        'ANALIENX_RDC_OPTIONS_INVALID: capability options require command runner:capability'
      );
    }
    if (
      effectiveKey === workspace &&
      capability.action !== 'system' &&
      capability.action !== 'processes'
    ) {
      throw new Error(
        'ANALIENX_RDC_CONTEXT_REQUIRED: repo capability calls require ' +
        'cd /d "<registered worktree>" && runner:capability'
      );
    }
  }
  // A repo path may be an input, helper script, or diagnostic target. It is not
  // evidence of an execution cwd. Forward unresolved ownership to the bridge's
  // canonical TaskRouter preflight, which must succeed before any child starts.
  const initiative = (
    !execution &&
    capability &&
    effectiveKey === workspace &&
    (capability.action === 'system' || capability.action === 'processes')
  ) ? {
    initiative_id: 'workstation-ops',
    project: 'supervisor-control-plane',
    repository: 'analienx/config',
    stream: 'runner-redesign',
    activity_type: 'host-observability',
    classified: true,
  } : resolveInitiativeContext(effectiveCwd, split.command);
  const root = requestRoot();
  fs.mkdirSync(root, { recursive: true });
  cleanupStaleRequests(root);
  const requestPath = path.win32.join(root, crypto.randomUUID() + '.json');
  const payload = {
    schema: execution ? 'analienx.rdc-slrunner-request/v2' : 'analienx.rdc-slrunner-request/v1',
    cwd: effectiveCwd,
    ...(execution?.operation ? {} : (capability ? { capability } : { command: split.command })),
    ...(execution ? { execution } : {}),
    shell: originalShell,
    initiative_id: initiative.initiative_id,
    activity_type: initiative.activity_type,
    project: execution?.identity.project_id || initiative.project,
    repository: execution?.identity.repository || initiative.repository,
    worktree: execution?.identity.worktree || effectiveCwd,
    stream: execution?.identity.stream_id || initiative.stream,
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
