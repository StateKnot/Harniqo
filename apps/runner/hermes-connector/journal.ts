import { chmodSync, closeSync, lstatSync, openSync, readdirSync, realpathSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import type { HermesRun } from '../../../packages/adapters/hermes/adapter.ts';
import {
  HermesConnectorError, parseAssignmentIdentity, sameAssignment,
} from '../../control-plane/workbench-connector/protocol.ts';
import type { ConnectorTerminalProof } from '../../control-plane/workbench-connector/protocol.ts';
import type { AssignmentIdentity } from '../../control-plane/workbench-execution/index.ts';

const applicationId = 0x48484331;
const databaseName = 'connector.sqlite';
const maxRecords = 128;
type Phase = 'admitted' | 'attaching' | 'attached' | 'starting' | 'running' | 'terminal';
export type ConnectorRecord = {
  assignment: AssignmentIdentity;
  phase: Phase;
  startHash?: string;
  nativeRunId?: string;
  observedRun?: HermesRun;
  reply?: string;
  terminalProof?: ConnectorTerminalProof;
};

function privateDirectory(path: string): string {
  const directory = resolve(path);
  const stat = lstatSync(directory);
  if (realpathSync(directory) !== directory || !stat.isDirectory() || stat.isSymbolicLink()
    || (process.platform !== 'win32' && (stat.mode & 0o077 || stat.uid !== process.getuid?.()))) {
    throw new HermesConnectorError('invalid_configuration');
  }
  return directory;
}

function openDatabase(path: string): DatabaseSync {
  const stat = lstatSync(path);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1
    || (process.platform !== 'win32' && ((stat.mode & 0o077) !== 0 || stat.uid !== process.getuid?.()))) {
    throw new HermesConnectorError('invalid_configuration');
  }
  return new DatabaseSync(path, { enableForeignKeyConstraints: true });
}

export function initializeConnectorState(stateDirectory: string): void {
  const directory = privateDirectory(stateDirectory);
  if (readdirSync(directory).length !== 0) throw new HermesConnectorError('invalid_configuration');
  const path = join(directory, databaseName);
  const descriptor = openSync(path, 'wx', 0o600);
  closeSync(descriptor);
  const database = openDatabase(path);
  try {
    database.exec(`PRAGMA application_id = ${applicationId}; PRAGMA user_version = 1;
      PRAGMA journal_mode = WAL; PRAGMA synchronous = FULL;
      CREATE TABLE assignments (
        attempt_id TEXT PRIMARY KEY,
        assignment TEXT NOT NULL,
        phase TEXT NOT NULL CHECK (phase IN ('admitted','attaching','attached','starting','running','terminal')),
        start_hash TEXT, native_run_id TEXT UNIQUE, observed_run TEXT, reply TEXT, terminal_proof TEXT
      );
      CREATE UNIQUE INDEX one_active_assignment ON assignments ((1)) WHERE phase <> 'terminal';`);
  } finally {
    database.close();
    chmodSync(path, 0o600);
  }
}

export class ConnectorJournal {
  private readonly db: DatabaseSync;

  constructor(stateDirectory: string) {
    this.db = openDatabase(join(privateDirectory(stateDirectory), databaseName));
    try {
      if (this.db.prepare('PRAGMA application_id').get()?.application_id !== applicationId
        || this.db.prepare('PRAGMA user_version').get()?.user_version !== 1) {
        throw new HermesConnectorError('invalid_configuration');
      }
      this.db.exec('PRAGMA locking_mode = EXCLUSIVE; PRAGMA journal_mode = WAL; PRAGMA synchronous = FULL; BEGIN EXCLUSIVE; COMMIT;');
      if (this.db.prepare('PRAGMA locking_mode').get()?.locking_mode !== 'exclusive'
        || this.db.prepare('PRAGMA journal_mode').get()?.journal_mode !== 'wal'
        || this.db.prepare('PRAGMA synchronous').get()?.synchronous !== 2) {
        throw new HermesConnectorError('invalid_configuration');
      }
      this.db.prepare('SELECT count(*) FROM assignments').get();
    } catch (error) {
      this.db.close();
      throw error;
    }
  }

  read(assignment: AssignmentIdentity): ConnectorRecord | undefined {
    const row = this.db.prepare('SELECT * FROM assignments WHERE attempt_id = ?').get(assignment.attemptId);
    if (!row) return;
    const original = parseAssignmentIdentity(JSON.parse(String(row.assignment)));
    if (!sameAssignment(assignment, original)) throw new HermesConnectorError('identity_mismatch');
    return {
      assignment: original,
      phase: row.phase as Phase,
      ...(row.start_hash ? { startHash: String(row.start_hash) } : {}),
      ...(row.native_run_id ? { nativeRunId: String(row.native_run_id) } : {}),
      ...(row.observed_run ? { observedRun: JSON.parse(String(row.observed_run)) as HermesRun } : {}),
      ...(row.reply ? { reply: String(row.reply) } : {}),
      ...(row.terminal_proof ? { terminalProof: JSON.parse(String(row.terminal_proof)) as ConnectorTerminalProof } : {}),
    };
  }

  admit(assignment: AssignmentIdentity): boolean {
    if (this.read(assignment)) return true;
    if (this.db.prepare("SELECT 1 FROM assignments WHERE phase <> 'terminal'").get()
      || Number(this.db.prepare('SELECT count(*) AS count FROM assignments').get()?.count) >= maxRecords) return false;
    const latest = this.db.prepare('SELECT assignment FROM assignments ORDER BY rowid DESC LIMIT 1').get();
    if (latest && parseAssignmentIdentity(JSON.parse(String(latest.assignment))).leaseGeneration >= assignment.leaseGeneration) return false;
    this.db.prepare('INSERT INTO assignments (attempt_id, assignment, phase) VALUES (?, ?, ?)')
      .run(assignment.attemptId, JSON.stringify(assignment), 'admitted');
    return true;
  }

  update(record: ConnectorRecord): void {
    if (!this.read(record.assignment)) throw new HermesConnectorError('access_denied');
    this.db.prepare(`UPDATE assignments SET phase = ?, start_hash = ?, native_run_id = ?,
      observed_run = ?, reply = ?, terminal_proof = ? WHERE attempt_id = ?`)
      .run(record.phase, record.startHash ?? null, record.nativeRunId ?? null,
        record.observedRun ? JSON.stringify(record.observedRun) : null, record.reply ?? null,
        record.terminalProof ? JSON.stringify(record.terminalProof) : null, record.assignment.attemptId);
  }

  close(): void {
    this.db.close();
  }
}
