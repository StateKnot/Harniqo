import { execFile } from 'node:child_process';
import { open, rename } from 'node:fs/promises';
import { dirname } from 'node:path';
import { promisify } from 'node:util';
import { parseAssignmentIdentity } from '../../control-plane/workbench-connector/protocol.ts';
import { readDeadline } from './deadline.ts';

const exec = promisify(execFile);
const file = process.argv[2];
let interrupted = false;
process.once('SIGTERM', () => { interrupted = true; });
process.once('SIGINT', () => { interrupted = true; });

try {
  if (!file) throw new Error('deadline_file_required');
  const grant = await readDeadline(file);
  const assignment = parseAssignmentIdentity(grant.assignment);
  if (!/^[0-9a-f]{64}$/u.test(grant.containerId) || !/^sha256:[0-9a-f]{64}$/u.test(grant.imageId)
    || !/^\/(?:[A-Za-z0-9._-]+\/)*[A-Za-z0-9._-]+$/u.test(grant.dockerBinary)
    || !Number.isSafeInteger(grant.expiresAt) || grant.expiresAt > Date.now() + 120_000) throw new Error('invalid_deadline');
  const docker = (args: string[]) => exec(grant.dockerBinary, ['--host', 'unix:///var/run/docker.sock', ...args], {
    encoding: 'utf8', timeout: 4000, maxBuffer: 8192,
  });
  const format = '{"id":{{json .Id}},"image":{{json .Image}},"running":{{json .State.Running}},'
    + '"pid":{{json .State.Pid}},"restart":{{json .HostConfig.RestartPolicy.Name}},'
    + '"organization":{{json (index .Config.Labels "org.harniqo.organization-id")}},'
    + '"project":{{json (index .Config.Labels "org.harniqo.project-id")}},'
    + '"workspace":{{json (index .Config.Labels "org.harniqo.workspace-id")}},'
    + '"runtime":{{json (index .Config.Labels "org.harniqo.runtime-instance-id")}}}';
  async function state(): Promise<'running' | 'stopped' | 'absent'> {
    let value: Record<string, unknown>;
    try { value = JSON.parse((await docker(['container', 'inspect', '--format', format, grant.containerId])).stdout); }
    catch {
      const listed = await docker(['container', 'ls', '--all', '--quiet', '--no-trunc', '--filter', 'id=' + grant.containerId]);
      if (!listed.stdout.trim()) return 'absent';
      throw new Error('deadline_container_unavailable');
    }
    if (value.id !== grant.containerId || value.image !== grant.imageId || value.restart !== 'no'
      || value.organization !== assignment.organizationId || value.project !== assignment.projectId
      || value.workspace !== assignment.workspaceId || value.runtime !== assignment.runtimeInstanceId
      || typeof value.running !== 'boolean' || !Number.isSafeInteger(value.pid)) throw new Error('deadline_identity_mismatch');
    if (value.running) return 'running';
    if (value.pid !== 0) throw new Error('deadline_stop_unconfirmed');
    return 'stopped';
  }
  const initial = await state();
  if (initial === 'absent') throw new Error('deadline_container_unavailable');
  if (process.send) await new Promise<void>((resolve, reject) => {
    process.send!({ ready: true }, error => error ? reject(error) : resolve());
  });
  if (process.connected) process.disconnect();
  let started = initial === 'running';
  for (;;) {
    try {
      let observed = await state();
      if (observed === 'running') started = true;
      if (!started && grant.awaitStart) {
        const marker = await open(file + '.started', 'r').catch(() => undefined);
        if (marker) { started = true; await marker.close(); }
      }
      if (observed === 'running' && (interrupted || Date.now() >= grant.expiresAt)) {
        await docker(['container', 'stop', '--time', '1', grant.containerId]);
        observed = await state();
      }
      if (observed !== 'running' && (!grant.awaitStart || started || interrupted || Date.now() >= grant.expiresAt)) {
        const receipt = file + '.receipt.json';
        const temporary = receipt + '.' + process.pid;
        const handle = await open(temporary, 'wx', 0o600);
        try {
          await handle.writeFile(JSON.stringify({
            version: 1, containerId: grant.containerId, imageId: grant.imageId, expiresAt: grant.expiresAt,
            observedAt: Date.now(), state: observed, ...(observed === 'stopped' ? { pid: 0, running: false } : {}),
          }));
          await handle.sync();
        } finally { await handle.close(); }
        await rename(temporary, receipt);
        const directory = await open(dirname(file), 'r');
        try { await directory.sync(); } finally { await directory.close(); }
        break;
      }
    } catch {
      // Keep the independent deadline owner alive while the local daemon recovers.
    }
    const remaining = grant.expiresAt - Date.now();
    await new Promise(resolve => setTimeout(resolve, remaining <= 0 ? 1000 : Math.min(1000, Math.max(10, remaining))));
  }
} catch {
  process.exitCode = 1;
}
