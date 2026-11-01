import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  chmodSync, closeSync, linkSync, mkdtempSync, openSync, readFileSync,
  renameSync, rmSync, symlinkSync, writeFileSync, writeSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { createHash } from 'node:crypto';
import { DATABASE_FILENAME, EventSpool, EventSpoolError, REQUIRED_NODE_VERSION } from './spool.ts';
import type { CommittedAck, SpoolErrorCode, SpoolLimits, SpoolOptions, SpoolScope } from './types.ts';

const scope: SpoolScope = {
  organization_id: '00000000-0000-4000-8000-000000000001',
  project_id: '00000000-0000-4000-8000-000000000002',
  run_id: '00000000-0000-4000-8000-000000000004',
  attempt_id: '00000000-0000-4000-8000-000000000005',
  runtime_instance_id: '00000000-0000-4000-8000-000000000009',
  stream_id: '00000000-0000-4000-8000-000000000028',
  binding: { platform_epoch: '00000000-0000-4000-8000-000000000003', lease_generation: 1 },
};
function body(seq: number, text = 'Synthetic delta', override: Record<string, unknown> = {}): Buffer {
  return Buffer.from(JSON.stringify({
    contract_version: '0.1.0',
    scope: { organization_id: scope.organization_id, project_id: scope.project_id },
    run_id: scope.run_id, attempt_id: scope.attempt_id, runtime_instance_id: scope.runtime_instance_id,
    stream_id: scope.stream_id, binding: scope.binding, source_seq: seq,
    observed_at: '2026-10-29T00:00:00Z', type: 'text.delta',
    payload: { block_id: '00000000-0000-4000-8000-000000000029', text }, ...override,
  }));
}
function ack(seq: number, override: Partial<CommittedAck> = {}): CommittedAck {
  return { attempt_id: scope.attempt_id, stream_id: scope.stream_id, binding: scope.binding, level: 'committed', committed_through: seq, ...override };
}
function expect(code: SpoolErrorCode, call: () => unknown, uncertain = false): EventSpoolError {
  let caught: EventSpoolError | undefined;
  assert.throws(call, error => {
    assert.ok(error instanceof EventSpoolError);
    assert.equal(error.code, code);
    assert.equal(error.mayHaveCommitted, uncertain);
    assert.equal(error.message, `Event spool: ${code}`);
    caught = error;
    return true;
  });
  return caught!;
}
function fixture(t: { after: (fn: () => void) => void }, limits?: Partial<SpoolLimits>) {
  const directory = mkdtempSync(join(tmpdir(), 'harniqo-spool-'));
  chmodSync(directory, 0o700);
  const options: SpoolOptions = limits === undefined ? { directory, scope } : { directory, scope, limits };
  let current: EventSpool | undefined;
  t.after(() => {
    try { current?.close(); } catch { /* fault tests deliberately preserve first failure */ }
    rmSync(directory, { recursive: true, force: true });
  });
  return {
    options, directory,
    create: () => (current = EventSpool.create(options)),
    open: () => (current = EventSpool.open(options)),
    database: join(directory, DATABASE_FILENAME),
  };
}
function external(database: string, sql: string): void {
  const db = new DatabaseSync(database);
  try { db.prepare(sql).run(); } finally { db.close(); }
}

test('real SQLite create, durable local receipt, bounded WAL, close and reopen replay', t => {
  const f = fixture(t);
  const spool = f.create();
  const bytes = body(1);
  assert.deepEqual(spool.append({ source_seq: 1, body: bytes }), { level: 'durable_received', source_seq: 1, duplicate: false });
  const stats = spool.stats();
  assert.equal(stats.node_version, REQUIRED_NODE_VERSION);
  assert.equal(stats.sqlite_version, '3.53.4');
  assert.ok(stats.sqlite_compile_options.length > 0);
  assert.equal(stats.sqlite_compile_options_sha256, createHash('sha256').update(JSON.stringify(stats.sqlite_compile_options)).digest('hex'));
  assert.equal(stats.pending_records, 1);
  assert.equal(stats.pending_bytes, bytes.length);
  assert.ok(stats.wal_bytes > 0 && stats.wal_bytes <= stats.wal_bound_bytes);
  assert.ok(stats.database_bytes <= stats.database_bound_bytes);
  assert.equal(stats.failure_code, null);
  spool.close();
  const reopened = f.open();
  assert.deepEqual(Buffer.from(reopened.pendingBatch().events[0]!.body), bytes);
  assert.equal(reopened.stats().configuration_sha256, stats.configuration_sha256);
});

test('pending duplicate is exact-byte idempotent and preserves counters', t => {
  const spool = fixture(t).create();
  const bytes = body(1);
  spool.append({ source_seq: 1, body: bytes });
  assert.equal(spool.append({ source_seq: 1, body: bytes }).duplicate, true);
  assert.equal(spool.stats().pending_records, 1);
  assert.equal(spool.stats().pending_bytes, bytes.length);
  expect('event_conflict', () => spool.append({ source_seq: 1, body: body(1, 'different') }));
  expect('event_conflict', () => spool.append({ source_seq: 1, body: Buffer.from(`${bytes.toString()}\n`) }));
  assert.equal(spool.stats().failure_code, null);
});

test('unsafe SQLite synchronization and WAL build flags are refused', t => {
  const prepare = DatabaseSync.prototype.prepare;
  for (const flag of ['NO_SYNC', 'OMIT_WAL', 'OMIT_PAGER_PRAGMAS', 'THREADSAFE=0']) {
    const f = fixture(t);
    const intercepted = t.mock.method(DatabaseSync.prototype, 'prepare', function(this: DatabaseSync, sql: string) {
      const statement = prepare.call(this, sql);
      if (sql === 'PRAGMA compile_options') t.mock.method(statement, 'all', () => [{ compile_options: flag }]);
      return statement;
    });
    try { expect('unsupported_runtime', () => f.create()); }
    finally { intercepted.mock.restore(); }
  }
});

test('caller buffer and returned replay buffer cannot mutate stored events', t => {
  const spool = fixture(t).create();
  const bytes = body(1);
  const original = Buffer.from(bytes);
  spool.append({ source_seq: 1, body: bytes });
  bytes.fill(0);
  spool.pendingBatch().events[0]!.body.fill(0);
  assert.deepEqual(Buffer.from(spool.pendingBatch().events[0]!.body), original);
});

test('out of order events expose gaps, bounded replay and contiguous frontier', t => {
  const spool = fixture(t).create();
  for (const seq of [4, 2, 1]) spool.append({ source_seq: seq, body: body(seq) });
  const batch = spool.pendingBatch();
  assert.deepEqual(batch.events.map(event => event.source_seq), [1, 2, 4]);
  assert.equal(batch.contiguous_received_through, 2);
  assert.equal(batch.highest_received, 4);
  assert.deepEqual(batch.gaps, [{ from: 3, through: 3 }]);
  expect('ack_crosses_gap', () => spool.applyCommittedAck(ack(4)));
  spool.append({ source_seq: 3, body: body(3) });
  assert.equal(spool.pendingBatch().contiguous_received_through, 4);
  assert.deepEqual(spool.pendingBatch().gaps, []);
});

test('trusted committed ACK atomically persists cursor and deletes only acknowledged bytes', t => {
  const f = fixture(t);
  const spool = f.create();
  for (const seq of [1, 2, 3]) spool.append({ source_seq: seq, body: body(seq) });
  assert.deepEqual(spool.applyCommittedAck(ack(2)), { committed_through: 2, deleted_records: 2, deleted_bytes: body(1).length + body(2).length });
  spool.close();
  const reopened = f.open();
  assert.equal(reopened.stats().committed_through, 2);
  assert.equal(reopened.stats().pending_bytes, body(3).length);
  assert.deepEqual(reopened.pendingBatch().events.map(event => event.source_seq), [3]);
  expect('event_already_committed', () => reopened.append({ source_seq: 1, body: body(1, 'any bytes') }));
});

test('equal and stale ACKs are safe and cannot move cursor backwards', t => {
  const spool = fixture(t).create();
  spool.append({ source_seq: 1, body: body(1) });
  spool.applyCommittedAck(ack(1));
  for (const seq of [1, 0]) assert.deepEqual(spool.applyCommittedAck(ack(seq)), { committed_through: 1, deleted_records: 0, deleted_bytes: 0 });
  assert.equal(spool.stats().committed_through, 1);
  expect('ack_crosses_gap', () => spool.applyCommittedAck(ack(2)));
});

test('ACK must match attempt, stream, binding and committed level', t => {
  const spool = fixture(t).create();
  spool.append({ source_seq: 1, body: body(1) });
  expect('ack_scope_mismatch', () => spool.applyCommittedAck(ack(1, { attempt_id: scope.run_id })));
  expect('ack_scope_mismatch', () => spool.applyCommittedAck(ack(1, { stream_id: scope.run_id })));
  expect('ack_scope_mismatch', () => spool.applyCommittedAck(ack(1, { binding: { ...scope.binding, lease_generation: 2 } })));
  expect('ack_scope_mismatch', () => spool.applyCommittedAck(ack(1, { binding: { ...scope.binding, platform_epoch: scope.run_id } })));
  expect('invalid_ack', () => spool.applyCommittedAck({ ...ack(1), level: 'durable_received' } as unknown as CommittedAck));
  expect('invalid_ack', () => spool.applyCommittedAck(ack(-1)));
  assert.equal(spool.stats().pending_records, 1);
});

test('full record queue preserves pending events and allows committed ACK drain', t => {
  const spool = fixture(t, { max_pending_records: 2, max_batch_records: 2 }).create();
  for (const seq of [1, 2]) spool.append({ source_seq: seq, body: body(seq) });
  expect('queue_full', () => spool.append({ source_seq: 3, body: body(3) }));
  assert.equal(spool.stats().pending_records, 2);
  assert.equal(spool.append({ source_seq: 1, body: body(1) }).duplicate, true);
  spool.applyCommittedAck(ack(1));
  spool.append({ source_seq: 3, body: body(3) });
  assert.deepEqual(spool.pendingBatch().events.map(event => event.source_seq), [2, 3]);
});

test('pending byte capacity is exact and failure does not discard earlier events', t => {
  const capacity = body(1).length * 2;
  const spool = fixture(t, { max_pending_bytes: capacity, max_event_bytes: capacity }).create();
  spool.append({ source_seq: 1, body: body(1) });
  spool.append({ source_seq: 2, body: body(2) });
  expect('queue_full', () => spool.append({ source_seq: 3, body: body(3) }));
  assert.equal(spool.stats().pending_bytes, capacity);
  spool.applyCommittedAck(ack(2));
  assert.equal(spool.stats().pending_bytes, 0);
});

test('real SQLITE_FULL page limit rolls back, preserves records and permits drain', t => {
  const spool = fixture(t, { max_pages: 16, max_event_bytes: 8192 }).create();
  let accepted = 0;
  let full = false;
  for (let seq = 1; seq < 100; seq++) {
    try { spool.append({ source_seq: seq, body: body(seq, 'x'.repeat(6000)) }); accepted++; }
    catch (error) { assert.ok(error instanceof EventSpoolError); assert.equal(error.code, 'storage_full'); assert.equal(error.mayHaveCommitted, false); full = true; break; }
  }
  assert.ok(full && accepted > 0);
  assert.equal(spool.stats().pending_records, accepted);
  assert.equal(spool.stats().failure_code, null);
  assert.ok(spool.stats().page_count <= 16);
  spool.applyCommittedAck(ack(accepted));
  spool.append({ source_seq: accepted + 1, body: body(accepted + 1) });
  assert.equal(spool.stats().pending_records, 1);
});

test('forward-gap capacity follows contiguous frontier', t => {
  const spool = fixture(t, { max_forward_gap: 2 }).create();
  spool.append({ source_seq: 2, body: body(2) });
  expect('forward_gap_exceeded', () => spool.append({ source_seq: 3, body: body(3) }));
  spool.append({ source_seq: 1, body: body(1) });
  spool.append({ source_seq: 4, body: body(4) });
  assert.equal(spool.stats().highest_received, 4);
});

test('replay batch record and byte limits do not remove unseen events', t => {
  const spool = fixture(t, { max_batch_records: 2, max_event_bytes: body(1).length, max_batch_bytes: body(1).length * 2 - 1 }).create();
  for (const seq of [1, 2, 3]) spool.append({ source_seq: seq, body: body(seq) });
  assert.deepEqual(spool.pendingBatch().events.map(event => event.source_seq), [1]);
  assert.equal(spool.pendingBatch().highest_received, 3);
  assert.equal(spool.stats().pending_records, 3);
});

test('invalid JSON, invalid UTF-8, sequence and size are rejected before mutation', t => {
  const spool = fixture(t, { max_event_bytes: 1024 }).create();
  expect('invalid_event', () => spool.append({ source_seq: 0, body: body(1) }));
  expect('invalid_event', () => spool.append({ source_seq: 1, body: Buffer.from('{') }));
  expect('invalid_event', () => spool.append({ source_seq: 1, body: Buffer.from([0xff]) }));
  expect('invalid_event', () => spool.append({ source_seq: 1, body: body(2) }));
  expect('event_too_large', () => spool.append({ source_seq: 1, body: body(1, 'x'.repeat(1024)) }));
  assert.equal(spool.stats().pending_records, 0);
});

test('event JSON header must match every fixed owner and lease field', t => {
  const spool = fixture(t).create();
  for (const key of ['run_id', 'attempt_id', 'runtime_instance_id', 'stream_id']) {
    expect('event_scope_mismatch', () => spool.append({ source_seq: 1, body: body(1, '', { [key]: scope.project_id }) }));
  }
  expect('event_scope_mismatch', () => spool.append({ source_seq: 1, body: body(1, '', { scope: { organization_id: scope.organization_id, project_id: scope.run_id } }) }));
  expect('event_scope_mismatch', () => spool.append({ source_seq: 1, body: body(1, '', { binding: { ...scope.binding, lease_generation: 2 } }) }));
  assert.equal(spool.stats().pending_records, 0);
});

test('reopening requires identical frozen configuration and scope', t => {
  const f = fixture(t);
  f.create().close();
  expect('incompatible_store', () => EventSpool.open({ ...f.options, limits: { max_pages: 1024 } }));
  expect('incompatible_store', () => EventSpool.open({ ...f.options, scope: { ...scope, binding: { ...scope.binding, lease_generation: 2 } } }));
  assert.equal(f.open().stats().pending_records, 0);
});

test('caller mutation cannot change stored scope, binding or limits', t => {
  const f = fixture(t);
  const mutableScope = { ...scope, binding: { ...scope.binding } };
  const limits = { max_pages: 1024 };
  const spool = EventSpool.create({ ...f.options, scope: mutableScope, limits });
  try {
    mutableScope.binding.lease_generation = 2;
    limits.max_pages = 16;
    spool.append({ source_seq: 1, body: body(1) });
    assert.equal(spool.stats().limits.max_pages, 1024);
  } finally { spool.close(); }
});

test('create refuses populated directory and open never initializes missing or empty database', t => {
  const f = fixture(t);
  expect('store_missing', () => f.open());
  writeFileSync(f.database, '', { mode: 0o600 });
  expect('incompatible_store', () => f.open());
  expect('store_exists', () => f.create());
  assert.equal(readFileSync(f.database).length, 0);
});

test('open rejects foreign SQLite database without altering it', t => {
  const f = fixture(t);
  external(f.database, 'CREATE TABLE unrelated(id INTEGER)');
  chmodSync(f.database, 0o600);
  const before = readFileSync(f.database);
  expect('incompatible_store', () => f.open());
  assert.deepEqual(readFileSync(f.database), before);
});

test('invalid trusted limits and runtime are rejected', t => {
  const f = fixture(t);
  for (const limits of [{ max_pages: 0 }, { max_pages: Infinity }, { max_pending_records: 1.5 }, { max_forward_gap: 0 }, { max_batch_bytes: 1 }, { max_batch_records: 257 }]) {
    expect('invalid_configuration', () => EventSpool.create({ ...f.options, limits }));
  }
  const version = Object.getOwnPropertyDescriptor(process, 'version')!;
  try {
    Object.defineProperty(process, 'version', { ...version, value: 'v24.22.0' });
    expect('unsupported_runtime', () => f.create());
  } finally { Object.defineProperty(process, 'version', version); }
});

test('private directory mode and database file mode are mandatory', t => {
  const f = fixture(t);
  chmodSync(f.directory, 0o755);
  expect('unsafe_path', () => f.create());
  chmodSync(f.directory, 0o700);
  f.create().close();
  chmodSync(f.database, 0o644);
  expect('unsafe_path', () => f.open());
});

test('symlink directory ancestry and symlink database are refused', t => {
  const f = fixture(t);
  const parent = mkdtempSync(join(tmpdir(), 'harniqo-spool-link-'));
  t.after(() => rmSync(parent, { recursive: true, force: true }));
  const link = join(parent, 'link');
  symlinkSync(f.directory, link);
  expect('unsafe_path', () => EventSpool.create({ ...f.options, directory: link }));
  expect('unsafe_path', () => EventSpool.create({ ...f.options, directory: `${link}/` }));
  const target = join(parent, 'file');
  writeFileSync(target, '', { mode: 0o600 });
  symlinkSync(target, f.database);
  expect('unsafe_path', () => f.open());
});

test('hard links and unknown files are refused without deleting evidence', t => {
  const f = fixture(t);
  f.create().close();
  const link = `${f.directory}-hardlink`;
  t.after(() => rmSync(link, { force: true }));
  linkSync(f.database, link);
  expect('unsafe_path', () => f.open());
  rmSync(link);
  writeFileSync(join(f.directory, 'unexpected'), 'preserved', { mode: 0o600 });
  expect('unsafe_path', () => f.open());
  assert.equal(readFileSync(join(f.directory, 'unexpected'), 'utf8'), 'preserved');
});

test('live database inode replacement latches writes and close still releases SQLite', t => {
  const f = fixture(t);
  const spool = f.create();
  const renamed = `${f.directory}-retained`;
  t.after(() => rmSync(renamed, { force: true }));
  renameSync(f.database, renamed);
  writeFileSync(f.database, readFileSync(renamed), { mode: 0o600 });
  // The basename still exists, but its inode is no longer the one this writer opened.
  expect('unsafe_path', () => spool.append({ source_seq: 1, body: body(1) }));
  expect('faulted', () => spool.append({ source_seq: 1, body: body(1) }));
  expect('unsafe_path', () => spool.close());
  spool.close();
});

test('unexpected SHM file is refused because EXCLUSIVE uses a private WAL index', t => {
  const f = fixture(t);
  f.create().close();
  writeFileSync(`${f.database}-shm`, Buffer.alloc(32768), { mode: 0o600 });
  expect('unsafe_path', () => f.open());
});

test('counter corruption fails closed on reopen and retains original database', t => {
  const f = fixture(t);
  const spool = f.create();
  spool.append({ source_seq: 1, body: body(1) });
  spool.close();
  external(f.database, 'UPDATE spool_metadata SET pending_bytes=pending_bytes+1 WHERE id=1');
  expect('integrity_failure', () => f.open());
  assert.ok(readFileSync(f.database).length > 0);
});

test('stored payload corruption is detected by digest and owner reconciliation', t => {
  const f = fixture(t);
  const spool = f.create();
  spool.append({ source_seq: 1, body: body(1) });
  spool.close();
  external(f.database, "UPDATE pending_events SET sha256='invalid' WHERE source_seq=1");
  expect('integrity_failure', () => f.open());
});

test('real malformed SQLite page is rejected rather than treated as an empty queue', t => {
  const f = fixture(t);
  f.create().close();
  const fd = openSync(f.database, 'r+');
  try { writeSync(fd, Buffer.alloc(64, 0xff), 0, 64, 4096); } finally { closeSync(fd); }
  expect('integrity_failure', () => f.open());
  assert.ok(readFileSync(f.database).length >= 4096);
});

test('schema additions are incompatible and never silently migrated', t => {
  const f = fixture(t);
  f.create().close();
  external(f.database, 'CREATE TABLE unrelated(id INTEGER)');
  expect('incompatible_store', () => f.open());
});

test('checkpoint failure latches without starting append; read snapshots retain failure observation', t => {
  const spool = fixture(t).create();
  const original = DatabaseSync.prototype.prepare;
  let failed = false;
  const mock = t.mock.method(DatabaseSync.prototype, 'prepare', function (this: DatabaseSync, sql: string) {
    const statement = original.call(this, sql);
    if (sql === 'PRAGMA wal_checkpoint(TRUNCATE)' && !failed) {
      failed = true;
      t.mock.method(statement, 'get', () => { throw Object.assign(new Error('injected checkpoint I/O'), { errcode: 10 }); });
    }
    return statement;
  });
  expect('storage_io', () => spool.append({ source_seq: 1, body: body(1) }));
  mock.mock.restore();
  assert.equal(spool.stats().failure_code, 'storage_io');
  assert.equal(spool.pendingBatch().events.length, 0);
  expect('faulted', () => spool.applyCommittedAck(ack(0)));
  expect('storage_io', () => spool.close());
});

test('error after a real COMMIT reports uncertainty, latches, and reopens committed bytes', t => {
  const f = fixture(t);
  const spool = f.create();
  const original = DatabaseSync.prototype.prepare;
  let failed = false;
  const mock = t.mock.method(DatabaseSync.prototype, 'prepare', function (this: DatabaseSync, sql: string) {
    const statement = original.call(this, sql);
    if (sql === 'COMMIT' && !failed) {
      failed = true;
      const run = statement.run;
      t.mock.method(statement, 'run', function (this: typeof statement, ...params: Parameters<typeof run>) {
        const result = run.apply(this, params);
        assert.ok(result);
        throw Object.assign(new Error('injected lost COMMIT result'), { errcode: 10 });
      });
    }
    return statement;
  });
  expect('storage_io', () => spool.append({ source_seq: 1, body: body(1) }), true);
  mock.mock.restore();
  assert.equal(spool.stats().failure_code, 'storage_io');
  assert.equal(spool.stats().pending_records, 1);
  expect('faulted', () => spool.append({ source_seq: 2, body: body(2) }), true);
  expect('storage_io', () => spool.close(), true);
  assert.equal(f.open().append({ source_seq: 1, body: body(1) }).duplicate, true);
});

test('failure between ACK cursor update and payload deletion rolls both back', t => {
  const f = fixture(t);
  const spool = f.create();
  spool.append({ source_seq: 1, body: body(1) });
  const original = DatabaseSync.prototype.prepare;
  const mock = t.mock.method(DatabaseSync.prototype, 'prepare', function (this: DatabaseSync, sql: string) {
    const statement = original.call(this, sql);
    if (sql === 'DELETE FROM pending_events WHERE source_seq<=?') {
      t.mock.method(statement, 'run', () => { throw Object.assign(new Error('injected ACK I/O'), { errcode: 10 }); });
    }
    return statement;
  });
  expect('storage_io', () => spool.applyCommittedAck(ack(1)));
  mock.mock.restore();
  assert.equal(spool.stats().committed_through, 0);
  assert.equal(spool.stats().pending_records, 1);
  expect('storage_io', () => spool.close());
  const reopened = f.open();
  assert.equal(reopened.stats().committed_through, 0);
  assert.equal(reopened.pendingBatch().events[0]!.source_seq, 1);
});

test('SQLite version and actual PRAGMA readback guards reject incompatible observations', t => {
  for (const target of ['version', 'PRAGMA cache_spill']) {
    const f = fixture(t);
    const original = DatabaseSync.prototype.prepare;
    const mock = t.mock.method(DatabaseSync.prototype, 'prepare', function (this: DatabaseSync, sql: string) {
      const statement = original.call(this, sql);
      if ((target === 'version' && sql.startsWith('SELECT sqlite_version()')) || sql === target) {
        const get = statement.get;
        t.mock.method(statement, 'get', function (this: typeof statement, ...params: Parameters<typeof get>) {
          const actual = get.apply(this, params);
          return { ...actual, ...(target === 'version' ? { version: '3.53.3' } : { cache_spill: 1 }) };
        });
      }
      return statement;
    });
    try { expect(target === 'version' ? 'unsupported_runtime' : 'incompatible_store', () => f.create()); }
    finally { mock.mock.restore(); }
  }
});

test('close is idempotent and all data operations reject a closed handle', t => {
  const spool = fixture(t).create();
  spool.close();
  spool.close();
  expect('closed', () => spool.stats());
  expect('closed', () => spool.pendingBatch());
  expect('closed', () => spool.append({ source_seq: 1, body: body(1) }));
  expect('closed', () => spool.applyCommittedAck(ack(0)));
});
