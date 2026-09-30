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
  fs.mkdirSync(path.dirname(bridge), { recursive: true });
  fs.mkdirSync(workspace, { recursive: true });
  fs.writeFileSync(bridge, '# fixture\n');
  const prior = {
    workspace: process.env.ANALIENX_RDC_WORKSPACE_ROOT,
    requests: process.env.ANALIENX_RDC_REQUEST_ROOT,
    bridge: process.env.ANALIENX_RDC_BRIDGE,
  };
  process.env.ANALIENX_RDC_WORKSPACE_ROOT = workspace;
  process.env.ANALIENX_RDC_REQUEST_ROOT = requests;
  process.env.ANALIENX_RDC_BRIDGE = bridge;
  return {
    root, workspace, requests, bridge,
    close() {
      if (prior.workspace === undefined) delete process.env.ANALIENX_RDC_WORKSPACE_ROOT;
      else process.env.ANALIENX_RDC_WORKSPACE_ROOT = prior.workspace;
      if (prior.requests === undefined) delete process.env.ANALIENX_RDC_REQUEST_ROOT;
      else process.env.ANALIENX_RDC_REQUEST_ROOT = prior.requests;
      if (prior.bridge === undefined) delete process.env.ANALIENX_RDC_BRIDGE;
      else process.env.ANALIENX_RDC_BRIDGE = prior.bridge;
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
  } finally {
    f.close();
  }
});

test('leading cmd cd /d becomes runner cwd instead of wrapper logic', async () => {
  const f = fixture();
  try {
    const repo = path.join(f.workspace, 'worktrees', 'cinema-feral');
    fs.mkdirSync(repo, { recursive: true });
    const command = 'cd /d "' + repo + '" && node tool.mjs --check';
    const routed = await routeAnalienxRunner({ command, shell: 'cmd.exe' }, 'cmd.exe');
    const request = JSON.parse(fs.readFileSync(routed.requestPath, 'utf8'));
    assert.equal(path.win32.normalize(request.cwd), path.win32.normalize(repo));
    assert.equal(request.command, 'node tool.mjs --check');
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
