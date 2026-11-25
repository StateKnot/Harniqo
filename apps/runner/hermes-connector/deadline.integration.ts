import assert from 'node:assert/strict';
import { execFile, spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { chmod, mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';
import { test } from 'node:test';
import { createDockerHermesAuthority } from './docker-authority.ts';

const execute = promisify(execFile);
test('the independent deadline stops the pinned container after its connector parent exits', { timeout: 20_000 }, async () => {
  assert.equal(process.platform, 'linux');
  const image = process.env.HARNIQO_DEADLINE_TEST_IMAGE;
  assert.match(image ?? '', /^sha256:[0-9a-f]{64}$/u);
  const directory = await realpath(await mkdtemp(join(tmpdir(), 'harniqo-deadline-')));
  await chmod(directory, 0o700);
  const docker = (args: string[]) => execute('/usr/bin/docker', ['--host', 'unix:///var/run/docker.sock', ...args], {
    encoding: 'utf8', timeout: 5000, maxBuffer: 8192,
  });
  const assignment = {
    organizationId: randomUUID(), projectId: randomUUID(), principalId: randomUUID(), runId: randomUUID(),
    commandId: randomUUID(), attemptId: randomUUID(), workspaceId: randomUUID(), executionTargetId: randomUUID(),
    deploymentRevisionId: randomUUID(), configurationRevisionId: randomUUID(), inputSnapshotId: randomUUID(),
    budgetPolicyId: randomUUID(), runtimeInstanceId: randomUUID(), harnessReleaseId: randomUUID(), platformEpoch: randomUUID(),
    instanceGeneration: 1, leaseGeneration: 1, nativeSessionId: 'harniqo_' + randomUUID(),
  };
  let container = '';
  try {
    container = (await docker(['run', '-d', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
      '--pids-limit', '32', '--memory', '128m', '--cpus', '0.25', '--publish', '127.0.0.1:10098:8642',
      '--label', 'org.harniqo.organization-id=' + assignment.organizationId,
      '--label', 'org.harniqo.project-id=' + assignment.projectId,
      '--label', 'org.harniqo.workspace-id=' + assignment.workspaceId,
      '--label', 'org.harniqo.runtime-instance-id=' + assignment.runtimeInstanceId,
      '--entrypoint', '/bin/sh', image!, '-c', 'sleep 30'])).stdout.trim();
    const config = {
      assignment, expiresAt: Date.now() + 2000, containerId: container, imageId: image!,
      nativeOrigin: 'http://127.0.0.1:10098', nativeToken: 'synthetic-native-token-with-no-model-access',
      deadlineDirectory: directory,
    };
    const configPath = join(directory, 'parent.json');
    const parentPath = join(directory, 'parent.mjs');
    await writeFile(configPath, JSON.stringify(config), { mode: 0o600 });
    await writeFile(parentPath, `import { readFile } from 'node:fs/promises';
      import { createDockerHermesAuthority } from ${JSON.stringify(new URL('./docker-authority.ts', import.meta.url).href)};
      const config = JSON.parse(await readFile(process.argv[2], 'utf8'));
      const authority = createDockerHermesAuthority(config);
      if (!await authority.authorize(config.assignment, AbortSignal.timeout(5000))) process.exitCode = 1;
      else console.log('authorized-parent-exiting');
    `, { mode: 0o600 });
    const parent = spawn(process.execPath, [parentPath, configPath], {
      stdio: ['ignore', 'pipe', 'pipe'], env: { PATH: '/usr/bin:/bin', LANG: 'C' },
    });
    let output = '';
    parent.stdout.on('data', value => { output += value.toString(); });
    const exited = await new Promise<number | null>((resolve, reject) => {
      parent.once('error', reject); parent.once('close', resolve);
    });
    assert.equal(exited, 0);
    assert.match(output, /authorized-parent-exiting/u);
    let stopped = false;
    while (Date.now() < config.expiresAt + 7000) {
      const state = JSON.parse((await docker(['container', 'inspect', '--format',
        '{"running":{{json .State.Running}},"pid":{{json .State.Pid}}}', container])).stdout);
      if (state.running === false && state.pid === 0) { stopped = true; break; }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.equal(stopped, true);
    const extended = createDockerHermesAuthority({ ...config, expiresAt: Date.now() + 30_000 });
    await assert.rejects(extended.authorize(assignment, AbortSignal.timeout(3000)), { code: 'access_denied' });
    let receipt: { state?: string; expiresAt?: number } | undefined;
    for (let retry = 0; retry < 20; retry++) {
      try { receipt = JSON.parse(await readFile(join(directory, 'deadline-' + container + '.json.receipt.json'), 'utf8')); break; }
      catch { await new Promise(resolve => setTimeout(resolve, 100)); }
    }
    assert.equal(receipt?.state, 'stopped');
    assert.equal(receipt?.expiresAt, config.expiresAt);
  } finally {
    if (container) await docker(['container', 'rm', '--force', container]);
    await rm(directory, { recursive: true, force: true });
  }
});
