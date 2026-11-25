import { execFile } from 'node:child_process';
import { request } from 'node:http';
import { lstat } from 'node:fs/promises';
import { promisify } from 'node:util';
import { HermesAdapter } from '../../../packages/adapters/hermes/adapter.ts';
import {
  HermesConnectorError, parseAssignmentIdentity, sameAssignment,
} from '../../control-plane/workbench-connector/protocol.ts';
import type { AssignmentIdentity } from '../../control-plane/workbench-execution/index.ts';
import type { ConnectorAuthority } from './index.ts';
import { startDockerDeadline } from './deadline.ts';
import type { DeadlineGuard } from './deadline.ts';

const execute = promisify(execFile);
const containerIdPattern = /^[0-9a-f]{64}$/u;
const imageIdPattern = /^sha256:[0-9a-f]{64}$/u;
const labelKeys = {
  organizationId: 'org.harniqo.organization-id',
  projectId: 'org.harniqo.project-id',
  workspaceId: 'org.harniqo.workspace-id',
  runtimeInstanceId: 'org.harniqo.runtime-instance-id',
} as const;

export type DockerHermesAuthorityOptions = {
  assignment: AssignmentIdentity;
  expiresAt: number;
  containerId: string;
  imageId: string;
  nativeOrigin: string;
  nativeToken: string;
  deadlineDirectory: string;
  awaitStart?: boolean;
  dockerBinary?: string;
  servedModelReceipt?: (assignment: Readonly<AssignmentIdentity>, nativeRunId: string, signal: AbortSignal) => Promise<string | undefined>;
};

export type ContainerState = { id: string; image: string; running: boolean; pid: number };

export function createDockerHermesAuthority(options: DockerHermesAuthorityOptions): ConnectorAuthority & {
  inspectRuntime(signal: AbortSignal): Promise<ContainerState>;
  prepareDeadline(): Promise<DeadlineGuard>;
} {
  const assignment = Object.freeze(parseAssignmentIdentity(options.assignment));
  const origin = URL.parse(options.nativeOrigin);
  if (process.platform !== 'linux' || !containerIdPattern.test(options.containerId) || !imageIdPattern.test(options.imageId)
    || !Number.isSafeInteger(options.expiresAt) || options.expiresAt <= Date.now()
    || options.expiresAt > Date.now() + 120_000
    || !origin || !/^http:\/\/127\.0\.0\.1:\d{5}\/?$/u.test(options.nativeOrigin)
    || origin.pathname !== '/' || Number(origin.port) < 10000 || Number(origin.port) > 10100
    || !/^[\x21-\x7e]{32,1024}$/u.test(options.nativeToken)
    || typeof options.deadlineDirectory !== 'string'
    || (options.dockerBinary !== undefined && !/^\/(?:[A-Za-z0-9._-]+\/)*[A-Za-z0-9._-]+$/u.test(options.dockerBinary))) {
    throw new HermesConnectorError('invalid_configuration');
  }
  const containerId = options.containerId;
  const expectedImage = options.imageId;
  const expiresAt = options.expiresAt;
  const docker = options.dockerBinary ?? '/usr/bin/docker';
  let guardPromise: Promise<DeadlineGuard> | undefined;
  const deadlineGuard = () => guardPromise ??= startDockerDeadline(options.deadlineDirectory, {
    assignment, containerId, imageId: expectedImage, expiresAt, dockerBinary: docker,
    ...(options.awaitStart ? { awaitStart: true } : {}),
  });
  const native = new HermesAdapter({ origin: origin.origin, token: options.nativeToken, timeoutMs: 5000 });
  const nativeToken = options.nativeToken;
  const hostPort = origin.port;
  const labels = Object.entries(labelKeys).map(([field, label]) =>
    `"${field}":{{json (index .Config.Labels "${label}")}}`).join(',');
  const format = `{"id":{{json .Id}},"image":{{json .Image}},"running":{{json .State.Running}},`
    + `"pid":{{json .State.Pid}},"readonly":{{json .HostConfig.ReadonlyRootfs}},`
    + `"restart":{{json .HostConfig.RestartPolicy.Name}},`
    + `"pidMode":{{json .HostConfig.PidMode}},"ipcMode":{{json .HostConfig.IpcMode}},`
    + `"capAdd":{{json .HostConfig.CapAdd}},"capDrop":{{json .HostConfig.CapDrop}},`
    + `"devices":{{json .HostConfig.Devices}},"securityOpt":{{json .HostConfig.SecurityOpt}},`
    + `"privileged":{{json .HostConfig.Privileged}},"mounts":{{json .Mounts}},`
    + `"published":{{json (index .HostConfig.PortBindings "8642/tcp")}},"labels":{${labels}}}`;

  async function inspect(signal: AbortSignal): Promise<ContainerState> {
    let output: string;
    try {
      ({ stdout: output } = await execute(docker, ['--host', 'unix:///var/run/docker.sock', 'container', 'inspect', '--format', format, containerId], {
        timeout: 5000, maxBuffer: 64 * 1024, signal, encoding: 'utf8',
      }));
    } catch { throw new HermesConnectorError('unavailable'); }
    let value: Record<string, unknown>;
    try { value = JSON.parse(output) as Record<string, unknown>; }
    catch { throw new HermesConnectorError('unavailable'); }
    const assignedLabels = value.labels as Record<string, unknown> | null;
    const mounts = value.mounts as { Source?: string; Destination?: string; Type?: string }[] | null;
    const published = value.published as { HostIp?: string; HostPort?: string }[] | null;
    const capDrop = value.capDrop as string[] | null;
    const securityOpt = value.securityOpt as string[] | null;
    if (value.id !== containerId || value.image !== expectedImage || value.readonly !== true || value.privileged !== false
      || value.restart !== 'no'
      || typeof value.running !== 'boolean' || !Number.isSafeInteger(value.pid) || Number(value.pid) < 0
      || value.pidMode !== '' || !['private', 'none'].includes(String(value.ipcMode))
      || (value.capAdd !== null && (!Array.isArray(value.capAdd) || value.capAdd.length !== 0))
      || (value.devices !== null && (!Array.isArray(value.devices) || value.devices.length !== 0))
      || !Array.isArray(capDrop) || !capDrop.some(item => item.toUpperCase() === 'ALL')
      || !Array.isArray(securityOpt) || !securityOpt.some(item => item === 'no-new-privileges' || item === 'no-new-privileges=true')
      || !assignedLabels || !Object.keys(labelKeys).every(field => assignedLabels[field] === assignment[field as keyof typeof labelKeys])
      || !Array.isArray(mounts) || mounts.some(mount => mount.Source?.endsWith('.sock') || mount.Destination?.endsWith('.sock')
        || mount.Destination === '/var/run' || mount.Destination === '/run' || mount.Destination === '/'
        || mount.Source === '/' || mount.Source === '/var/run' || mount.Source === '/run')
      || !Array.isArray(published) || published.length !== 1
      || published[0]?.HostIp !== '127.0.0.1' || published[0]?.HostPort !== hostPort) {
      throw new HermesConnectorError('access_denied');
    }
    for (const mount of mounts) {
      if (mount.Type === 'tmpfs') continue;
      if (mount.Type !== 'bind' || typeof mount.Source !== 'string') throw new HermesConnectorError('access_denied');
      const source = await lstat(mount.Source);
      if (!source.isFile() && !source.isDirectory()) throw new HermesConnectorError('access_denied');
    }
    return { id: containerId, image: expectedImage, running: value.running, pid: Number(value.pid) };
  }

  function inGrant(actual: Readonly<AssignmentIdentity>, signal: AbortSignal): boolean {
    signal.throwIfAborted();
    return sameAssignment(assignment, actual) && Date.now() < expiresAt;
  }

  return {
    inspectRuntime: inspect,
    prepareDeadline: deadlineGuard,
    async authorize(actual, signal) {
      if (!inGrant(actual, signal)) return null;
      const guard = await deadlineGuard();
      const container = await inspect(signal);
      if (container.running && !guard.alive()) return null;
      return Date.now() < expiresAt ? { expiresAt } : null;
    },
    async hasNativeSession(actual, signal) {
      if (!inGrant(actual, signal) || !(await inspect(signal)).running) return false;
      const body = await new Promise<{ status: number; text: string }>((resolve, reject) => {
        const outgoing = request(new URL('/api/sessions/' + encodeURIComponent(actual.nativeSessionId), origin), {
          method: 'GET', headers: { authorization: 'Bearer ' + nativeToken, accept: 'application/json' },
          signal: AbortSignal.any([signal, AbortSignal.timeout(5000)]), agent: false, maxHeaderSize: 8192,
        }, async response => {
          try {
            if (response.statusCode === 404) { response.resume(); resolve({ status: 404, text: '' }); return; }
            if (response.statusCode !== 200 || !/^application\/json(?:\s*;|$)/iu.test(response.headers['content-type'] ?? '')) {
              throw new HermesConnectorError('unavailable');
            }
            const chunks: Buffer[] = [];
            let size = 0;
            for await (const item of response) {
              const chunk = Buffer.isBuffer(item) ? item : Buffer.from(item);
              size += chunk.byteLength;
              if (size > 256 * 1024) throw new HermesConnectorError('unavailable');
              chunks.push(chunk);
            }
            if (!response.complete) throw new HermesConnectorError('unavailable');
            resolve({ status: 200, text: new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks, size)) });
          } catch {
            response.destroy();
            reject(new HermesConnectorError('unavailable'));
          }
        });
        outgoing.once('error', () => reject(new HermesConnectorError('unavailable')));
        outgoing.end();
      });
      if (body.status === 404) return false;
      try {
        const result = JSON.parse(body.text) as { session?: { id?: unknown } };
        return result.session?.id === actual.nativeSessionId;
      } catch { throw new HermesConnectorError('unavailable'); }
    },
    async confirmTerminal(actual, nativeRunId, signal) {
      if (!inGrant(actual, signal)) return null;
      const container = await inspect(signal);
      // The server has already persisted this native terminal result and reply.
      // A stopped immutable container can therefore reconstruct writer proof
      // after the stop receipt was lost without resuming its execution.
      const observed = container.running ? await native.inspect(nativeRunId, actual.nativeSessionId, signal) : undefined;
      if (observed && observed.status !== 'completed' && observed.status !== 'failed') return null;
      let servedModel = await options.servedModelReceipt?.(actual, nativeRunId, signal);
      if (servedModel !== undefined && (!/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/u.test(servedModel) || servedModel.includes('://'))) {
        throw new HermesConnectorError('unavailable');
      }
      if (observed?.model && servedModel && observed.model !== servedModel) throw new HermesConnectorError('identity_mismatch');
      servedModel ??= observed?.model;
      if (!inGrant(actual, signal)) return null;
      if (container.running) {
        try {
          await execute(docker, ['--host', 'unix:///var/run/docker.sock', 'container', 'stop', '--time', '5', containerId], {
            timeout: 8000, maxBuffer: 1024, signal, encoding: 'utf8',
          });
        } catch { return null; }
      }
      const stopped = await inspect(signal);
      if (stopped.running || stopped.pid !== 0) return null;
      return { nativeRunId, writerExited: true, ...(servedModel ? { servedModel } : {}) };
    },
  };
}
