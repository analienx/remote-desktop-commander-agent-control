// Hosted-only probe; copied beside the freshly installed npm dependencies.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';

const root = path.dirname(new URL(import.meta.url).pathname).replace(/^\/([A-Za-z]:)/, '$1');
const packageRoot = path.join(root, 'node_modules/@wonderwhy-er/desktop-commander');
const worktree = path.join(root, 'fixture');
const bridge = path.join(root, 'fixture-bridge.py');
const local = path.join(root, 'local');
const audit = path.join(local, 'Analienx/SLRunner/audit_log.py');
fs.mkdirSync(worktree, {recursive:true});
fs.mkdirSync(path.dirname(audit), {recursive:true});
fs.writeFileSync(bridge, "print('HOSTED_BRIDGE_STUB')\n");
fs.writeFileSync(audit, 'import sys\nfrom pathlib import Path\np=Path(' + JSON.stringify(path.join(root,'audit.jsonl')) + ')\nwith p.open("a") as f: f.write(sys.stdin.read()+"\\n")\n');
const transport = new StdioClientTransport({
  command: process.execPath, args:[path.join(packageRoot,'dist/index.js')],
  env:{...process.env, LOCALAPPDATA:local, ANALIENX_RDC_WORKSPACE_ROOT:root,
       ANALIENX_RDC_BRIDGE:bridge, ANALIENX_RDC_REQUEST_ROOT:path.join(root,'requests')},
  stderr:'pipe',
});
const client = new Client({name:'hosted-rdc-upgrade-probe',version:'1.0.0'});
try {
  await client.connect(transport);
  const listed = await client.listTools();
  for (const name of ['start_process','write_file','read_file'])
    assert(listed.tools.some(t=>t.name===name), name);
  const bad = await client.callTool({name:'start_process',arguments:{
    command:'runner:exec --project-id fixture --worktree "D:\\missing-upgrade-fixture" -- must-not-execute',shell:'cmd.exe',timeout_ms:3000,
  }});
  assert.equal(bad.isError,true);
  const good = await client.callTool({name:'start_process',arguments:{
    command:'runner:exec --project-id fixture --worktree "'+worktree+'" -- must-not-execute',shell:'cmd.exe',timeout_ms:3000,
  }});
  assert.notEqual(good.isError,true,JSON.stringify(good));
  assert.match(JSON.stringify(good),/HOSTED_BRIDGE_STUB/);
  const requests = fs.readdirSync(path.join(root,'requests')).filter(n=>n.endsWith('.json'));
  const payload = JSON.parse(fs.readFileSync(path.join(root,'requests',requests.at(-1))));
  assert.equal(payload.execution.identity.project_id,'fixture');
  assert.equal(payload.command,'must-not-execute');
  const written = await client.callTool({name:'write_file',arguments:{path:path.join(worktree,'audit-probe.txt'),content:'hosted audit proof'}});
  assert.notEqual(written.isError,true,JSON.stringify(written));
  const events=fs.readFileSync(path.join(root,'audit.jsonl'),'utf8').trim().split('\n').map(JSON.parse);
  assert(events.some(e=>e.operation==='write_file'&&e.phase==='received'));
  assert(events.some(e=>e.operation==='write_file'&&e.phase==='succeeded'));
  const edited=await client.callTool({name:'edit_block',arguments:{file_path:path.join(worktree,'audit-probe.txt'),old_string:'hosted audit proof',new_string:'edited audit proof'}});
  assert.notEqual(edited.isError,true,JSON.stringify(edited));
  const owned=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'ignore'});
  try {
    const killed=await client.callTool({name:'kill_process',arguments:{pid:owned.pid}});
    assert.notEqual(killed.isError,true,JSON.stringify(killed));
    const control=fs.readFileSync(path.join(root,'audit.jsonl'),'utf8').trim().split('\n').map(JSON.parse);
    assert(control.some(e=>e.operation==='edit_block'&&e.phase==='succeeded'));
    assert(control.some(e=>e.operation==='process_terminate'&&e.phase==='succeeded'&&e.pid===owned.pid));
  } finally { owned.kill(); }
  console.log('MCP_HANDSHAKE_ROUTING_AND_NATIVE_WRITE_AUDIT_PASS');
} finally { await client.close(); }
