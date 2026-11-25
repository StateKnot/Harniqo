import { execFile } from 'node:child_process';
import { closeSync, lstatSync, openSync, realpathSync } from 'node:fs';
import { mkdir, readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { promisify } from 'node:util';
import { setTimeout as sleep } from 'node:timers/promises';
import { HermesConnectorError, parseAssignmentIdentity, sameAssignment } from '../../control-plane/workbench-connector/protocol.ts';
import type { AssignmentIdentity } from '../../control-plane/workbench-execution/index.ts';
import type { ConnectorAuthority, ConnectorBinding } from './index.ts';
import { createDockerHermesAuthority } from './docker-authority.ts';
import { readDeadline } from './deadline.ts';

const execute = promisify(execFile);
const databaseName = 'persistent-runtime.sqlite';
const applicationId = 0x48504331;
type Grant = { assignment: AssignmentIdentity; expiresAt: number; phase: 'prepared' | 'running' | 'terminal' };
type DockerAuthority = ReturnType<typeof createDockerHermesAuthority>;

export type PersistentDockerOptions = {
  binding: ConnectorBinding;
  runDeadlineMs: number;
  containerId: string;
  imageId: string;
  nativeOrigin: string;
  nativeToken: string;
  stateDirectory: string;
};

function stateDirectory(input: string): string {
  const directory = resolve(input);
  const stat = lstatSync(directory);
  if (!stat.isDirectory() || realpathSync(directory) !== directory || stat.isSymbolicLink()
    || stat.uid !== process.getuid?.() || (stat.mode & 0o077) !== 0) throw new HermesConnectorError('invalid_configuration');
  return directory;
}

function openLedger(directory: string): DatabaseSync {
  const path = join(stateDirectory(directory), databaseName);
  const stat = lstatSync(path);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1
    || stat.uid !== process.getuid?.() || (stat.mode & 0o077) !== 0) throw new HermesConnectorError('invalid_configuration');
  return new DatabaseSync(path);
}

export function initializePersistentDockerState(directory: string): void {
  const path = join(stateDirectory(directory), databaseName);
  closeSync(openSync(path, 'wx', 0o600));
  const db = openLedger(directory);
  try {
    db.exec(`PRAGMA application_id = ${applicationId}; PRAGMA user_version = 1;
      PRAGMA journal_mode = WAL; PRAGMA synchronous = FULL;
      CREATE TABLE grants (
        attempt_id TEXT PRIMARY KEY, assignment TEXT NOT NULL, expires_at INTEGER NOT NULL,
        phase TEXT NOT NULL CHECK (phase IN ('prepared','running','terminal'))
      );
      CREATE UNIQUE INDEX one_active_grant ON grants ((1)) WHERE phase <> 'terminal';`);
  } finally { db.close(); }
}

export function createPersistentDockerAuthority(options: PersistentDockerOptions): ConnectorAuthority & {
  ready(signal: AbortSignal): Promise<boolean>;
  close(): void;
} {
  const directory = stateDirectory(options.stateDirectory);
  if (!Number.isInteger(options.runDeadlineMs) || options.runDeadlineMs < 1000 || options.runDeadlineMs > 120_000) {
    throw new HermesConnectorError('invalid_configuration');
  }
  const binding = Object.freeze({ ...options.binding });
  const seed = parseAssignmentIdentity({ ...binding,
    runId: '00000000-0000-4000-8000-000000000001', commandId: '00000000-0000-4000-8000-000000000002',
    attemptId: '00000000-0000-4000-8000-000000000003', leaseGeneration: 1, nativeSessionId: 'configuration-validation',
  });
  const inspector = createDockerHermesAuthority({
    ...options, assignment: seed, expiresAt: Date.now() + options.runDeadlineMs, deadlineDirectory: directory,
  });
  const db = openLedger(directory);
  try {
    if (db.prepare('PRAGMA application_id').get()?.application_id !== applicationId
      || db.prepare('PRAGMA user_version').get()?.user_version !== 1) throw new HermesConnectorError('invalid_configuration');
    db.exec('PRAGMA locking_mode = EXCLUSIVE; PRAGMA journal_mode = WAL; PRAGMA synchronous = FULL; BEGIN EXCLUSIVE; COMMIT;');
    if (db.prepare('PRAGMA synchronous').get()?.synchronous !== 2) throw new HermesConnectorError('invalid_configuration');
  } catch (error) { db.close(); throw error; }
  const authorities = new Map<string, Promise<DockerAuthority>>();
  let closed = false;

  function bound(identity: AssignmentIdentity): boolean {
    return Object.keys(binding).every(key => identity[key as keyof AssignmentIdentity] === binding[key as keyof ConnectorBinding]);
  }

  function parseGrant(row: Record<string, unknown>): Grant {
    return { assignment: parseAssignmentIdentity(JSON.parse(String(row.assignment))),
      expiresAt: Number(row.expires_at), phase: row.phase as Grant['phase'] };
  }

  function existing(identity: AssignmentIdentity): Grant | undefined {
    const row = db.prepare('SELECT * FROM grants WHERE attempt_id = ?').get(identity.attemptId);
    if (!row) return;
    const grant = parseGrant(row);
    if (!sameAssignment(grant.assignment, identity)) throw new HermesConnectorError('identity_mismatch');
    return grant;
  }

  function active(): Grant | undefined {
    const row = db.prepare("SELECT * FROM grants WHERE phase <> 'terminal'").get();
    return row ? parseGrant(row) : undefined;
  }

  function grantDirectory(identity: AssignmentIdentity): string {
    return join(directory, 'grant-' + identity.attemptId);
  }

  async function stoppedReceipt(grant: Grant): Promise<boolean> {
    try {
      const raw = await readFile(join(grantDirectory(grant.assignment), 'deadline-' + options.containerId + '.json.receipt.json'), 'utf8');
      const receipt = JSON.parse(raw) as Record<string, unknown>;
      return receipt.containerId === options.containerId && receipt.imageId === options.imageId
        && receipt.expiresAt === grant.expiresAt && receipt.state === 'stopped' && receipt.pid === 0 && receipt.running === false;
    } catch { return false; }
  }

  async function healthy(signal: AbortSignal): Promise<boolean> {
    try {
      const response = await fetch(new URL('/health', options.nativeOrigin), {
        signal: AbortSignal.any([signal, AbortSignal.timeout(1000)]), redirect: 'error', credentials: 'omit',
      });
      await response.body?.cancel();
      return response.status === 200;
    } catch { return false; }
  }

  async function authority(identity: AssignmentIdentity): Promise<DockerAuthority> {
    const found = authorities.get(identity.attemptId);
    if (found) return found;
    const loading = (async () => {
      let grant = existing(identity);
      let created = false;
      if (!grant) {
        if (active()) throw new HermesConnectorError('access_denied');
        const latest = db.prepare('SELECT * FROM grants ORDER BY rowid DESC LIMIT 1').get();
        if (latest) {
          const previous = parseGrant(latest);
          if (previous.assignment.leaseGeneration >= identity.leaseGeneration || !await stoppedReceipt(previous)) {
            throw new HermesConnectorError('access_denied');
          }
        }
        if (Number(db.prepare('SELECT count(*) AS count FROM grants').get()?.count) >= 128) {
          throw new HermesConnectorError('unavailable');
        }
        grant = { assignment: identity, expiresAt: Date.now() + options.runDeadlineMs, phase: 'prepared' };
        db.prepare("INSERT INTO grants VALUES (?, ?, ?, 'prepared')")
          .run(identity.attemptId, JSON.stringify(identity), grant.expiresAt);
        created = true;
      }
      if (grant.phase === 'terminal' || grant.expiresAt <= Date.now()) throw new HermesConnectorError('access_denied');
      const privateDirectory = grantDirectory(identity);
      if (created) await mkdir(privateDirectory, { mode: 0o700 });
      const current = createDockerHermesAuthority({ ...options, assignment: identity,
        expiresAt: grant.expiresAt, deadlineDirectory: privateDirectory, awaitStart: true });
      const operation = AbortSignal.timeout(12_000);
      const state = await current.inspectRuntime(operation);
      if (created && (state.running || state.pid !== 0)) throw new HermesConnectorError('access_denied');
      if (!created && grant.phase === 'prepared' && !state.running) throw new HermesConnectorError('unavailable');
      if (!created && grant.phase === 'prepared') {
        const armed = await readDeadline(join(privateDirectory, 'deadline-' + options.containerId + '.json'));
        if (!sameAssignment(armed.assignment, identity) || armed.expiresAt !== grant.expiresAt || armed.awaitStart !== true) {
          throw new HermesConnectorError('access_denied');
        }
      }
      const guard = await current.prepareDeadline();
      if (created) {
        if (!guard.alive() || grant.expiresAt <= Date.now()) throw new HermesConnectorError('access_denied');
        await execute('/usr/bin/docker', ['--host', 'unix:///var/run/docker.sock', 'container', 'start', options.containerId],
          { timeout: 8000, maxBuffer: 1024, signal: operation, encoding: 'utf8' });
        const started = await current.inspectRuntime(operation);
        if (!started.running || started.pid <= 0) throw new HermesConnectorError('unavailable');
      }
      await guard.started();
      db.prepare("UPDATE grants SET phase = 'running' WHERE attempt_id = ?").run(identity.attemptId);
      return current;
    })();
    authorities.set(identity.attemptId, loading);
    try { return await loading; }
    catch (error) { authorities.delete(identity.attemptId); throw error; }
  }

  return {
    async authorize(actual, signal) {
      if (closed || !bound(actual)) return null;
      try {
        const current = await authority(actual);
        const grant = await current.authorize(actual, signal);
        if (!grant) return null;
        return await healthy(signal) ? grant : null;
      } catch { return null; }
    },
    async hasNativeSession(actual, signal) {
      if (closed || !bound(actual) || active()?.assignment.attemptId !== actual.attemptId) return false;
      return (await authority(actual)).hasNativeSession(actual, signal);
    },
    async confirmTerminal(actual, nativeRunId, signal) {
      if (closed || !bound(actual) || active()?.assignment.attemptId !== actual.attemptId) return null;
      const proof = await (await authority(actual)).confirmTerminal(actual, nativeRunId, signal);
      if (!proof) return null;
      const grant = existing(actual)!;
      const deadline = Date.now() + 2500;
      while (!await stoppedReceipt(grant)) {
        if (Date.now() >= deadline) return null;
        await sleep(100, undefined, { signal });
      }
      db.prepare("UPDATE grants SET phase = 'terminal' WHERE attempt_id = ?").run(actual.attemptId);
      return proof;
    },
    async ready(signal) {
      if (closed) return false;
      try {
        const state = await inspector.inspectRuntime(signal);
        const grant = active();
        if (!grant) return !state.running && state.pid === 0
          && Number(db.prepare('SELECT count(*) AS count FROM grants').get()?.count) < 128;
        if (grant.expiresAt <= Date.now()) return false;
        return state.running && await healthy(signal);
      } catch { return false; }
    },
    close() { if (!closed) { closed = true; db.close(); } },
  };
}
