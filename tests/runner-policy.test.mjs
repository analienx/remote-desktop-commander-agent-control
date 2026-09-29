import test from 'node:test';
import assert from 'node:assert/strict';
import { enforceAnalienxRunnerRouting } from '../src/analienx-runner-policy.js';

const request = 'C:\\Workspace\\.analienx\\rdc-requests\\11111111-1111-1111-1111-111111111111.json';
const bridge = 'python.exe "%LOCALAPPDATA%\\RDC-Control\\rdc_runner_bridge.py" --request "' + request + '"';

test('fixed Python Runner bridge command is accepted', () => {
  assert.deepEqual(
    enforceAnalienxRunnerRouting({ command: bridge, shell: 'cmd.exe' }),
    { requestPath: request },
  );
});

test('direct process commands are rejected', () => {
  assert.throws(
    () => enforceAnalienxRunnerRouting({ command: 'node app.js', shell: 'cmd.exe' }),
    /ANALIENX_RDC_RUNNER_REQUIRED/,
  );
});

test('alternate shell is rejected', () => {
  assert.throws(
    () => enforceAnalienxRunnerRouting({ command: bridge, shell: 'powershell.exe' }),
    /cmd.exe/,
  );
});

test('request path must be UUID JSON under fixed root', () => {
  const bad = 'python.exe "%LOCALAPPDATA%\\RDC-Control\\rdc_runner_bridge.py" --request "C:\\Workspace\\tmp\\request.json"';
  assert.throws(
    () => enforceAnalienxRunnerRouting({ command: bad, shell: 'cmd.exe' }),
    /ANALIENX_RDC_RUNNER_REQUIRED/,
  );
});
