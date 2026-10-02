import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';
import {
  routeAnalienxRunner,
  splitCmdWorkingDirectory,
} from '../src/analienx-runner-policy.js';

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'rdc-policy-'));
  const workspace = path.join(root, 'Workspace');
  const requests = path.join(workspace, '.analienx', 'rdc-requests');
  const bridge = path.join(root, 'RDC-Control', 'rdc_runner_bridge.py');
  const initiatives = path.join(workspace, '.analienx', 'runner', 'initiatives.json');
  const repoIndex = path.join(workspace, '.analienx', 'runner', 'repo-index.json');
  fs.mkdirSync(path.dirname(bridge), { recursive: true });
  fs.mkdirSync(workspace, { recursive: true });
  fs.writeFileSync(bridge, '# fixture\n');
  const prior = {
    workspace: process.env.ANALIENX_RDC_WORKSPACE_ROOT,
    requests: process.env.ANALIENX_RDC_REQUEST_ROOT,
    bridge: process.env.ANALIENX_RDC_BRIDGE,
    initiatives: process.env.ANALIENX_RDC_INITIATIVE_REGISTRY,
    repoIndex: process.env.ANALIENX_RDC_REPO_INDEX,
  };
  process.env.ANALIENX_RDC_WORKSPACE_ROOT = workspace;
  process.env.ANALIENX_RDC_REQUEST_ROOT = requests;
  process.env.ANALIENX_RDC_BRIDGE = bridge;
  process.env.ANALIENX_RDC_INITIATIVE_REGISTRY = initiatives;
  process.env.ANALIENX_RDC_REPO_INDEX = repoIndex;
  return {
    root, workspace, requests, bridge, initiatives, repoIndex,
    close() {
      if (prior.workspace === undefined) delete process.env.ANALIENX_RDC_WORKSPACE_ROOT;
      else process.env.ANALIENX_RDC_WORKSPACE_ROOT = prior.workspace;
      if (prior.requests === undefined) delete process.env.ANALIENX_RDC_REQUEST_ROOT;
      else process.env.ANALIENX_RDC_REQUEST_ROOT = prior.requests;
      if (prior.bridge === undefined) delete process.env.ANALIENX_RDC_BRIDGE;
      else process.env.ANALIENX_RDC_BRIDGE = prior.bridge;
      if (prior.initiatives === undefined) delete process.env.ANALIENX_RDC_INITIATIVE_REGISTRY;
      else process.env.ANALIENX_RDC_INITIATIVE_REGISTRY = prior.initiatives;
      if (prior.repoIndex === undefined) delete process.env.ANALIENX_RDC_REPO_INDEX;
      else process.env.ANALIENX_RDC_REPO_INDEX = prior.repoIndex;
      fs.rmSync(root, { recursive: true, force: true });
    },
  };
}

test('ordinary process command is transparently routed', async () => {
  const f = fixture();
  try {
    const routed = await routeAnalienxRunner(
      { command: 'git status', shell: 'cmd.exe', timeout_ms: 12000 },
      'cmd.exe',
    );
    assert.equal(routed.shell, 'cmd.exe');
    assert.match(routed.command, /rdc_runner_bridge\.py/);
    assert.ok(fs.existsSync(routed.requestPath));
    const request = JSON.parse(fs.readFileSync(routed.requestPath, 'utf8'));
    assert.equal(request.command, 'git status');
    assert.equal(request.shell, 'cmd.exe');
    assert.equal(request.cwd, f.workspace);
    assert.equal(request.timeout_seconds, 86400);
    assert.match(request.initiative_id, /^unclassified-/);
    assert.equal(request.category, 'RDC-UNCLASSIFIED');
  } finally {
    f.close();
  }
});

test('repo index removes unnecessary unclassified worktree buckets', async () => {
  const f = fixture();
  try {
    const repo = path.join(f.workspace, 'worktrees', 'config-muse-goals');
    fs.mkdirSync(repo, { recursive: true });
    fs.mkdirSync(path.dirname(f.repoIndex), { recursive: true });
    fs.writeFileSync(f.repoIndex, JSON.stringify({
      schema: 1,
      entries: [{
        root: repo,
        repository: 'analienx/config',
        project: 'supervisor-control-plane',
        stream: null,
      }],
    }, null, 2));
    const routed = await routeAnalienxRunner(
      { command: 'cd /d "' + repo + '" && git status', shell: 'cmd.exe' },
      'cmd.exe',
    );
    const request = JSON.parse(fs.readFileSync(routed.requestPath, 'utf8'));
    assert.equal(request.initiative_id, 'adhoc-supervisor-control-plane');
    assert.equal(request.project, 'supervisor-control-plane');
    assert.equal(request.repository, 'analienx/config');
    assert.equal(request.category, 'RDC');
  } finally {
    f.close();
  }
});

test('read-only host inspection gets a stable workstation bucket', async () => {
  const f = fixture();
  try {
    const routed = await routeAnalienxRunner(
      { command: 'powershell.exe -NoProfile -Command "Get-Process | Select-Object -First 5"', shell: 'cmd.exe' },
      'cmd.exe',
    );
    const request = JSON.parse(fs.readFileSync(routed.requestPath, 'utf8'));
    assert.equal(request.initiative_id, 'workstation-ops');
    assert.equal(request.project, 'supervisor-control-plane');
    assert.equal(request.activity_type, 'host-observability');
    assert.equal(request.category, 'RDC');
  } finally {
    f.close();
  }
});

test('leading cmd cd /d becomes runner cwd instead of wrapper logic', async () => {
  const f = fixture();
  try {
    const repo = path.join(f.workspace, 'worktrees', 'cinema-feral');
    fs.mkdirSync(repo, { recursive: true });
    fs.mkdirSync(path.dirname(f.initiatives), { recursive: true });
    fs.writeFileSync(f.initiatives, JSON.stringify({
      schema: 1,
      bindings: [{ root: repo, initiative_id: 'feral-60s-trailer', project: 'cinema', repository: 'analienx/cinema', stream: 'autonomous-production', activity_type: 'keyframe-generation' }],
    }, null, 2));
    const command = 'cd /d "' + repo + '" && node tool.mjs --check';
    const routed = await routeAnalienxRunner({ command, shell: 'cmd.exe' }, 'cmd.exe');
    const request = JSON.parse(fs.readFileSync(routed.requestPath, 'utf8'));
    assert.equal(path.win32.normalize(request.cwd), path.win32.normalize(repo));
    assert.equal(request.command, 'node tool.mjs --check');
    assert.equal(request.initiative_id, 'feral-60s-trailer');
    assert.equal(request.project, 'cinema');
    assert.equal(request.repository, 'analienx/cinema');
    assert.equal(path.win32.normalize(request.worktree), path.win32.normalize(repo));
    assert.equal(request.stream, 'autonomous-production');
    assert.equal(request.activity_type, 'keyframe-generation');
    assert.equal(request.category, 'RDC');
  } finally {
    f.close();
  }
});

test('repo-scoped process commands without explicit cwd fail instead of becoming unclassified', async () => {
  const f = fixture();
  try {
    const repo = path.join(f.workspace, 'worktrees', 'cinema-feral');
    fs.mkdirSync(repo, { recursive: true });
    await assert.rejects(
      routeAnalienxRunner(
        { command: 'python "' + path.join(repo, 'tool.py') + '"', shell: 'cmd.exe' },
        'cmd.exe',
      ),
      /ANALIENX_RDC_CONTEXT_REQUIRED/,
    );
  } finally {
    f.close();
  }
});

test('malformed or out-of-workspace initiative binding is ignored', async () => {
  const f = fixture();
  try {
    const repo = path.join(f.workspace, 'worktrees', 'cinema-feral');
    const outside = path.join(f.root, 'outside');
    fs.mkdirSync(repo, { recursive: true });
    fs.mkdirSync(outside, { recursive: true });
    fs.mkdirSync(path.dirname(f.initiatives), { recursive: true });
    fs.writeFileSync(f.initiatives, JSON.stringify({
      schema: 1,
      bindings: [
        { root: repo, initiative_id: 'not valid spaces', project: 'cinema' },
        { root: outside, initiative_id: 'outside-scope', project: 'cinema' },
      ],
    }, null, 2));
    const command = 'cd /d "' + repo + '" && git status';
    const routed = await routeAnalienxRunner({ command, shell: 'cmd.exe' }, 'cmd.exe');
    const request = JSON.parse(fs.readFileSync(routed.requestPath, 'utf8'));
    assert.match(request.initiative_id, /^unclassified-/);
    assert.equal(request.category, 'RDC-UNCLASSIFIED');
  } finally {
    f.close();
  }
});

test('stale request files are cleaned before routing', async () => {
  const f = fixture();
  try {
    fs.mkdirSync(f.requests, { recursive: true });
    const stale = path.join(f.requests, 'stale.json');
    fs.writeFileSync(stale, '{}');
    const old = new Date(Date.now() - 72 * 60 * 60 * 1000);
    fs.utimesSync(stale, old, old);
    const routed = await routeAnalienxRunner({ command: 'git status', shell: 'cmd.exe' }, 'cmd.exe');
    assert.equal(fs.existsSync(stale), false);
    assert.equal(fs.existsSync(routed.requestPath), true);
  } finally {
    f.close();
  }
});

test('resolved PowerShell shell is preserved inside the request', async () => {
  const f = fixture();
  try {
    const routed = await routeAnalienxRunner(
      { command: 'Write-Output hi' },
      'powershell.exe',
    );
    const request = JSON.parse(fs.readFileSync(routed.requestPath, 'utf8'));
    assert.equal(request.shell, 'powershell.exe');
    assert.equal(request.command, 'Write-Output hi');
  } finally {
    f.close();
  }
});

test('bridge filename may be mentioned by benign git commands', async () => {
  const f = fixture();
  try {
    const routed = await routeAnalienxRunner(
      { command: 'git add src/rdc_runner_bridge.py', shell: 'cmd.exe' },
      'cmd.exe',
    );
    assert.ok(fs.existsSync(routed.requestPath));
  } finally {
    f.close();
  }
});

test('internal bridge cannot be manually nested', async () => {
  const f = fixture();
  try {
    await assert.rejects(
      routeAnalienxRunner(
        { command: 'python rdc_runner_bridge.py --request x.json', shell: 'cmd.exe' },
        'cmd.exe',
      ),
      /cannot be invoked directly/,
    );
  } finally {
    f.close();
  }
});

test('split helper leaves out-of-workspace cd intact', () => {
  const f = fixture();
  try {
    const original = 'cd /d "D:\\Elsewhere" && git status';
    assert.deepEqual(
      splitCmdWorkingDirectory(original, 'cmd.exe'),
      { cwd: f.workspace, command: original },
    );
  } finally {
    f.close();
  }
});
