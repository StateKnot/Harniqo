import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { chmod, mkdtemp, realpath, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';
import { test } from 'node:test';
import { createPersistentDockerAuthority, initializePersistentDockerState } from './persistent.ts';

const execute = promisify(execFile);
test('one dedicated runtime starts two sequential immutable grants and preserves unknown attempts across restart', { timeout: 30_000 }, async () => {
  assert.equal(process.platform, 'linux');
  const image = process.env.HARNIQO_PERSISTENT_TEST_IMAGE;
  assert.match(image ?? '', /^sha256:[0-9a-f]{64}$/u);
  const directory = await realpath(await mkdtemp(join(tmpdir(), 'harniqo-persistent-')));
  await chmod(directory, 0o700);
  const docker = (args: string[]) => execute('/usr/bin/docker', ['--host', 'unix:///var/run/docker.sock', ...args],
    { encoding: 'utf8', timeout: 8000, maxBuffer: 8192 });
  const binding = {
    organizationId: randomUUID(), projectId: randomUUID(), principalId: randomUUID(), workspaceId: randomUUID(),
    executionTargetId: randomUUID(), deploymentRevisionId: randomUUID(), configurationRevisionId: randomUUID(),
    inputSnapshotId: randomUUID(), budgetPolicyId: randomUUID(), runtimeInstanceId: randomUUID(),
    harnessReleaseId: randomUUID(), platformEpoch: randomUUID(), instanceGeneration: 1,
  };
  const identity = (leaseGeneration: number) => ({ ...binding, leaseGeneration, nativeSessionId: 'persistent-session',
    runId: randomUUID(), commandId: randomUUID(), attemptId: randomUUID() });
  let container = '';
  let authority: ReturnType<typeof createPersistentDockerAuthority> | undefined;
  try {
    const fixture = join(directory, 'fixture.py');
    await writeFile(fixture, `import http.server,json
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  if self.path=='/health': value={'healthy':True}
  elif self.path.startswith('/v1/runs/'): value={'run_id':self.path.rsplit('/',1)[1],'session_id':'persistent-session','status':'completed','output':'fixture reply'}
  elif self.path=='/api/sessions/persistent-session': value={'session':{'id':'persistent-session'}}
  else: self.send_error(404); return
  data=json.dumps(value).encode(); self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
http.server.HTTPServer(('0.0.0.0',8642),Handler).serve_forever()
`, { mode: 0o600 });
    container = (await docker(['create', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
      '--pids-limit', '32', '--memory', '128m', '--cpus', '0.25', '--user', process.getuid!() + ':' + process.getgid!(),
      '--publish', '127.0.0.1:10096:8642', '--mount', 'type=bind,src=' + fixture + ',dst=/fixture.py,readonly',
      '--label', 'org.harniqo.organization-id=' + binding.organizationId,
      '--label', 'org.harniqo.project-id=' + binding.projectId,
      '--label', 'org.harniqo.workspace-id=' + binding.workspaceId,
      '--label', 'org.harniqo.runtime-instance-id=' + binding.runtimeInstanceId,
      '--entrypoint', '/opt/hermes/.venv/bin/python', image!, '/fixture.py'])).stdout.trim();
    initializePersistentDockerState(directory);
    const options = { binding, containerId: container, imageId: image!, nativeOrigin: 'http://127.0.0.1:10096',
      nativeToken: 'synthetic-native-token-with-no-model-access', stateDirectory: directory, runDeadlineMs: 5000 };
    authority = createPersistentDockerAuthority(options);
    assert.equal(await authority.ready(AbortSignal.timeout(3000)), true);
    const first = identity(1);
    const second = identity(2);
    const admit = async (assignment: ReturnType<typeof identity>) => {
      for (let i = 0; i < 25; i++) {
        const grant = await authority!.authorize(assignment, AbortSignal.timeout(5000));
        if (grant) return grant;
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      assert.fail('the configured native fixture must become ready');
    };
    const initial = await admit(first);
    assert.equal(await authority.hasNativeSession(first, AbortSignal.timeout(3000)), true);
    assert.equal(await authority.authorize(second, AbortSignal.timeout(3000)), null);
    const runId = 'run_' + 'a'.repeat(32);
    assert.deepEqual(await authority.confirmTerminal(first, runId, AbortSignal.timeout(8000)),
      { nativeRunId: runId, writerExited: true });
    assert.equal(await authority.ready(AbortSignal.timeout(3000)), true);
    authority.close();
    authority = createPersistentDockerAuthority(options);
    const next = await admit(second);
    assert.ok(next.expiresAt > initial.expiresAt);
    assert.equal(await authority.authorize(first, AbortSignal.timeout(3000)), null);
    authority.close();
    authority = createPersistentDockerAuthority(options);
    const resumed = await admit(second);
    assert.equal(resumed.expiresAt, next.expiresAt);
    assert.deepEqual(await authority.confirmTerminal(second, runId, AbortSignal.timeout(8000)),
      { nativeRunId: runId, writerExited: true });
    assert.equal(await authority.ready(AbortSignal.timeout(3000)), true);
    // An unexpected already-running writer cannot become a new authorized start.
    await docker(['container', 'start', container]);
    const uncertain = identity(3);
    assert.equal(await authority.authorize(uncertain, AbortSignal.timeout(3000)), null);
    await docker(['container', 'stop', '--time', '1', container]);
    authority.close();
    authority = createPersistentDockerAuthority(options);
    assert.equal(await authority.authorize(uncertain, AbortSignal.timeout(3000)), null);
    assert.equal(await authority.authorize(identity(4), AbortSignal.timeout(3000)), null);
    assert.equal(await authority.ready(AbortSignal.timeout(3000)), false);
    const stopped = (await docker(['container', 'inspect', '--format', '{{.State.Running}} {{.State.Pid}}', container])).stdout.trim();
    assert.equal(stopped, 'false 0');
  } finally {
    authority?.close();
    if (container) await docker(['container', 'rm', '--force', container]);
    await new Promise(resolve => setTimeout(resolve, 1200));
    await rm(directory, { recursive: true, force: true });
  }
});
