import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { routeAnalienxRunner } from '../src/analienx-runner-policy.js';

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'rdc-inline-'));
  const workspace = path.join(root, 'Workspace');
  const worktree = path.join(workspace, 'worktrees', 'ha-p10-original-network-recovery');
  const bridge = path.join(root, 'RDC-Control', 'rdc_runner_bridge.py');
  const requests = path.join(workspace, '.analienx', 'rdc-requests');
  fs.mkdirSync(worktree, { recursive: true });
  fs.mkdirSync(path.dirname(bridge), { recursive: true });
  fs.writeFileSync(bridge, '# fixture\n');
  const keys = {
    ANALIENX_RDC_WORKSPACE_ROOT: workspace,
    ANALIENX_RDC_REQUEST_ROOT: requests,
    ANALIENX_RDC_BRIDGE: bridge,
  };
  const old = Object.fromEntries(Object.keys(keys).map(k => [k, process.env[k]]));
  Object.assign(process.env, keys);
  return {
    worktree, workspace,
    close() {
      for (const [k, value] of Object.entries(old)) {
        if (value === undefined) delete process.env[k]; else process.env[k] = value;
      }
      fs.rmSync(root, { recursive: true, force: true });
    },
  };
}
const envelope = wt => 'runner:exec --project-id home-assistant --stream-id zigbee-coordinator-migration --repository analienx/home-assistant-stack --worktree "' + wt + '" -- ';

test('worktree-only context and PowerShell location preserve foreign helper arguments', async () => {
  const f = fixture();
  try {
    const command = 'python "C:\\Workspace\\repos\\config\\helper.py" --issue analienx/other#73';
    for (const [prefix, shell] of [
      ['runner:cwd "' + f.worktree + '" -- ', 'cmd.exe'],
      ['Set-Location -LiteralPath "' + f.worktree + '"; ', 'powershell.exe'],
      ["cd '" + f.worktree + "' && ", 'pwsh.exe'],
    ]) {
      const routed = await routeAnalienxRunner({ command: prefix + command, shell }, shell);
      const req = JSON.parse(fs.readFileSync(routed.requestPath, 'utf8'));
      assert.equal(req.cwd, f.worktree);
      assert.equal(req.command, command);
      assert.equal(req.execution, undefined); // Canonical bridge derives owner.
    }
    const next = await routeAnalienxRunner({command:'echo second-session', shell:'cmd.exe'}, 'cmd.exe');
    assert.equal(JSON.parse(fs.readFileSync(next.requestPath)).cwd, f.workspace);
  } finally { f.close(); }
});

test('invalid worktree-only contexts and internal bridge invocation fail closed', async () => {
  const f = fixture();
  try {
    for (const command of [
      'runner:cwd "D:\\Elsewhere" -- echo bad',
      'runner:cwd "' + f.workspace + '/missing" -- echo bad',
      'runner:cwd "' + f.worktree + '" echo bad',
      'runner:cwd "' + f.worktree + '" -- runner:cwd "' + f.worktree + '" -- echo bad',
      'runner:cwd "' + f.worktree + '" -- python rdc_runner_bridge.py --request x.json',
    ]) {
      await assert.rejects(routeAnalienxRunner({command, shell:'cmd.exe'}, 'cmd.exe'),
        /ANALIENX_RDC_(CONTEXT_INVALID|RUNNER_REQUIRED)/);
    }
  } finally { f.close(); }
});

test('inline identity is stripped from the shell and encoded as v2 execution identity', async () => {
  const f = fixture();
  try {
    const executable = 'python "C:\\Workspace\\repos\\config\\supervisor\\tool.py" --issue analienx/config#2';
    const result = await routeAnalienxRunner(
      { command: envelope(f.worktree) + executable, shell: 'cmd.exe' }, 'cmd.exe');
    const req = JSON.parse(fs.readFileSync(result.requestPath, 'utf8'));
    assert.equal(req.schema, 'analienx.rdc-slrunner-request/v2');
    assert.equal(req.cwd, f.worktree);
    assert.equal(req.project, 'home-assistant');
    assert.equal(req.stream, 'zigbee-coordinator-migration');
    assert.equal(req.repository, 'analienx/home-assistant-stack');
    assert.equal(req.command, executable);
    assert.deepEqual(req.execution, {
      schema: 1,
      identity: {
        project_id: 'home-assistant', stream_id: 'zigbee-coordinator-migration',
        repository: 'analienx/home-assistant-stack', worktree: f.worktree,
      },
    });
    assert.doesNotMatch(req.command, /runner:exec/);
  } finally { f.close(); }
});

test('inline identity supports read-only commands without a repository field', async () => {
  const f = fixture();
  try {
    const command = 'runner:exec --project-id home-assistant --stream-id zigbee-coordinator-migration --worktree "' + f.worktree + '" -- git status -sb';
    const routed = await routeAnalienxRunner({command, shell:'cmd.exe'}, 'cmd.exe');
    const req = JSON.parse(fs.readFileSync(routed.requestPath, 'utf8'));
    assert.equal(req.execution.identity.repository, undefined);
    assert.equal(req.command, 'git status -sb');
    assert.equal(req.worktree, f.worktree);
  } finally { f.close(); }
});

test('malformed, injected, and doubly-specified inline identities fail closed', async () => {
  const f = fixture();
  try {
    const invalid = [
      'runner:exec --project-id home-assistant --worktree "D:\\Elsewhere" -- git status',
      'runner:exec --project-id home-assistant --worktree "' + f.worktree + '&whoami" -- git status',
      'runner:exec --project-id home-assistant --worktree "' + f.worktree + '" git status',
      envelope(f.worktree) + 'runner:exec --project-id x --worktree "' + f.worktree + '" -- whoami',
      'runner:exec --project-id home-assistant --worktree "' + f.worktree + '" --  ',
    ];
    for (const command of invalid) {
      await assert.rejects(routeAnalienxRunner({ command, shell:'cmd.exe' }, 'cmd.exe'),
        /ANALIENX_RDC_(EXECUTION_IDENTITY_INVALID|OPTIONS_INVALID)/);
    }
    await assert.rejects(routeAnalienxRunner({
      command: envelope(f.worktree) + 'git status',
      shell: 'cmd.exe',
      options: { execution: { schema: 1, identity: { project_id: 'home-assistant', worktree: f.worktree } } },
    }, 'cmd.exe'), /cannot be combined with typed options/);
    await assert.rejects(routeAnalienxRunner({
      command: envelope(f.worktree) + 'python rdc_runner_bridge.py --request x.json',
      shell: 'cmd.exe',
    }, 'cmd.exe'), /cannot be invoked directly/);
  } finally { f.close(); }
});
