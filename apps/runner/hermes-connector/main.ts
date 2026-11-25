import { constants } from 'node:fs';
import { open } from 'node:fs/promises';
import { isIP } from 'node:net';
import { isAbsolute } from 'node:path';
import { HermesAdapter } from '../../../packages/adapters/hermes/adapter.ts';
import { parseAssignmentIdentity } from '../../control-plane/workbench-connector/protocol.ts';
import { createDockerHermesAuthority, openHermesConnector } from './index.ts';
import type { HermesConnector } from './index.ts';

async function configFile(path: unknown, secret: boolean, maxBytes: number): Promise<Buffer> {
  if (typeof path !== 'string' || !isAbsolute(path)) throw new Error('invalid_connector_file');
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.nlink !== 1 || stat.size < 1 || stat.size > maxBytes
      || (process.platform !== 'win32' && (stat.uid !== process.getuid?.() || (stat.mode & (secret ? 0o077 : 0o022)) !== 0))) {
      throw new Error('invalid_connector_file');
    }
    return await file.readFile();
  } finally {
    await file.close();
  }
}

function record(value: unknown, keys: readonly string[]): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).length !== keys.length || !keys.every(key => Object.hasOwn(value, key))) {
    throw new Error('invalid_connector_configuration');
  }
  return value as Record<string, unknown>;
}

async function start(): Promise<HermesConnector> {
  const config = record(JSON.parse((await configFile(process.env.HARNIQO_CONNECTOR_FILE, true, 16 * 1024)).toString('utf8')), [
    'assignment', 'expiresAt', 'containerId', 'imageId', 'nativeOrigin', 'nativeTokenFile',
    'tls', 'controllerFingerprint256', 'stateDirectory', 'host', 'port',
  ]);
  const tlsConfig = record(config.tls, ['keyFile', 'certFile', 'caFile']);
  const assignment = parseAssignmentIdentity(config.assignment);
  if (typeof config.expiresAt !== 'number' || typeof config.containerId !== 'string'
    || typeof config.imageId !== 'string' || typeof config.nativeOrigin !== 'string'
    || typeof config.controllerFingerprint256 !== 'string'
    || typeof config.stateDirectory !== 'string' || !isAbsolute(config.stateDirectory)
    || typeof config.host !== 'string' || isIP(config.host) === 0
    || typeof config.port !== 'number' || !Number.isInteger(config.port)
    || config.port < 10000 || config.port > 10100 || config.port === 10023) {
    throw new Error('invalid_connector_configuration');
  }
  const [key, cert, ca, token] = await Promise.all([
    configFile(tlsConfig.keyFile, true, 32 * 1024), configFile(tlsConfig.certFile, false, 64 * 1024),
    configFile(tlsConfig.caFile, false, 256 * 1024), configFile(config.nativeTokenFile, true, 1024),
  ]);
  const nativeToken = new TextDecoder('utf-8', { fatal: true }).decode(token).trim();
  const authority = createDockerHermesAuthority({
    assignment, expiresAt: config.expiresAt, containerId: config.containerId, imageId: config.imageId,
    nativeOrigin: config.nativeOrigin, nativeToken, deadlineDirectory: config.stateDirectory,
  });
  if (!await authority.authorize(assignment, AbortSignal.timeout(5000))) throw new Error('connector_grant_unavailable');
  const { runId: _run, commandId: _command, attemptId: _attempt, leaseGeneration: _lease,
    nativeSessionId: _session, ...binding } = assignment;
  const connector = openHermesConnector({
    tls: { key, cert, ca }, controllerFingerprint256: config.controllerFingerprint256,
    stateDirectory: config.stateDirectory, binding, authority,
    hermes: new HermesAdapter({ origin: config.nativeOrigin, token: nativeToken, timeoutMs: 8000 }),
  });
  try {
    await new Promise<void>((resolve, reject) => {
      connector.server.once('error', reject);
      connector.server.listen(config.port as number, config.host as string, resolve);
    });
    connector.server.on('error', () => {
      console.error('Harniqo Hermes connector listener failed.');
      void connector.close().catch(() => { process.exitCode = 1; });
    });
    return connector;
  } catch (error) {
    await connector.close();
    throw error;
  }
}

try {
  const connector = await start();
  const shutdown = () => { void connector.close().catch(() => { process.exitCode = 1; }); };
  process.once('SIGTERM', shutdown);
  process.once('SIGINT', shutdown);
  console.log('Harniqo dedicated Hermes connector ready');
} catch {
  console.error('Harniqo Hermes connector startup failed. Check its private configuration, runtime grant, existing ledger and certificates.');
  process.exitCode = 1;
}
