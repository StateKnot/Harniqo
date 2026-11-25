import { spawn } from 'node:child_process';
import type { ChildProcess } from 'node:child_process';
import { constants } from 'node:fs';
import { open, realpath } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { HermesConnectorError, sameAssignment } from '../../control-plane/workbench-connector/protocol.ts';
import type { AssignmentIdentity } from '../../control-plane/workbench-execution/index.ts';

export type DockerDeadline = {
  assignment: AssignmentIdentity;
  containerId: string;
  imageId: string;
  expiresAt: number;
  dockerBinary: string;
  awaitStart?: boolean;
};

export type DeadlineGuard = { alive(): boolean; started(): Promise<void> };

export async function readDeadline(path: string): Promise<DockerDeadline> {
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.nlink !== 1 || stat.size < 1 || stat.size > 16 * 1024
      || stat.uid !== process.getuid?.() || (stat.mode & 0o077) !== 0) throw new Error('unsafe_deadline');
    return JSON.parse(await file.readFile('utf8')) as DockerDeadline;
  } finally { await file.close(); }
}

export async function startDockerDeadline(directory: string, grant: DockerDeadline): Promise<DeadlineGuard> {
  const absolute = resolve(directory);
  if (await realpath(absolute) !== absolute) throw new HermesConnectorError('invalid_configuration');
  const handle = await open(absolute, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
  try {
    const stat = await handle.stat();
    if (!stat.isDirectory() || stat.uid !== process.getuid?.() || (stat.mode & 0o077) !== 0) {
      throw new HermesConnectorError('invalid_configuration');
    }
    const path = join(absolute, 'deadline-' + grant.containerId + '.json');
    try {
      const file = await open(path, 'wx', 0o600);
      try { await file.writeFile(JSON.stringify(grant)); await file.sync(); }
      finally { await file.close(); }
      await handle.sync();
    } catch (error) {
      if (!error || typeof error !== 'object' || !('code' in error) || error.code !== 'EEXIST') throw error;
    }
    const prior = await readDeadline(path);
    if (prior.containerId !== grant.containerId || prior.imageId !== grant.imageId
      || prior.expiresAt !== grant.expiresAt || prior.dockerBinary !== grant.dockerBinary
      || !!prior.awaitStart !== !!grant.awaitStart
      || !sameAssignment(prior.assignment, grant.assignment)) throw new HermesConnectorError('access_denied');
    const child: ChildProcess = spawn(process.execPath, [fileURLToPath(new URL('./deadline-main.ts', import.meta.url)), path], {
      detached: true, stdio: ['ignore', 'ignore', 'ignore', 'ipc'],
      env: { PATH: '/usr/bin:/bin', LANG: 'C' },
    });
    const ready = await new Promise<boolean>(resolve => {
      const timer = setTimeout(() => resolve(false), 6000);
      const finish = (success: boolean) => { clearTimeout(timer); resolve(success); };
      child.once('message', message => finish(!!message && typeof message === 'object'
        && 'ready' in message && message.ready === true));
      child.once('error', () => finish(false));
      child.once('exit', () => finish(false));
    });
    if (child.connected) child.disconnect();
    child.unref();
    if (!ready) throw new HermesConnectorError('unavailable');
    return {
      async started() {
        const marker = await open(path + '.started', 'a', 0o600);
        try { await marker.sync(); } finally { await marker.close(); }
        const parent = await open(absolute, 'r');
        try { await parent.sync(); } finally { await parent.close(); }
      },
      alive() {
        if (!child.pid || child.exitCode !== null || child.signalCode !== null) return false;
        try { process.kill(child.pid, 0); return true; } catch { return false; }
      },
    };
  } finally { await handle.close(); }
}
