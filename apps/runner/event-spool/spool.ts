import { createHash } from 'node:crypto';
import {
  closeSync, constants, existsSync, fstatSync, fsyncSync, lstatSync, openSync,
  readSync, readdirSync, statfsSync,
} from 'node:fs';
import { dirname, isAbsolute, join, normalize, parse } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import type { SQLInputValue, StatementSync } from 'node:sqlite';
import type {
  AckReceipt, AppendEvent, CommittedAck, DurableReceipt, PendingBatch, SequenceState,
  SpoolErrorCode, SpoolLimits, SpoolOptions, SpoolScope, SpoolStats,
} from './types.ts';

export const REQUIRED_NODE_VERSION = 'v24.22.1';
export const REQUIRED_SQLITE_VERSION = '3.53.4';
export const REQUIRED_SQLITE_SOURCE_ID = '2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc';
export const DATABASE_FILENAME = 'spool.sqlite';
const PAGE_SIZE = 4096;
const APPLICATION_ID = 0x48515350;
const SCHEMA_VERSION = 1;
const FILE_NAMES = [DATABASE_FILENAME, `${DATABASE_FILENAME}-wal`];
const EXT4_MAGIC = 0xef53;
const DEFAULT_LIMITS: SpoolLimits = Object.freeze({
  max_pages: 2048, max_pending_bytes: 4 * 1024 * 1024, max_pending_records: 1024,
  max_event_bytes: 64 * 1024, max_batch_bytes: 1024 * 1024, max_batch_records: 256,
  max_forward_gap: 4096,
});
const META_SQL = 'CREATE TABLE spool_metadata (id INTEGER PRIMARY KEY CHECK(id=1), format INTEGER NOT NULL, configuration TEXT NOT NULL, configuration_sha256 TEXT NOT NULL, scope TEXT NOT NULL, scope_sha256 TEXT NOT NULL, committed_through INTEGER NOT NULL CHECK(committed_through>=0), pending_records INTEGER NOT NULL CHECK(pending_records>=0), pending_bytes INTEGER NOT NULL CHECK(pending_bytes>=0)) STRICT';
const EVENTS_SQL = 'CREATE TABLE pending_events (source_seq INTEGER PRIMARY KEY CHECK(source_seq>0), body BLOB NOT NULL, sha256 TEXT NOT NULL, body_bytes INTEGER NOT NULL CHECK(body_bytes>0 AND body_bytes=length(body))) STRICT';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
type Row = Record<string, unknown>;
type Identity = { dev: number; ino: number };
type State = { meta: Row; rows: Row[]; sequence: SequenceState; count: number; bytes: number };

/** Errors deliberately exclude SQLite text, paths, event bytes and credentials. */
export class EventSpoolError extends Error {
  readonly code: SpoolErrorCode;
  readonly mayHaveCommitted: boolean;
  constructor(code: SpoolErrorCode, mayHaveCommitted = false) {
    super(`Event spool: ${code}`);
    this.name = 'EventSpoolError';
    this.code = code;
    this.mayHaveCommitted = mayHaveCommitted;
  }
}

function fail(code: SpoolErrorCode, uncertain = false): never { throw new EventSpoolError(code, uncertain); }
function sha256(value: string | Uint8Array): string { return createHash('sha256').update(value).digest('hex'); }
function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function integer(value: unknown, min: number, max = Number.MAX_SAFE_INTEGER): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= min && value <= max;
}
function number(value: unknown): number {
  if (!integer(value, 0)) fail('integrity_failure');
  return value;
}
function sqlError(error: unknown, uncertain = false): EventSpoolError {
  if (error instanceof EventSpoolError) return uncertain && !error.mayHaveCommitted
    ? new EventSpoolError(error.code, true) : error;
  const raw = object(error) && typeof error.errcode === 'number' ? error.errcode & 255 : 0;
  const code = raw === 5 || raw === 6 ? 'store_locked' : raw === 13 ? 'storage_full'
    : raw === 11 || raw === 26 ? 'integrity_failure' : 'storage_io';
  return new EventSpoolError(code, uncertain);
}
function identity(stat: Identity): Identity { return { dev: stat.dev, ino: stat.ino }; }
function same(a: Identity, b: Identity): boolean { return a.dev === b.dev && a.ino === b.ino; }

function verifyMountProfile(directory: string): void {
  // statfs magic alone also matches ext2/3. Resolve the kernel's actual mount type,
  // without trusting a shell, a caller-supplied device path, or an unbounded proc read.
  if (statfsSync(directory).type !== EXT4_MAGIC) fail('unsafe_path');
  const fd = openSync('/proc/self/mountinfo', constants.O_RDONLY);
  let used = 0;
  const buffer = Buffer.alloc(4 * 1024 * 1024 + 1);
  try {
    for (;;) {
      const length = readSync(fd, buffer, used, buffer.length - used, null);
      used += length;
      if (used === buffer.length) fail('unsafe_path');
      if (length === 0) break;
    }
  } finally { closeSync(fd); }
  let selected = '';
  let type = '';
  for (const line of buffer.subarray(0, used).toString('utf8').split('\n')) {
    if (!line) continue;
    const halves = line.split(' - ');
    if (halves.length !== 2) fail('unsafe_path');
    const fields = halves[0]!.split(' ');
    const raw = fields[4];
    if (raw === undefined) fail('unsafe_path');
    const mount = raw.replace(/\\(040|011|012|134)/g, (_, octal: string) => String.fromCharCode(Number.parseInt(octal, 8)));
    if ((directory === mount || directory.startsWith(mount === '/' ? '/' : `${mount}/`)) && mount.length >= selected.length) {
      selected = mount;
      type = halves[1]!.split(' ')[0] ?? '';
    }
  }
  if (selected === '' || type !== 'ext4') fail('unsafe_path');
}

function configuration(options: SpoolOptions): { limits: SpoolLimits; scope: SpoolScope } {
  if (!object(options) || !object(options.scope) || !object(options.scope.binding)) fail('invalid_configuration');
  const scope = options.scope;
  for (const key of ['organization_id', 'project_id', 'run_id', 'attempt_id', 'runtime_instance_id', 'stream_id'] as const) {
    if (typeof scope[key] !== 'string' || !UUID.test(scope[key])) fail('invalid_configuration');
  }
  if (!UUID.test(scope.binding.platform_epoch) || !integer(scope.binding.lease_generation, 1)) fail('invalid_configuration');
  if (options.limits !== undefined && !object(options.limits)) fail('invalid_configuration');
  for (const key of Object.keys(options.limits ?? {})) if (!(key in DEFAULT_LIMITS)) fail('invalid_configuration');
  const limits = { ...DEFAULT_LIMITS, ...options.limits };
  if (!integer(limits.max_pages, 16, 65536)
    || !integer(limits.max_pending_bytes, 1, 128 * 1024 * 1024)
    || !integer(limits.max_pending_records, 1, 65536)
    || !integer(limits.max_event_bytes, 1, 1024 * 1024)
    || !integer(limits.max_batch_bytes, 1, 16 * 1024 * 1024)
    || !integer(limits.max_batch_records, 1, 256)
    || !integer(limits.max_forward_gap, 1, 1048576)
    || limits.max_event_bytes > limits.max_pending_bytes
    || limits.max_event_bytes > limits.max_batch_bytes
    || limits.max_batch_records > limits.max_pending_records) fail('invalid_configuration');
  return {
    limits: Object.freeze(limits),
    scope: Object.freeze({
      organization_id: scope.organization_id, project_id: scope.project_id, run_id: scope.run_id,
      attempt_id: scope.attempt_id, runtime_instance_id: scope.runtime_instance_id, stream_id: scope.stream_id,
      binding: Object.freeze({ platform_epoch: scope.binding.platform_epoch, lease_generation: scope.binding.lease_generation }),
    }),
  };
}

/**
 * A synchronous, single-writer local queue. No database/statement/iterator escapes this Module.
 * Every mutation starts with a verified TRUNCATE checkpoint. One bounded committed WAL may
 * remain afterwards, so recovery also covers a killed process before its next checkpoint.
 */
export class EventSpool {
  readonly #directory: string;
  readonly #path: string;
  readonly #directoryIdentity: Identity;
  readonly #directoryFd: number;
  readonly #databaseIdentity: Identity;
  readonly #limits: SpoolLimits;
  readonly #scope: SpoolScope;
  readonly #configuration: string;
  readonly #scopeJson: string;
  readonly #configurationHash: string;
  readonly #scopeHash: string;
  readonly #db: DatabaseSync;
  readonly #compileOptions: readonly string[];
  readonly #compileOptionsHash: string;
  #closed = false;
  #failure: EventSpoolError | null = null;

  static create(options: SpoolOptions): EventSpool { return new EventSpool(options, true); }
  static open(options: SpoolOptions): EventSpool { return new EventSpool(options, false); }

  private constructor(options: SpoolOptions, create: boolean) {
    const config = configuration(options);
    this.#limits = config.limits;
    this.#scope = config.scope;
    this.#configuration = JSON.stringify({ format: SCHEMA_VERSION, ...config.limits });
    this.#scopeJson = JSON.stringify(config.scope);
    this.#configurationHash = sha256(this.#configuration);
    this.#scopeHash = sha256(this.#scopeJson);
    if (process.platform !== 'linux' || process.version !== REQUIRED_NODE_VERSION || typeof process.getuid !== 'function') fail('unsupported_runtime');
    if (typeof options.directory !== 'string' || !isAbsolute(options.directory)
      || options.directory.endsWith('/') || normalize(options.directory) !== options.directory
      || options.directory === parse(options.directory).root) fail('unsafe_path');
    this.#directory = options.directory;
    this.#path = join(this.#directory, DATABASE_FILENAME);
    let directoryFd: number | undefined;
    let db: DatabaseSync | undefined;
    try {
      this.#checkAncestors();
      verifyMountProfile(this.#directory);
      const directoryStat = lstatSync(this.#directory);
      this.#directoryIdentity = identity(directoryStat);
      directoryFd = openSync(this.#directory, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
      this.#directoryFd = directoryFd;
      if (!same(fstatSync(directoryFd), directoryStat)) fail('unsafe_path');
      this.#inspectFiles(false);
      if (create) {
        if (readdirSync(this.#directory).length !== 0) fail('store_exists');
        const fd = openSync(this.#path, constants.O_CREAT | constants.O_EXCL | constants.O_RDWR | constants.O_NOFOLLOW, 0o600);
        try { fsyncSync(fd); } finally { closeSync(fd); }
        fsyncSync(directoryFd);
      } else {
        if (!existsSync(this.#path)) fail('store_missing');
        this.#verifyHeader();
      }
      this.#databaseIdentity = identity(lstatSync(this.#path));
      this.#inspectFiles(true);
      db = new DatabaseSync(this.#path, { enableForeignKeyConstraints: false, enableDoubleQuotedStringLiterals: false, allowExtension: false, timeout: 0 });
      this.#db = db;
      // Must precede the first operation that accesses WAL, including version/schema reads.
      this.#one('PRAGMA locking_mode=EXCLUSIVE');
      this.#run('PRAGMA trusted_schema=OFF');
      const version = this.#one('SELECT sqlite_version() AS version, sqlite_source_id() AS source');
      if (version.version !== REQUIRED_SQLITE_VERSION || version.source !== REQUIRED_SQLITE_SOURCE_ID) fail('unsupported_runtime');
      const compile = this.#all('PRAGMA compile_options').map(row => row.compile_options);
      if (compile.length === 0 || compile.length > 256 || compile.some(value => typeof value !== 'string'
        || value.length === 0 || value.length > 256)) fail('unsupported_runtime');
      const compileOptions = compile as string[];
      if (compileOptions.some(value => /^(?:NO_SYNC|OMIT_WAL|OMIT_PAGER_PRAGMAS|OMIT_COMPILEOPTION_DIAGS)(?:=|$)/u.test(value)
        || value === 'THREADSAFE=0')) fail('unsupported_runtime');
      this.#compileOptions = Object.freeze([...compileOptions].sort());
      this.#compileOptionsHash = sha256(JSON.stringify(this.#compileOptions));
      if (!create) this.#verifySchema();
      this.#configure();
      if (create) this.#initialize();
      this.#verifySchema();
      this.#verifyPragmas();
      this.#checkpoint();
      this.#readState(true);
      this.#inspectFiles(true);
      fsyncSync(directoryFd);
    } catch (error) {
      // Failed creation/opening never deletes or replaces evidence. Preserve the first failure.
      try { db?.close(); } catch { /* first error wins */ }
      if (directoryFd !== undefined) try { closeSync(directoryFd); } catch { /* first error wins */ }
      throw sqlError(error);
    }
  }

  append(event: AppendEvent): DurableReceipt {
    this.#writable();
    if (!object(event) || !integer(event.source_seq, 1) || !(event.body instanceof Uint8Array) || event.body.byteLength === 0) fail('invalid_event');
    if (event.body.byteLength > this.#limits.max_event_bytes) fail('event_too_large');
    const body = Buffer.from(event.body);
    this.#validateEvent(body, event.source_seq);
    const digest = sha256(body);
    return this.#mutate(() => {
      const state = this.#readState(false);
      if (event.source_seq <= state.sequence.committed_through) fail('event_already_committed');
      const existing = this.#get('SELECT body, sha256 FROM pending_events WHERE source_seq=?', event.source_seq);
      if (existing) {
        if (existing.sha256 !== digest || !(existing.body instanceof Uint8Array) || !body.equals(existing.body)) fail('event_conflict');
        return { level: 'durable_received', source_seq: event.source_seq, duplicate: true };
      }
      if (event.source_seq - state.sequence.contiguous_received_through > this.#limits.max_forward_gap) fail('forward_gap_exceeded');
      if (state.count >= this.#limits.max_pending_records || body.length > this.#limits.max_pending_bytes - state.bytes) fail('queue_full');
      this.#run('INSERT INTO pending_events(source_seq,body,sha256,body_bytes) VALUES(?,?,?,?)', event.source_seq, body, digest, body.length);
      this.#run('UPDATE spool_metadata SET pending_records=pending_records+1,pending_bytes=pending_bytes+? WHERE id=1', body.length);
      return { level: 'durable_received', source_seq: event.source_seq, duplicate: false };
    });
  }

  pendingBatch(): PendingBatch {
    return this.#read(() => {
      const state = this.#readState(false);
      const events = [];
      let bytes = 0;
      for (const row of state.rows) {
        const size = number(row.body_bytes);
        if (events.length >= this.#limits.max_batch_records || bytes + size > this.#limits.max_batch_bytes) break;
        if (!(row.body instanceof Uint8Array) || typeof row.sha256 !== 'string') fail('integrity_failure');
        events.push({ source_seq: number(row.source_seq), body: Uint8Array.from(row.body), sha256: row.sha256 });
        bytes += size;
      }
      return { ...state.sequence, events };
    });
  }

  applyCommittedAck(ack: CommittedAck): AckReceipt {
    this.#writable();
    if (!object(ack) || ack.level !== 'committed' || !integer(ack.committed_through, 0) || !object(ack.binding)
      || Object.keys(ack).some(key => !['attempt_id', 'stream_id', 'binding', 'level', 'committed_through'].includes(key))
      || Object.keys(ack.binding).some(key => !['platform_epoch', 'lease_generation'].includes(key))) fail('invalid_ack');
    if (ack.attempt_id !== this.#scope.attempt_id || ack.stream_id !== this.#scope.stream_id
      || ack.binding.platform_epoch !== this.#scope.binding.platform_epoch
      || ack.binding.lease_generation !== this.#scope.binding.lease_generation) fail('ack_scope_mismatch');
    return this.#mutate(() => {
      const state = this.#readState(false);
      if (ack.committed_through <= state.sequence.committed_through) return { committed_through: state.sequence.committed_through, deleted_records: 0, deleted_bytes: 0 };
      if (ack.committed_through > state.sequence.contiguous_received_through) fail('ack_crosses_gap');
      const deleted = state.rows.filter(row => number(row.source_seq) <= ack.committed_through);
      const bytes = deleted.reduce((total, row) => total + number(row.body_bytes), 0);
      // Cursor and deletion are one durable transaction. No unbounded historical hash ledger.
      this.#run('UPDATE spool_metadata SET committed_through=?,pending_records=pending_records-?,pending_bytes=pending_bytes-? WHERE id=1', ack.committed_through, deleted.length, bytes);
      this.#run('DELETE FROM pending_events WHERE source_seq<=?', ack.committed_through);
      return { committed_through: ack.committed_through, deleted_records: deleted.length, deleted_bytes: bytes };
    });
  }

  stats(): SpoolStats {
    return this.#read(pragmas => {
      const state = this.#readState(false);
      const sizes = this.#inspectFiles(true);
      return {
        ...state.sequence, pending_records: state.count, pending_bytes: state.bytes,
        database_bytes: sizes.database, wal_bytes: sizes.wal, shm_bytes: sizes.shm,
        page_count: number(this.#one('PRAGMA page_count').page_count),
        database_bound_bytes: this.#limits.max_pages * PAGE_SIZE, wal_bound_bytes: this.#walBound(),
        failure_code: this.#failure?.code ?? null, node_version: process.version,
        sqlite_version: REQUIRED_SQLITE_VERSION, sqlite_source_id: REQUIRED_SQLITE_SOURCE_ID,
        sqlite_compile_options: this.#compileOptions, sqlite_compile_options_sha256: this.#compileOptionsHash,
        configuration_sha256: this.#configurationHash, scope_sha256: this.#scopeHash,
        limits: this.#limits,
        pragmas,
      };
    });
  }

  close(): void {
    if (this.#closed) return;
    let first: EventSpoolError | null = this.#failure;
    try { this.#inspectFiles(true); this.#checkpoint(); fsyncSync(this.#directoryFd); }
    catch (error) { first ??= sqlError(error); }
    // Always attempt SQLite close, even after checkpoint/identity failure.
    try { this.#db.close(); } catch (error) { first ??= sqlError(error); }
    try { closeSync(this.#directoryFd); } catch (error) { first ??= sqlError(error); }
    this.#closed = true;
    if (first) throw first;
  }

  #checkAncestors(): void {
    const uid = process.getuid!();
    let path = this.#directory;
    for (;;) {
      const stat = lstatSync(path);
      if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.uid !== uid && stat.uid !== 0)) fail('unsafe_path');
      const mode = stat.mode & 0o7777;
      if (path === this.#directory) {
        if (stat.uid !== uid || mode !== 0o700) fail('unsafe_path');
      } else if ((mode & 0o022) !== 0 && (mode & 0o1000) === 0) fail('unsafe_path');
      const parent = dirname(path);
      if (parent === path) break;
      path = parent;
    }
  }

  #inspectFiles(requireDatabase: boolean): { database: number; wal: number; shm: number } {
    const stat = lstatSync(this.#directory);
    if (!stat.isDirectory() || stat.isSymbolicLink() || !same(stat, this.#directoryIdentity)
      || stat.uid !== process.getuid!() || (stat.mode & 0o7777) !== 0o700
      || !same(fstatSync(this.#directoryFd), this.#directoryIdentity)) fail('unsafe_path');
    if (statfsSync(this.#directory).type !== EXT4_MAGIC) fail('unsafe_path');
    const sizes = { database: 0, wal: 0, shm: 0 };
    const names = readdirSync(this.#directory);
    for (const name of names) {
      if (!FILE_NAMES.includes(name)) fail('unsafe_path');
      const file = lstatSync(join(this.#directory, name));
      if (!file.isFile() || file.isSymbolicLink() || file.nlink !== 1 || file.uid !== process.getuid!()
        || (file.mode & 0o7777) !== 0o600 || file.dev !== this.#directoryIdentity.dev) fail('unsafe_path');
      if (name === DATABASE_FILENAME) {
        if (requireDatabase && !same(file, this.#databaseIdentity)) fail('unsafe_path');
        sizes.database = file.size;
      } else if (name.endsWith('-wal')) sizes.wal = file.size;
    }
    if (requireDatabase && !names.includes(DATABASE_FILENAME)) fail('unsafe_path');
    if (sizes.database > this.#limits.max_pages * PAGE_SIZE || sizes.wal > this.#walBound()) fail('storage_bounds_exceeded');
    return sizes;
  }

  #verifyHeader(): void {
    const fd = openSync(this.#path, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      const header = Buffer.alloc(100);
      if (readSync(fd, header, 0, header.length, 0) !== 100
        || header.subarray(0, 16).toString('binary') !== 'SQLite format 3\0'
        || header.readUInt16BE(16) !== PAGE_SIZE || header.readUInt32BE(68) !== APPLICATION_ID
        || header.readUInt32BE(60) !== SCHEMA_VERSION) fail('incompatible_store');
    } finally { closeSync(fd); }
  }

  #configure(): void {
    this.#run('PRAGMA page_size=4096');
    if (this.#one('PRAGMA journal_mode=WAL').journal_mode !== 'wal') fail('incompatible_store');
    this.#run('PRAGMA synchronous=FULL');
    this.#run('PRAGMA cache_spill=OFF');
    this.#run(`PRAGMA cache_size=${this.#limits.max_pages}`);
    this.#run('PRAGMA temp_store=MEMORY');
    this.#one('PRAGMA wal_autocheckpoint=0');
    this.#one('PRAGMA mmap_size=0');
    this.#one(`PRAGMA max_page_count=${this.#limits.max_pages}`);
  }

  #initialize(): void {
    this.#checkpoint();
    this.#run('BEGIN IMMEDIATE');
    try {
      this.#run(META_SQL);
      this.#run(EVENTS_SQL);
      this.#run(`PRAGMA application_id=${APPLICATION_ID}`);
      this.#run(`PRAGMA user_version=${SCHEMA_VERSION}`);
      this.#run('INSERT INTO spool_metadata VALUES(1,?,?,?,?,?,0,0,0)', SCHEMA_VERSION, this.#configuration, this.#configurationHash, this.#scopeJson, this.#scopeHash);
      this.#run('COMMIT');
    } catch (error) {
      if (this.#db.isTransaction) try { this.#run('ROLLBACK'); } catch { /* preserve first error */ }
      throw error;
    }
    this.#checkpoint();
    fsyncSync(this.#directoryFd);
  }

  #verifySchema(): void {
    const schema = this.#all("SELECT name,type,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY name");
    if (schema.length !== 2 || schema[0]?.name !== 'pending_events' || schema[0]?.type !== 'table' || schema[0]?.sql !== EVENTS_SQL
      || schema[1]?.name !== 'spool_metadata' || schema[1]?.type !== 'table' || schema[1]?.sql !== META_SQL
      || this.#one('PRAGMA application_id').application_id !== APPLICATION_ID
      || this.#one('PRAGMA user_version').user_version !== SCHEMA_VERSION) fail('incompatible_store');
    const meta = this.#get('SELECT * FROM spool_metadata WHERE id=1');
    if (!meta || meta.format !== SCHEMA_VERSION || meta.configuration !== this.#configuration
      || meta.configuration_sha256 !== this.#configurationHash || meta.scope !== this.#scopeJson
      || meta.scope_sha256 !== this.#scopeHash || this.#one('SELECT count(*) AS n FROM spool_metadata').n !== 1) fail('incompatible_store');
  }

  #verifyPragmas(): Readonly<Record<string, string | number>> {
    const readbacks: Record<string, string | number> = Object.create(null);
    for (const [pragma, expected] of [
      ['locking_mode', 'exclusive'], ['journal_mode', 'wal'], ['synchronous', 2], ['page_size', PAGE_SIZE],
      ['cache_spill', 0], ['cache_size', this.#limits.max_pages], ['temp_store', 2], ['trusted_schema', 0],
      ['wal_autocheckpoint', 0], ['mmap_size', 0], ['max_page_count', this.#limits.max_pages],
    ] as const) {
      const actual = this.#one(`PRAGMA ${pragma}`)[pragma];
      if ((typeof actual !== 'string' && typeof actual !== 'number') || actual !== expected) fail('incompatible_store');
      readbacks[pragma] = actual;
    }
    if (number(this.#one('PRAGMA page_count').page_count) > this.#limits.max_pages) fail('storage_bounds_exceeded');
    return Object.freeze(readbacks);
  }

  #readState(fullCheck: boolean): State {
    if (fullCheck) {
      const check = this.#all('PRAGMA integrity_check');
      if (check.length !== 1 || check[0]?.integrity_check !== 'ok') fail('integrity_failure');
    }
    const meta = this.#get('SELECT committed_through,pending_records,pending_bytes FROM spool_metadata WHERE id=1');
    if (!meta) fail('integrity_failure');
    const committed = number(meta.committed_through);
    const count = number(meta.pending_records);
    const bytes = number(meta.pending_bytes);
    if (count > this.#limits.max_pending_records || bytes > this.#limits.max_pending_bytes) fail('integrity_failure');
    // LIMIT catches inconsistent hostile/corrupt counters without materializing an unbounded table.
    const rows = this.#all('SELECT source_seq,body,sha256,body_bytes FROM pending_events ORDER BY source_seq LIMIT ?', this.#limits.max_pending_records + 1);
    if (rows.length !== count) fail('integrity_failure');
    let actualBytes = 0;
    let previous = committed;
    let contiguous = committed;
    const gaps = [];
    for (const row of rows) {
      const seq = number(row.source_seq);
      const length = number(row.body_bytes);
      if (seq <= previous || !(row.body instanceof Uint8Array) || length !== row.body.byteLength
        || length < 1 || length > this.#limits.max_event_bytes || row.sha256 !== sha256(row.body)) fail('integrity_failure');
      this.#validateEvent(row.body, seq, true);
      if (seq > previous + 1) gaps.push({ from: previous + 1, through: seq - 1 });
      if (seq === contiguous + 1) contiguous = seq;
      previous = seq;
      actualBytes += length;
    }
    if (actualBytes !== bytes || previous - contiguous > this.#limits.max_forward_gap) fail('integrity_failure');
    return { meta, rows, count, bytes, sequence: { committed_through: committed, contiguous_received_through: contiguous, highest_received: previous, gaps } };
  }

  #validateEvent(body: Uint8Array, seq: number, stored = false): void {
    let event: unknown;
    try { event = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(body)); }
    catch { fail(stored ? 'integrity_failure' : 'invalid_event'); }
    if (!object(event) || !object(event.scope) || !object(event.binding)
      || event.source_seq !== seq || event.contract_version !== '0.1.0') fail(stored ? 'integrity_failure' : 'invalid_event');
    if (event.scope.organization_id !== this.#scope.organization_id || event.scope.project_id !== this.#scope.project_id
      || event.run_id !== this.#scope.run_id || event.attempt_id !== this.#scope.attempt_id
      || event.runtime_instance_id !== this.#scope.runtime_instance_id || event.stream_id !== this.#scope.stream_id
      || event.binding.platform_epoch !== this.#scope.binding.platform_epoch
      || event.binding.lease_generation !== this.#scope.binding.lease_generation) fail(stored ? 'integrity_failure' : 'event_scope_mismatch');
  }

  #walBound(): number {
    // One commit: <=N distinct database pages, plus conservative 32 FULL-sector padding frames.
    return 32 + (this.#limits.max_pages + 32) * (PAGE_SIZE + 24);
  }

  #checkpoint(): void {
    const result = this.#one('PRAGMA wal_checkpoint(TRUNCATE)');
    if (result.busy !== 0 || result.log !== 0 || result.checkpointed !== 0) fail('checkpoint_failed');
    const sizes = this.#inspectFiles(true);
    if (sizes.wal !== 0) fail('checkpoint_failed');
  }

  #writable(): void {
    if (this.#closed) fail('closed');
    if (this.#failure) fail('faulted', this.#failure.mayHaveCommitted);
  }

  #read<T>(read: (pragmas: Readonly<Record<string, string | number>>) => T): T {
    if (this.#closed) fail('closed');
    try { this.#inspectFiles(true); const pragmas = this.#verifyPragmas(); return read(pragmas); }
    catch (error) { const failure = sqlError(error); this.#failure ??= failure; throw failure; }
  }

  #mutate<T>(write: () => T): T {
    let commitAttempted = false;
    let began = false;
    try {
      this.#inspectFiles(true);
      this.#verifyPragmas();
      this.#checkpoint();
      this.#run('BEGIN IMMEDIATE');
      began = true;
      const result = write();
      commitAttempted = true;
      this.#run('COMMIT');
      this.#inspectFiles(true);
      fsyncSync(this.#directoryFd);
      return result;
    } catch (error) {
      let failure = sqlError(error, commitAttempted);
      if (began && this.#db.isTransaction) {
        try { this.#run('ROLLBACK'); }
        catch { failure = new EventSpoolError('storage_io', commitAttempted); }
      }
      const expected = ['event_already_committed', 'event_conflict', 'forward_gap_exceeded', 'queue_full', 'ack_crosses_gap'].includes(failure.code);
      // SQLITE_FULL is recoverable only after a proven rollback/checkpoint; ACK can then drain.
      if (failure.code === 'storage_full' && !commitAttempted) {
        try { this.#checkpoint(); this.#readState(true); }
        catch (reconcileError) { failure = sqlError(reconcileError); this.#failure ??= failure; }
      } else if (!expected || commitAttempted) this.#failure ??= failure;
      throw failure;
    }
  }

  #one(sql: string, ...params: SQLInputValue[]): Row {
    const row = this.#get(sql, ...params);
    if (!row) fail('integrity_failure');
    return row;
  }
  #get(sql: string, ...params: SQLInputValue[]): Row | undefined {
    return this.#statement(sql, statement => statement.get(...params));
  }
  #all(sql: string, ...params: SQLInputValue[]): Row[] {
    return this.#statement(sql, statement => statement.all(...params));
  }
  #run(sql: string, ...params: SQLInputValue[]): void {
    this.#statement(sql, statement => { statement.run(...params); });
  }
  #statement<T>(sql: string, execute: (statement: StatementSync) => T): T {
    // Statement.close landed in the pinned Node release after our @types snapshot.
    const statement = this.#db.prepare(sql) as StatementSync & { close(): void };
    let failed = false;
    try { return execute(statement); }
    catch (error) { failed = true; throw error; }
    finally {
      try { statement.close(); }
      catch (error) { if (!failed) throw error; }
    }
  }
}
